"""Stone 0.4.17 Live Paper Trading — 6-tier targets, WebSocket, position recovery.

Changes over 0.4.15:
- 6-tier profit targets with list-based fields (replaces 3-tier target_75/1125/150)
- calc_targets() function for dynamic N-tier target computation
- get_trailing_pct() for generic N-tier trailing stop lookup
- WebSocket real-time streaming with StreamState and _Bar/_on_bar/_on_trade handlers
- Position recovery on startup (scan Alpaca positions, restore state)
- Data feed SIP support (configurable DATA_FEED)
- force_sell_position qty guard (close_position only when qty matches)
- All order.id -> str(order.id) for protective_order_id serialization
- Bracket entry with take_profit
- replace_stop_for_remaining uses any(reached_list)
- trade_type "recovered" for restored positions
- Backward-compat properties for target_75/target_1125/target_150
- save_state serialization fixes (protective_order_id, reached_list, sold_shares_list)
- Chart targets use dict comprehension from retracement tiers
- GetOrdersRequest import for recovery
- Guards: pos.reached_list and, old_open > 0 and
- Remove dead code after continue in trailing stop handler
"""

import re
import json
import time
import datetime as dt
from zoneinfo import ZoneInfo
from collections import defaultdict
from dataclasses import dataclass, field
import threading

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest, MarketOrderRequest,
    StopLimitOrderRequest, TrailingStopOrderRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType, QueryOrderStatus, OrderStatus
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import Adjustment, DataFeed

import importlib.util, sys, os

# Add parent dir to path for scanner/strategy imports
_ver_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_ver_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

# Load version-specific config
_spec = importlib.util.spec_from_file_location("config", os.path.join(_ver_dir, "config_stone_0.4.17.py"))
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)
sys.modules["config"] = config

from scanner import get_tradable_symbols
from strategy import (
    calc_atr, calc_stop_price, calc_price_at_retracement, calc_position_size,
    find_reentry_point,
)

# ── 0.4.10 Parameters ────────────────────────────────────────────────
ENTRY_LIMIT_BUFFER = getattr(config, "ENTRY_LIMIT_BUFFER", 0.005)
STOP_LIMIT_BUFFER = getattr(config, "STOP_LIMIT_BUFFER", 0.03)
FORCE_CLOSE_LIMIT_TIMEOUT = getattr(config, "FORCE_CLOSE_LIMIT_TIMEOUT", 120)
TARGET_LIMIT_BUFFER = 0.003
REENTRY_CUTOFF = getattr(config, "REENTRY_CUTOFF_TIME", "12:30")

# ── 0.4.10: Leveraged ETF detection ─────────────────────────────────
_LEV_PATTERN = re.compile(r'(2X|3X|BULL|BEAR)$', re.IGNORECASE)
_LEV_SUFFIXES = ("U", "L")


def is_leveraged_etf(symbol: str) -> bool:
    if _LEV_PATTERN.search(symbol):
        return True
    if len(symbol) > 3 and symbol[-1] in _LEV_SUFFIXES:
        return True
    if any(symbol.startswith(p) for p in ("TQQQ", "SQQQ", "UPRO", "SPXU", "TNA", "TZA",
                                           "MSTU", "MSTZ", "CONL", "NAIL", "WEBL", "FNGU",
                                           "FNGD", "SOXL", "SOXS", "TECL", "TECS", "UDOW",
                                           "SDOW", "UMDD", "SMDD", "TQQ", "SQQ", "YINN",
                                           "YANG", "CURE", "LABD", "LABU", "DRN", "DRV",
                                           "DGP", "DGZ", "BOIL", "KOLD", "NUGT", "DUST",
                                           "JNUG", "JDST", "GLL", "UGL")):
        return True
    return False


# ── Clients ────────────────────────────────────────────────────────
_ALPACA_PAPER = getattr(config, "ALPACA_PAPER", False)
trading_client = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=_ALPACA_PAPER)
data_client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)


_LOG_FILE = os.path.join(_ver_dir, "live_0417.log")
_REPORT_DIR = os.path.join(_ver_dir, "daily_reports")

def log(msg):
    now = dt.datetime.now().strftime("%H:%M:%S")
    line = f"[{now}] {msg}"
    print(line, flush=True)
    try:
        with open(_LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def smart_sleep_until(target_dt, check_interval=30):
    """Sleep until target EST datetime, with progressive logging."""
    while True:
        now = dt.datetime.now(tz=ZoneInfo("America/New_York"))
        remaining = (target_dt - now).total_seconds()
        if remaining <= 0:
            break
        if remaining > 300:
            log(f"Next event in {remaining / 60:.0f} min, sleeping...")
            time.sleep(min(remaining * 0.85, 600))
        else:
            log(f"Starting in {remaining / 60:.1f} min...")
            time.sleep(check_interval)


# ── Data feed selection ───────────────────────────────────────────────
# IEX: free, real-time, but only IEX exchange (~2-3% market volume)
# SIP: $99/mo, consolidated tape from all exchanges, better for small/mid-cap
DATA_FEED = DataFeed.IEX
_cfg_feed = getattr(config, "DATA_FEED", "iex").lower()
if _cfg_feed == "sip":
    DATA_FEED = DataFeed.SIP
    log("Using SIP data feed (consolidated, all exchanges)")
else:
    log("Using IEX data feed (free, IEX exchange only -- ~2-3% market volume)")


# ── Position tracking ──────────────────────────────────────────────
@dataclass
class LivePosition:
    symbol: str
    entry_price: float
    shares: int
    stop_price: float
    open_price: float
    trade_type: str = "first"
    remaining_shares: int = 0
    highest: float = 0.0
    prev_high: float = 0.0
    reentry_target: float = 0.0
    entry_time: dt.datetime = None
    protective_order_id: str = None
    # 0.4.10: Re-entry v2 fields
    reached_target1: bool = False
    sold_partial1_shares: int = 0
    breakeven_active: bool = False
    reentry_bar_count: int = 0
    atr: float = 0.0
    # 0.4.11: Time limit exit
    bar_count: int = 0
    time_limit_active: bool = False
    # 0.4.17: 6-tier list-based fields
    targets: list = field(default_factory=list)
    sell_ratios: list = field(default_factory=list)
    trail_pcts: list = field(default_factory=list)
    reached_list: list = None
    sold_shares_list: list = None
    target_mode: str = "retracement"

    def __post_init__(self):
        self.remaining_shares = self.shares
        if self.highest == 0.0:
            self.highest = self.entry_price
        if self.reached_list is None:
            self.reached_list = [False] * len(self.targets)
        if self.sold_shares_list is None:
            self.sold_shares_list = [0] * len(self.targets)

    # ── Backward-compat properties for 3-tier names ──
    @property
    def target_75(self): return self.targets[2] if len(self.targets) > 2 else 0
    @property
    def target_1125(self): return self.targets[4] if len(self.targets) > 4 else 0
    @property
    def target_150(self): return self.targets[5] if len(self.targets) > 5 else 0
    @property
    def reached_75(self): return self.reached_list[2] if self.reached_list and len(self.reached_list) > 2 else False
    @property
    def reached_1125(self): return self.reached_list[4] if self.reached_list and len(self.reached_list) > 4 else False
    @property
    def reached_150(self): return self.reached_list[5] if self.reached_list and len(self.reached_list) > 5 else False
    @property
    def sold_75_shares(self): return self.sold_shares_list[2] if self.sold_shares_list and len(self.sold_shares_list) > 2 else 0
    @property
    def sold_1125_shares(self): return self.sold_shares_list[4] if self.sold_shares_list and len(self.sold_shares_list) > 4 else 0
    @property
    def sold_150_shares(self): return self.sold_shares_list[5] if self.sold_shares_list and len(self.sold_shares_list) > 5 else 0


# ── 6-tier target calculation ──────────────────────────────────────
def calc_targets(entry_price: float, open_price: float):
    retracements = getattr(config, "PROFIT_RETRACEMENT_TIERS", [0.25, 0.50, 0.75, 1.00, 1.25, 1.50])
    caps = getattr(config, "TARGET_CAP_TIERS", [0.05, 0.10, 0.15, 0.20, 0.25, 0.35])
    sell_ratios = getattr(config, "PARTIAL_SELL_RATIOS", [1/8]*6)
    trail_pcts = getattr(config, "TRAILING_STOP_PCTS", [0.02, 0.025, 0.03, 0.035, 0.04, 0.05])
    targets = []
    any_capped = False
    if entry_price >= open_price:
        for i in range(len(caps)):
            targets.append(round(entry_price * (1 + caps[i]), 2))
        target_mode = "capped"
    else:
        for i in range(len(retracements)):
            ret_price = calc_price_at_retracement(entry_price, open_price, retracements[i])
            cap_price = round(entry_price * (1 + caps[i]), 2)
            t = min(ret_price, cap_price)
            if t < ret_price:
                any_capped = True
            targets.append(t)
        target_mode = "capped" if any_capped else "retracement"
    return targets, sell_ratios, trail_pcts, target_mode


def get_trailing_pct(pos) -> float:
    trail_pcts = getattr(config, "TRAILING_STOP_PCTS", [0.02, 0.025, 0.03, 0.035, 0.04, 0.05])
    if hasattr(pos, 'reached_list') and pos.reached_list:
        for ti in range(len(pos.reached_list) - 1, -1, -1):
            if pos.reached_list[ti]:
                return trail_pcts[ti] if ti < len(trail_pcts) else trail_pcts[-1]
    return trail_pcts[0]


# ── State export ────────────────────────────────────────────────────
def save_state(positions, candidates, daily_trades, daily_stopped,
               entry_checked, day_highs, accumulator, events_log):
    all_syms = set([c["symbol"] for c in candidates] + [p.symbol for p in positions])
    state = {
        "updated": dt.datetime.now().isoformat(),
        "version": "0.4.17",
        "data_feed": "SIP" if DATA_FEED == DataFeed.SIP else "IEX",
        "ws_connected": _stream_state.is_running() if _stream_state else False,
        "daily_trades": daily_trades,
        "daily_stopped": daily_stopped,
        "candidates": [
            {"symbol": c["symbol"], "open_price": c["open_price"],
             "prev_close": c["prev_close"], "gap_pct": round(c["gap_pct"], 4)}
            for c in candidates
        ],
        "positions": [
            {
                "symbol": p.symbol, "entry_price": p.entry_price,
                "shares": p.shares, "remaining_shares": p.remaining_shares,
                "stop_price": p.stop_price,
                "targets": p.targets,
                "sell_ratios": p.sell_ratios,
                "trail_pcts": p.trail_pcts,
                "reached_list": [bool(r) for r in p.reached_list] if p.reached_list else [],
                "sold_shares_list": [int(s) for s in p.sold_shares_list] if p.sold_shares_list else [],
                "target_mode": p.target_mode,
                "highest": p.highest, "trade_type": p.trade_type,
                "open_price": p.open_price,
                "entry_time": p.entry_time.isoformat() if p.entry_time else None,
                "reentry_target": p.reentry_target, "prev_high": p.prev_high,
                "protective_order_id": str(p.protective_order_id) if p.protective_order_id else None,
                # 0.4.10: Re-entry v2 fields
                "reached_target1": p.reached_target1,
                "sold_partial1_shares": p.sold_partial1_shares,
                "breakeven_active": p.breakeven_active,
                "reentry_bar_count": p.reentry_bar_count,
                "atr": p.atr,
            }
            for p in positions if p.remaining_shares > 0
        ],
        "entry_checked": list(entry_checked),
        "day_highs": {k: round(v, 4) for k, v in day_highs.items()},
        "bar_counts": {sym: accumulator.bar_count(sym) for sym in all_syms},
        "events": events_log[-50:],
    }
    state_path = os.path.join(_parent_dir, "live_state.json")
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)


def save_chart_data(accumulator, positions, chart_events, date_str):
    """Persist bar data and trade events for dashboard charting."""
    syms_data = {}
    all_syms = set(accumulator._5min_cache.keys()) | set(accumulator._minute_bars.keys())
    for sym in all_syms:
        bars_5m = accumulator.get_5min_bars(sym)
        bars_1m = list(accumulator._minute_bars.get(sym, []))

        def _fmt_ts(ts):
            if hasattr(ts, "strftime"):
                return ts.strftime("%H:%M")
            return str(ts)[-8:-3] if len(str(ts)) > 5 else str(ts)

        sym_entry = {
            "bars_5m": [
                {"ts": _fmt_ts(b["timestamp"]),
                 "o": round(b["open"], 4), "h": round(b["high"], 4),
                 "l": round(b["low"], 4), "c": round(b["close"], 4),
                 "v": b["volume"]}
                for b in bars_5m
            ],
            "bars_1m": [
                {"ts": _fmt_ts(b["timestamp"]),
                 "o": round(b["open"], 4), "h": round(b["high"], 4),
                 "l": round(b["low"], 4), "c": round(b["close"], 4),
                 "v": b["volume"]}
                for b in bars_1m
            ],
            "events": chart_events.get(sym, []),
        }
        # Add reference lines from current positions
        for pos in positions:
            if pos.symbol == sym and pos.remaining_shares > 0:
                sym_entry["entry_price"] = round(pos.entry_price, 4)
                sym_entry["stop_price"] = round(pos.stop_price, 4)
                # 0.4.17: Chart targets use dict comprehension from retracement tiers
                retracements = getattr(config, "PROFIT_RETRACEMENT_TIERS", [0.25, 0.50, 0.75, 1.00, 1.25, 1.50])
                chart_targets = {f"{int(r*100)}%": round(t, 4) for r, t in zip(retracements, pos.targets)}
                sym_entry["targets"] = chart_targets
                if pos.trade_type in ("reentry",) and pos.reentry_target > 0:
                    sym_entry["reentry_target"] = round(pos.reentry_target, 4)
                break
        syms_data[sym] = sym_entry

    chart_path = os.path.join(_ver_dir, "chart_data.json")
    try:
        with open(chart_path, "w") as f:
            json.dump({"date": date_str, "symbols": syms_data}, f, indent=2)
    except Exception as e:
        log(f"save_chart_data error: {e}")


# ── 5-min bar accumulator ──────────────────────────────────────────
class BarAccumulator:
    def __init__(self):
        self._seen_ts = defaultdict(set)
        self._minute_bars = defaultdict(list)
        self._5min_cache = defaultdict(list)

    def add_bar(self, symbol, bar):
        ts = bar.timestamp
        if hasattr(ts, "to_pydatetime"):
            ts = ts.to_pydatetime()
        ts_key = ts.replace(second=0, microsecond=0)
        if ts_key in self._seen_ts[symbol]:
            return False
        self._seen_ts[symbol].add(ts_key)
        self._minute_bars[symbol].append({
            "timestamp": ts_key,
            "open": float(bar.open), "high": float(bar.high),
            "low": float(bar.low), "close": float(bar.close),
            "volume": int(bar.volume),
        })
        self._rebuild_5min(symbol)
        return True

    def _rebuild_5min(self, symbol):
        minutes = sorted(self._minute_bars[symbol], key=lambda b: b["timestamp"])
        if not minutes:
            return
        buckets = {}
        for m in minutes:
            bucket_start = m["timestamp"].replace(
                minute=(m["timestamp"].minute // 5) * 5, second=0, microsecond=0
            )
            if bucket_start not in buckets:
                buckets[bucket_start] = {
                    "timestamp": bucket_start, "open": m["open"],
                    "high": m["high"], "low": m["low"],
                    "close": m["close"], "volume": m["volume"], "count": 1,
                }
            else:
                b = buckets[bucket_start]
                b["high"] = max(b["high"], m["high"])
                b["low"] = min(b["low"], m["low"])
                b["close"] = m["close"]
                b["volume"] += m["volume"]
                b["count"] += 1
        sorted_ts = sorted(buckets)
        completed = []
        for i, ts in enumerate(sorted_ts):
            if i < len(sorted_ts) - 1:
                b = buckets[ts]
                completed.append({
                    "timestamp": b["timestamp"], "open": b["open"],
                    "high": b["high"], "low": b["low"],
                    "close": b["close"], "volume": b["volume"],
                })
        self._5min_cache[symbol] = completed

    def get_5min_bars(self, symbol):
        return list(self._5min_cache.get(symbol, []))

    def bar_count(self, symbol):
        return len(self._5min_cache.get(symbol, []))


# ── WebSocket streaming state ───────────────────────────────────────
class _Bar:
    """Minimal bar object for accumulator compatibility."""
    pass


class StreamState:
    """Manages WebSocket stream subscriptions and real-time bar state."""
    def __init__(self, accumulator, positions_ref_fn, candidates_ref_fn):
        self.accumulator = accumulator
        self.positions_ref_fn = positions_ref_fn  # callable -> list[LivePosition]
        self.candidates_ref_fn = candidates_ref_fn  # callable -> list[dict]
        self._stream = None
        self._lock = threading.Lock()
        self._running = False
        self._trade_cache = defaultdict(dict)  # {symbol: {"price": ..., "size": ..., "ts": ...}}

    def start(self, symbols):
        """Start WebSocket stream for given symbols."""
        if not getattr(config, "USE_WEBSOCKET", False):
            log("WebSocket streaming disabled (USE_WEBSOCKET=False)")
            return
        try:
            from alpaca.data.live.stock import StockDataStream
            self._stream = StockDataStream(
                config.ALPACA_API_KEY,
                config.ALPACA_SECRET_KEY,
                feed=DATA_FEED,
            )
            for sym in symbols:
                self._stream.subscribe_bars(_on_bar, sym)
                self._stream.subscribe_trades(_on_trade, sym)
            self._running = True
            # Run in background thread
            t = threading.Thread(target=self._stream.run, daemon=True)
            t.start()
            log(f"WebSocket stream started for {len(symbols)} symbols")
        except ImportError:
            log("StockDataStream not available, falling back to polling only")
        except Exception as e:
            log(f"WebSocket start error: {e}, falling back to polling only")

    def stop(self):
        """Stop WebSocket stream."""
        self._running = False
        if self._stream:
            try:
                self._stream.stop()
            except Exception:
                pass

    def update_symbols(self, symbols):
        """Update stream subscriptions to include new symbols."""
        if not self._running or not self._stream:
            return
        try:
            for sym in symbols:
                self._stream.subscribe_bars(_on_bar, sym)
                self._stream.subscribe_trades(_on_trade, sym)
        except Exception as e:
            log(f"WebSocket subscribe error: {e}")


# Global stream state (initialized in run_trading_day)
_stream_state: StreamState | None = None


async def _on_bar(bar):
    """WebSocket bar handler — accumulates bars and triggers 5-min completion checks."""
    global _stream_state
    if _stream_state is None:
        return
    symbol = bar.symbol
    b = _Bar()
    b.timestamp = bar.timestamp
    if hasattr(b.timestamp, "to_pydatetime"):
        b.timestamp = b.timestamp.to_pydatetime()
    b.open = float(bar.open)
    b.high = float(bar.high)
    b.low = float(bar.low)
    b.close = float(bar.close)
    b.volume = int(bar.volume)
    added = _stream_state.accumulator.add_bar(symbol, b)
    if added:
        # Check for updated bar events
        _on_updated_bar(symbol, b)


async def _on_trade(trade):
    """WebSocket trade handler — caches latest trade for real-time price checks."""
    global _stream_state
    if _stream_state is None:
        return
    symbol = trade.symbol
    with _stream_state._lock:
        _stream_state._trade_cache[symbol] = {
            "price": float(trade.price),
            "size": int(trade.size),
            "ts": trade.timestamp,
        }


def _on_updated_bar(symbol, bar):
    """Called when a new minute bar is accumulated. Can trigger early exit checks."""
    # This is a hook for future real-time exit checks within the bar.
    # Currently, exits are checked in the main polling loop via snapshots.
    pass


# ── Data helpers ───────────────────────────────────────────────────
def get_snapshots(symbols):
    request = StockSnapshotRequest(symbol_or_symbols=symbols, feed=DATA_FEED)
    return data_client.get_stock_snapshot(request)


def get_prev_day_atr(symbol):
    today = dt.date.today()
    start = today - pd.Timedelta(days=30)
    request = StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
        start=start, end=today, adjustment=Adjustment.RAW, feed=DATA_FEED,
    )
    try:
        bars = data_client.get_stock_bars(request)
        if bars.df.empty:
            return 0.0
        df = bars.df
        if isinstance(df.index[0], tuple):
            df = df.xs(symbol, level="symbol")
        bar_list = [{"high": r["high"], "low": r["low"], "close": r["close"]}
                     for _, r in df.iterrows()]
        return calc_atr(bar_list, period=14)
    except Exception as e:
        log(f"ATR fetch error for {symbol}: {e}")
        return 0.0


# ── Gap scanning ───────────────────────────────────────────────────
def scan_gaps():
    symbols = get_tradable_symbols()
    log(f"Scanning {len(symbols)} symbols for gaps...")

    # 0.4.10: Filter out leveraged ETFs
    symbols = [s for s in symbols if not is_leveraged_etf(s)]
    log(f"After leveraged ETF filter: {len(symbols)} symbols")

    today = dt.date.today()
    yesterday = today - pd.Timedelta(days=5)
    end = pd.Timestamp(today, tz="America/New_York") + pd.Timedelta(days=1)

    batch_size = 500
    results = []

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        request = StockBarsRequest(
            symbol_or_symbols=batch, timeframe=TimeFrame.Day,
            start=yesterday, end=end, adjustment=Adjustment.RAW, feed=DATA_FEED,
        )
        try:
            bars = data_client.get_stock_bars(request)
        except Exception as e:
            log(f"API error: {e}")
            continue
        if bars.df.empty:
            continue
        df = bars.df

        for symbol in batch:
            try:
                sym_df = df[df.index.get_level_values("symbol") == symbol].sort_index() if isinstance(df.index[0], tuple) else df
                if len(sym_df) < 2:
                    continue
                prev = sym_df.iloc[-2]
                curr = sym_df.iloc[-1]
                prev_close = prev["close"]
                open_price = curr["open"]
                volume = prev["volume"]
                if prev_close <= 0:
                    continue
                gap_pct = (open_price / prev_close) - 1.0
                if gap_pct < config.GAP_THRESHOLD:
                    continue
                if gap_pct > getattr(config, "GAP_MAX", 100.0):
                    continue
                if volume < config.MIN_VOLUME:
                    continue
                if not (config.PRICE_MIN <= open_price <= config.PRICE_MAX):
                    continue
                dollar_volume = prev_close * volume
                if dollar_volume < config.MIN_DOLLAR_VOLUME:
                    continue
                results.append({
                    "symbol": symbol, "open_price": open_price,
                    "prev_close": prev_close, "gap_pct": gap_pct,
                    "volume": volume, "dollar_volume": dollar_volume,
                })
            except (KeyError, IndexError):
                continue

    results.sort(key=lambda x: x["gap_pct"], reverse=True)
    log(f"Found {len(results)} gap stocks")
    return results


def refresh_candidates(candidates):
    """Refresh candidate open prices at market open using snapshots.
    Re-validate gap thresholds with fresh regular-session open prices."""
    symbols = [c['symbol'] for c in candidates]
    if not symbols:
        return candidates

    log(f"Refreshing {len(symbols)} candidate prices at market open...")
    refreshed = []
    try:
        snaps = get_snapshots(symbols)
        for c in candidates:
            sym = c['symbol']
            snap = snaps.get(sym)
            updated = False
            if snap and snap.daily_bar:
                new_open = float(snap.daily_bar.open)
                if new_open > 0:
                    old_open = c['open_price']
                    c['open_price'] = new_open
                    c['gap_pct'] = (new_open / c['prev_close']) - 1.0
                    updated = True
                    if old_open > 0 and abs(new_open - old_open) / old_open > 0.005:
                        log(f"  {sym}: open updated ${old_open:.4f} -> ${new_open:.4f}")
            # Re-check gap threshold with refreshed price
            if c['gap_pct'] >= config.GAP_THRESHOLD:
                refreshed.append(c)
            else:
                log(f"  {sym}: gap narrowed to +{c['gap_pct']:.1%} (below {config.GAP_THRESHOLD:.0%}), skipping")
        log(f"After refresh: {len(refreshed)} candidates remain")
    except Exception as e:
        log(f"Refresh error: {e}, keeping original candidates")
        return candidates
    return refreshed


# ── Order execution ────────────────────────────────────────────────

def place_buy_limit(symbol, shares, price):
    try:
        order = trading_client.submit_order(LimitOrderRequest(
            symbol=symbol, qty=shares, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY, limit_price=round(price, 2),
        ))
        log(f"BUY LIMIT {symbol} {shares} @ ${price:.2f} -> order {order.id}")
        return order
    except Exception as e:
        log(f"BUY LIMIT FAILED {symbol}: {e}")
        return None


def place_bracket_entry(symbol, shares, entry_price, stop_price):
    """Place a bracket entry order with stop_loss and take_profit."""
    try:
        order = trading_client.submit_order(LimitOrderRequest(
            symbol=symbol, qty=shares, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=round(entry_price * (1 + ENTRY_LIMIT_BUFFER), 2),
            order_class="bracket",
            stop_loss={"stop_price": round(stop_price, 2)},
            take_profit={"limit_price": round(entry_price * 2, 2)},
        ))
        log(f"BRACKET ENTRY {symbol} {shares} @ ${entry_price:.2f} stop=${stop_price:.2f} tp=${entry_price * 2:.2f} -> order {order.id}")
        return order
    except Exception as e:
        log(f"BRACKET ENTRY FAILED {symbol}: {e}, falling back to plain limit")
        return place_buy_limit(symbol, shares, entry_price * (1 + ENTRY_LIMIT_BUFFER))


def place_sell_limit(symbol, shares, price):
    try:
        order = trading_client.submit_order(LimitOrderRequest(
            symbol=symbol, qty=shares, side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY, limit_price=round(price, 2),
        ))
        log(f"SELL LIMIT {symbol} {shares} @ ${price:.2f} -> order {order.id}")
        return order
    except Exception as e:
        log(f"SELL LIMIT FAILED {symbol}: {e}")
        return None


def place_sell_market(symbol, shares):
    try:
        order = trading_client.submit_order(MarketOrderRequest(
            symbol=symbol, qty=shares, side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        ))
        log(f"SELL MARKET {symbol} {shares} -> order {order.id}")
        return order
    except Exception as e:
        log(f"SELL MARKET FAILED {symbol}: {e}")
        return None


def place_stop_limit_sell(symbol, shares, stop_price, limit_price):
    try:
        order = trading_client.submit_order(StopLimitOrderRequest(
            symbol=symbol, qty=shares, side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            stop_price=round(stop_price, 2),
            limit_price=round(limit_price, 2),
        ))
        log(f"STOP-LIMIT {symbol} {shares} stop=${stop_price:.2f} limit=${limit_price:.2f} -> order {order.id}")
        return order
    except Exception as e:
        log(f"STOP-LIMIT FAILED {symbol}: {e}")
        return None


def place_trailing_stop_sell(symbol, shares, trail_percent):
    try:
        order = trading_client.submit_order(TrailingStopOrderRequest(
            symbol=symbol, qty=shares, side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            trail_percent=round(trail_percent, 1),
        ))
        log(f"TRAILING STOP {symbol} {shares} trail={trail_percent:.1f}% -> order {order.id}")
        return order
    except Exception as e:
        log(f"TRAILING STOP FAILED {symbol}: {e}")
        return None


def cancel_order(order_id):
    try:
        trading_client.cancel_order_by_id(order_id)
        log(f"CANCELLED order {order_id}")
    except Exception:
        pass


def cancel_all_orders():
    try:
        trading_client.cancel_orders()
    except Exception:
        pass


def close_all_positions():
    try:
        positions = trading_client.get_all_positions()
        for pos in positions:
            log(f"EOD CLOSE: selling {pos.qty} {pos.symbol}")
            trading_client.close_position(pos.symbol)
    except Exception as e:
        log(f"Close positions error: {e}")


def force_sell_position(symbol: str, qty: int) -> bool:
    # Cancel any pending sell orders for this symbol first
    # (they lock shares and will be replaced by this sell)
    try:
        open_orders = trading_client.get_orders(filter={"status": "open", "symbols": symbol})
        for o in open_orders:
            if o.side == OrderSide.SELL:
                cancel_order(str(o.id))
                log(f"FORCE SELL: cancelled pending sell order {o.id} for {symbol}")
    except Exception:
        pass
    time.sleep(0.5)

    # Get actual Alpaca position quantity (after cancellations)
    total_qty = 0
    try:
        alpaca_pos = trading_client.get_open_position(symbol)
        total_qty = int(float(alpaca_pos.qty))
    except Exception:
        pass

    # When exiting (stop loss, trailing stop, force close), sell ALL actual shares
    # because pending sell orders were just cancelled and qty may differ from tracked
    sell_qty = total_qty if total_qty > 0 else qty
    if sell_qty <= 0:
        log(f"FORCE SELL: {symbol} no shares to sell")
        return False

    # Method 1: Alpaca close_position API (atomic cancel + sell)
    if total_qty > 0:
        try:
            result = trading_client.close_position(symbol)
            if result:
                log(f"FORCE SELL (close_position): {symbol} {total_qty} shares")
                return True
        except Exception as e:
            log(f"close_position failed for {symbol}: {e}")

    # Method 2: Market sell
    try:
        order = place_sell_market(symbol, sell_qty)
        if order:
            filled = _wait_order_filled(order.id, timeout=30)
            if filled:
                log(f"FORCE SELL (market): {symbol} {sell_qty} shares")
                return True
    except Exception as e:
        log(f"market sell failed for {symbol}: {e}")

    # Method 3: Cancel all, wait longer, retry market sell
    try:
        cancel_all_orders()
        time.sleep(3)
        # Re-check actual position
        try:
            alpaca_pos = trading_client.get_open_position(symbol)
            sell_qty = int(float(alpaca_pos.qty))
        except Exception:
            pass
        if sell_qty <= 0:
            return False
        order = place_sell_market(symbol, sell_qty)
        if order:
            filled = _wait_order_filled(order.id, timeout=30)
            if filled:
                log(f"FORCE SELL (retry): {symbol} {sell_qty} shares")
                return True
    except Exception as e:
        log(f"All sell methods failed for {symbol}: {e}")

    return False


def check_order_filled(order_id) -> bool:
    try:
        order = trading_client.get_order_by_id(order_id)
        return order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)
    except Exception as e:
        log(f"check_order_filled error for {order_id}: {e}")
        return False


def get_order_filled_qty(order_id) -> int:
    """Return the number of shares actually filled for an order."""
    try:
        order = trading_client.get_order_by_id(order_id)
        if order.filled_qty:
            return int(float(order.filled_qty))
        return 0
    except Exception:
        return 0


def check_order_canceled(order_id) -> bool:
    try:
        order = trading_client.get_order_by_id(order_id)
        return order.status in (OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED)
    except Exception:
        return False


def _wait_order_filled(order_id, timeout=30) -> bool:
    deadline = dt.datetime.now() + dt.timedelta(seconds=timeout)
    while dt.datetime.now() < deadline:
        if check_order_filled(order_id):
            return True
        time.sleep(2)
    return False


def _force_close_remaining(positions: list[LivePosition]):
    try:
        alpaca_positions = trading_client.get_all_positions()
        if not alpaca_positions:
            for pos in positions:
                pos.remaining_shares = 0
            return

        held = {p.symbol: int(p.qty) for p in alpaca_positions}
        for pos in positions:
            if pos.symbol in held:
                log(f"FORCE CLOSE: {pos.symbol} still has {held[pos.symbol]} shares on Alpaca")
                sold = force_sell_position(pos.symbol, held[pos.symbol])
                if sold:
                    log(f"FORCE CLOSE SUCCESS: {pos.symbol}")
                else:
                    log(f"FORCE CLOSE FAILED: {pos.symbol}")
                pos.remaining_shares = 0
    except Exception as e:
        log(f"Force close check error: {e}")


# ── Protective order management ────────────────────────────────────

def place_protective_stop(pos: LivePosition) -> str | None:
    limit_price = round(pos.stop_price * (1 - STOP_LIMIT_BUFFER), 2)
    order = place_stop_limit_sell(pos.symbol, pos.remaining_shares, pos.stop_price, limit_price)
    if order:
        pos.protective_order_id = str(order.id)
        return str(order.id)
    return None


def replace_with_trailing_stop(pos: LivePosition, trail_pct: float) -> str | None:
    if pos.protective_order_id:
        cancel_order(pos.protective_order_id)
        pos.protective_order_id = None
    order = place_trailing_stop_sell(pos.symbol, pos.remaining_shares, trail_pct * 100)
    if order:
        pos.protective_order_id = str(order.id)
        return str(order.id)
    log(f"Trailing stop failed for {pos.symbol}, falling back to stop-limit")
    return place_protective_stop(pos)


def replace_stop_for_remaining(pos: LivePosition) -> str | None:
    if pos.protective_order_id:
        cancel_order(pos.protective_order_id)
        pos.protective_order_id = None

    if pos.remaining_shares <= 0:
        return None

    # 0.4.17: Use any(reached_list) instead of pos.reached_75
    if pos.reached_list and any(pos.reached_list):
        if pos.trade_type in ("first", "recovered"):
            trail_pct = get_trailing_pct(pos)
            return replace_with_trailing_stop(pos, trail_pct)
        elif pos.trade_type == "reentry":
            return replace_with_trailing_stop(pos, config.REENTRY_TRAILING_PCT_2)

    return place_protective_stop(pos)


# ── Entry detection ────────────────────────────────────────────────
def check_entry(symbol, open_price, accumulator):
    bars = accumulator.get_5min_bars(symbol)
    if len(bars) < 2:
        return 0, False
    pullback_idx = -1
    pullback_price = 0.0
    for i in range(len(bars)):
        if bars[i]["low"] < open_price:
            pullback_idx = i
            pullback_price = bars[i]["low"]
            break
    if pullback_idx < 0:
        return 0, False
    if not config.ENTRY_CONFIRMATION:
        return pullback_price, True
    if pullback_idx + 1 >= len(bars):
        return 0, False
    # Running minimum: keep updating pullback while price goes lower,
    # confirm when a subsequent bar's low is higher (bottom confirmed)
    for i in range(pullback_idx + 1, len(bars)):
        bar_low = bars[i]["low"]
        if bar_low < open_price and bar_low < pullback_price:
            pullback_idx = i
            pullback_price = bar_low
        elif bar_low >= pullback_price:
            return pullback_price, True
    return pullback_price, True


# ── Account equity ──────────────────────────────────────────────────
def _get_account_equity() -> float:
    try:
        acct = trading_client.get_account()
        eq = float(acct.equity)
        log(f"Account equity: ${eq:.2f}")
        return max(eq, config.MIN_POSITION_SIZE)
    except Exception as e:
        log(f"get_account_equity error: {e}, using INITIAL_CAPITAL")
        return config.INITIAL_CAPITAL


# ── Test connectivity ──────────────────────────────────────────────
def test_connectivity():
    log("Testing data connectivity...")
    try:
        snaps = get_snapshots(["SPY", "AAPL"])
        for sym, snap in snaps.items():
            if snap.daily_bar:
                log(f"  {sym} daily_bar: O={snap.daily_bar.open} H={snap.daily_bar.high} "
                    f"L={snap.daily_bar.low} C={snap.daily_bar.close}")
            if snap.minute_bar:
                log(f"  {sym} minute_bar: {snap.minute_bar.timestamp} "
                    f"O={snap.minute_bar.open} H={snap.minute_bar.high} "
                    f"L={snap.minute_bar.low} C={snap.minute_bar.close}")
            if snap.latest_trade:
                log(f"  {sym} latest_trade: ${snap.latest_trade.price}")
        log("Connectivity OK!")
        return True
    except Exception as e:
        log(f"Connectivity FAILED: {e}")
        return False


# ── Market calendar ────────────────────────────────────────────────
from market_calendar import (
    is_trading_day, get_trading_day_info, get_next_trading_day,
    calc_force_close_time, get_market_datetime,
)


# ── Daily report ──────────────────────────────────────────────────
def generate_daily_report(date_str, version, equity_start, equity_end,
                          daily_trades, trades_detail, candidates,
                          events_log):
    """Save structured daily report and print summary."""
    os.makedirs(_REPORT_DIR, exist_ok=True)

    wins = [t for t in trades_detail if t.get("pnl", 0) > 0]
    win_rate = len(wins) / len(trades_detail) if trades_detail else 0
    daily_pnl = equity_end - equity_start

    report = {
        "date": date_str,
        "version": version,
        "account_equity_start": round(equity_start, 2),
        "account_equity_end": round(equity_end, 2),
        "daily_pnl": round(daily_pnl, 2),
        "daily_trades": daily_trades,
        "win_trades": len(wins),
        "win_rate": round(win_rate, 3),
        "candidates": [
            {"symbol": c["symbol"], "gap_pct": round(c["gap_pct"], 4),
             "open_price": c["open_price"]}
            for c in (candidates or [])
        ],
        "trades": trades_detail,
        "events": events_log[-100:],
    }

    path = os.path.join(_REPORT_DIR, f"{date_str}.json")
    try:
        with open(path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        log(f"Daily report saved: {path}")
    except Exception as e:
        log(f"Failed to save report: {e}")

    # Print readable summary
    log("")
    log("=" * 50)
    log("         DAILY REPORT")
    log("=" * 50)
    log(f"Date: {date_str} | Version: {version}")
    log(f"Equity: ${equity_end:,.2f} | Daily P&L: ${daily_pnl:+,.2f}")
    log(f"Trades: {daily_trades} | Win rate: {win_rate:.1%}")
    log("-" * 50)
    for i, t in enumerate(trades_detail, 1):
        pnl_s = f"${t['pnl']:+,.2f}" if t.get("pnl") is not None else "N/A"
        log(f"#{i} {t.get('symbol','?'):6s} {t.get('type','?'):8s} "
            f"{t.get('shares',0)}sh  "
            f"${t.get('entry',0):.2f}->${t.get('exit',0):.2f}  "
            f"{t.get('exit_reason','?'):20s} {pnl_s}")
    log("=" * 50)

    return report


# ── Main scheduler ────────────────────────────────────────────────
def run_live():
    log("=" * 60)
    log("Stone 0.4.17 Live Paper Trading -- Auto Scheduler")
    equity = _get_account_equity()
    log(f"Capital: ${equity:,.2f} | Max daily trades: {config.MAX_DAILY_TRADES}")
    log(f"Entry buffer: +{ENTRY_LIMIT_BUFFER:.1%} | Stop-limit buffer: -{STOP_LIMIT_BUFFER:.1%}")
    log(f"Target buffer: -{TARGET_LIMIT_BUFFER:.1%} | Force-close timeout: {FORCE_CLOSE_LIMIT_TIMEOUT}s")
    log(f"Re-entry cutoff: {REENTRY_CUTOFF} EST | Leveraged ETF filter: ON")
    log(f"6-tier targets with list-based fields | calc_targets() | get_trailing_pct()")
    log(f"WebSocket: {'ON' if getattr(config, 'USE_WEBSOCKET', False) else 'OFF'} | "
        f"Data feed: {'SIP' if DATA_FEED == DataFeed.SIP else 'IEX'}")
    log(f"0.4.17: 6-tier targets, position recovery, SIP data feed, WebSocket streaming")
    log("=" * 60)

    if not test_connectivity():
        log("Data connectivity failed. Cannot trade.")
        return

    # Main scheduling loop -- runs forever
    while True:
        now_est = dt.datetime.now(tz=ZoneInfo("America/New_York"))
        today = now_est.date()

        # Check if today is a trading day
        today_info = get_trading_day_info(trading_client, today)

        if not today_info:
            next_day = get_next_trading_day(trading_client, today)
            next_date = dt.date.fromisoformat(next_day["date"])
            open_h, open_m = int(next_day["open"][:2]), int(next_day["open"][3:5])
            target = dt.datetime(next_date.year, next_date.month, next_date.day,
                                 open_h, open_m, tzinfo=ZoneInfo("America/New_York")) \
                     - dt.timedelta(minutes=10)
            log(f"Today ({today}) is NOT a trading day. "
                f"Next trading day: {next_day['date']} (open {next_day['open']} EST)")
            smart_sleep_until(target)
            continue

        # Today is a trading day -- get close time and force close
        close_str = today_info["close"]
        force_close_str = calc_force_close_time(close_str)
        close_h, close_m = int(close_str[:2]), int(close_str[3:5])
        fc_h, fc_m = int(force_close_str[:2]), int(force_close_str[3:5])
        open_h, open_m = int(today_info["open"][:2]), int(today_info["open"][3:5])

        force_close_time = dt.time(fc_h, fc_m)
        open_time = dt.time(open_h, open_m)

        # Compare full datetime, not just time -- avoids late-night false positive
        force_close_dt = dt.datetime(today.year, today.month, today.day,
                                     fc_h, fc_m, tzinfo=ZoneInfo("America/New_York"))
        if now_est >= force_close_dt:
            next_day = get_next_trading_day(trading_client, today + dt.timedelta(days=1))
            next_date = dt.date.fromisoformat(next_day["date"])
            n_open_h, n_open_m = int(next_day["open"][:2]), int(next_day["open"][3:5])
            target = dt.datetime(next_date.year, next_date.month, next_date.day,
                                 n_open_h, n_open_m, tzinfo=ZoneInfo("America/New_York")) \
                     - dt.timedelta(minutes=10)
            log(f"Market already closed for today. Next trading day: {next_day['date']}")
            smart_sleep_until(target)
            continue

        # Pre-open at 9:20 EST (10 min before 9:30 open)
        pre_open_dt = dt.datetime(today.year, today.month, today.day,
                                  open_h, open_m, tzinfo=ZoneInfo("America/New_York")) \
                      - dt.timedelta(minutes=10)
        if now_est < pre_open_dt:
            log(f"Market opens at {today_info['open']} EST. Waiting for pre-open (9:20)...")
            smart_sleep_until(pre_open_dt)

        # Run the trading day
        today_str = str(today)
        log(f"Starting trading day: {today_str} (close {close_str} EST, force_close {force_close_str})")
        if today_info["is_early_close"]:
            log(f"WARNING: Early close today at {close_str} EST!")

        # Get start equity
        equity_start = 0
        try:
            acct = trading_client.get_account()
            equity_start = float(acct.equity)
        except Exception:
            pass

        result = run_trading_day(force_close_time, force_close_str, today_info)

        # Get end equity
        equity_end = equity_start
        try:
            acct = trading_client.get_account()
            equity_end = float(acct.equity)
        except Exception:
            pass

        # Generate daily report
        generate_daily_report(
            date_str=today_str,
            version="0.4.17",
            equity_start=equity_start,
            equity_end=equity_end,
            daily_trades=result["daily_trades"],
            trades_detail=result["trades_detail"],
            candidates=result["candidates"],
            events_log=result["events_log"],
        )

        # Wait for next trading day (wake at 9:20 EST for pre-market scan)
        next_day = get_next_trading_day(trading_client, today + dt.timedelta(days=1))
        next_date = dt.date.fromisoformat(next_day["date"])
        n_open_h, n_open_m = int(next_day["open"][:2]), int(next_day["open"][3:5])
        target = dt.datetime(next_date.year, next_date.month, next_date.day,
                             n_open_h, n_open_m, tzinfo=ZoneInfo("America/New_York")) \
                 - dt.timedelta(minutes=10)
        log(f"Next trading day: {next_day['date']}. Sleeping until pre-open (9:20)...")
        smart_sleep_until(target)


def run_trading_day(force_close_time: dt.time, force_close_str: str,
                    today_info: dict) -> dict:
    """Execute one trading day. Returns result dict for daily report."""

    capital = _get_account_equity()

    positions: list[LivePosition] = []
    daily_trades = 0
    daily_stopped = False
    candidates = []
    entry_checked = set()
    reentry_checked = set()
    accumulator = BarAccumulator()
    day_highs = {}
    poll_count = 0
    events_log = []
    pending_buys = {}
    pending_sells = {}  # {order_id: {"symbol": str, "shares": int, "tier_idx": int|None}}
    chart_events = {}  # {symbol: [{ts, type, price, label}, ...]}
    trades_detail = []

    def add_chart_event(symbol, etype, price, label):
        if symbol not in chart_events:
            chart_events[symbol] = []
        chart_events[symbol].append({
            "ts": dt.datetime.now(tz=ZoneInfo("America/New_York")).strftime("%H:%M"),
            "type": etype,  # "buy" or "sell"
            "price": round(price, 4),
            "label": label,
        })

    # ── Pre-market scan gaps (BEFORE 9:30 to have candidates ready at open) ──
    log("Pre-market scanning for gap stocks...")
    candidates = scan_gaps()
    if not candidates:
        log("No gap stocks found in pre-market scan. Waiting for market open to re-scan...")
        # Wait for market open and try again
        while True:
            now = dt.datetime.now(tz=ZoneInfo("America/New_York"))
            if now.time() >= force_close_time:
                log("Market already closed, skipping trading day.")
                return {"daily_trades": 0, "trades_detail": [], "candidates": [], "events_log": events_log}
            if (now.hour == 9 and now.minute >= 30) or now.hour >= 10:
                break
            log(f"Waiting for market open ({today_info['open']} EST)...")
            time.sleep(30)
        log("Re-scanning at market open...")
        candidates = scan_gaps()
        if not candidates:
            log("No gap stocks found today.")
            return {"daily_trades": 0, "trades_detail": [], "candidates": [], "events_log": events_log}
    else:
        log(f"Pre-market found {len(candidates)} gap stocks")
        # Wait for market open
        while True:
            now = dt.datetime.now(tz=ZoneInfo("America/New_York"))
            if now.time() >= force_close_time:
                log("Market already closed, skipping trading day.")
                return {"daily_trades": 0, "trades_detail": [], "candidates": [], "events_log": events_log}
            if (now.hour == 9 and now.minute >= 30) or now.hour >= 10:
                break
            log(f"Waiting for market open ({today_info['open']} EST)... candidates ready.")
            time.sleep(30)

    # ── Refresh candidate open prices at market open ──
    if candidates:
        candidates = refresh_candidates(candidates)
    if not candidates:
        log("No gap stocks after price refresh.")
        return {"daily_trades": 0, "trades_detail": [], "candidates": [], "events_log": events_log}

    n_candidates = len(candidates)
    max_stocks = min(config.MAX_POSITIONS_PER_DAY, n_candidates)
    pos_per_stock = capital / max_stocks if max_stocks > 0 else capital
    pos_per_stock = min(pos_per_stock, config.MAX_POSITION_SIZE)
    candidates = candidates[:max_stocks]

    log(f"Candidates: {[c['symbol'] for c in candidates]}")
    for c in candidates:
        log(f"  {c['symbol']}: gap +{c['gap_pct']:.1%}, open=${c['open_price']:.4f}")
        day_highs[c['symbol']] = c['open_price']

    # ── Backfill historical 5-min bars for candidates ──
    now_est = dt.datetime.now(tz=ZoneInfo("America/New_York"))
    if candidates:
        today_open = now_est.replace(hour=9, minute=30, second=0, microsecond=0)
        try:
            req = StockBarsRequest(
                symbol_or_symbols=[c['symbol'] for c in candidates],
                timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                start=today_open, end=now_est,
                feed=DATA_FEED,
            )
            hist_bars = data_client.get_stock_bars(req)
            if not hist_bars.df.empty:
                df = hist_bars.df
                for c in candidates:
                    sym = c['symbol']
                    if isinstance(df.index[0], tuple):
                        sym_df = df[df.index.get_level_values("symbol") == sym]
                    else:
                        sym_df = df
                    for _, row in sym_df.iterrows():
                        b = _Bar()
                        b.timestamp = row.name if not isinstance(row.name, tuple) else row.name[1]
                        if hasattr(b.timestamp, "to_pydatetime"):
                            b.timestamp = b.timestamp.to_pydatetime()
                        b.open = row["open"]; b.high = row["high"]
                        b.low = row["low"]; b.close = row["close"]
                        b.volume = int(row["volume"])
                        accumulator.add_bar(sym, b)
                log(f"Backfilled bars: {dict((c['symbol'], accumulator.bar_count(c['symbol'])) for c in candidates)}")
        except Exception as e:
            log(f"Backfill error: {e}")

    # ── Recover existing Alpaca positions ──
    try:
        alpaca_positions = trading_client.get_all_positions()
        alpaca_orders = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
        for ap in alpaca_positions:
            sym = ap.symbol
            qty = int(float(ap.qty))
            avg_entry = float(ap.avg_entry_price)
            cur_price = float(ap.current_price)
            # Skip if already tracked
            if sym in [p.symbol for p in positions]:
                continue
            log(f"RECOVER: Found orphan position {sym} | {qty} shares @ ${avg_entry:.4f} (current ${cur_price:.4f})")

            # Find matching candidate for open_price
            cand = next((c for c in candidates if c["symbol"] == sym), None)
            open_price = cand["open_price"] if cand else avg_entry
            prev_close = cand["prev_close"] if cand else avg_entry

            # Find existing protective (SELL) order
            prot_order_id = None
            stop_price = avg_entry * 0.95  # default 5% stop
            for ao in alpaca_orders:
                if ao.symbol == sym and ao.side == OrderSide.SELL:
                    prot_order_id = str(ao.id)
                    if ao.stop_price:
                        stop_price = float(ao.stop_price)
                    log(f"RECOVER: Found protective order {ao.id} stop=${stop_price:.4f}")
                    break

            # 0.4.17: Calculate 6-tier targets for recovered position
            targets, sell_ratios, trail_pcts, target_mode = calc_targets(avg_entry, open_price)

            # Reconstruct sold_shares_list from today's Alpaca order history
            # We can't know which tier each sell belonged to, but we can count
            # total shares sold and mark the lowest tiers as sold (conservative)
            sold_shares_list = [0] * len(targets)
            total_sold_today = 0
            try:
                today_start = now_est.replace(hour=0, minute=0, second=0, microsecond=0)
                hist_orders = trading_client.get_orders(GetOrdersRequest(
                    status=QueryOrderStatus.CLOSED,
                    after=today_start,
                    direction="asc",
                ))
                for ho in hist_orders:
                    if ho.symbol == sym and ho.side == OrderSide.SELL and ho.filled_qty:
                        total_sold_today += int(float(ho.filled_qty))
            except Exception as e:
                log(f"RECOVER: Order history lookup failed for {sym}: {e}")

            # Distribute sold shares across tiers (fill lowest tiers first)
            remaining_sold = total_sold_today
            for ti in range(len(targets)):
                tier_sell = max(1, int(qty * sell_ratios[ti])) if sell_ratios else 0
                if remaining_sold <= 0:
                    break
                actual = min(tier_sell, remaining_sold)
                sold_shares_list[ti] = actual
                remaining_sold -= actual

            # reached_list: mark tiers as reached only if shares were sold at that tier
            reached = [sold_shares_list[ti] > 0 for ti in range(len(targets))]

            # If no history found, mark all as unreached (safest — won't trigger premature exits)
            if total_sold_today == 0:
                reached = [False] * len(targets)
                sold_shares_list = [0] * len(targets)

            highest_seen = max(cur_price, avg_entry)

            pos = LivePosition(
                symbol=sym, entry_price=avg_entry, shares=qty,
                stop_price=stop_price,
                open_price=open_price,
                trade_type="recovered",
                highest=highest_seen, prev_high=avg_entry,
                entry_time=now_est, protective_order_id=prot_order_id,
                atr=0.0,
                targets=targets, sell_ratios=sell_ratios,
                trail_pcts=trail_pcts,
                reached_list=reached,
                sold_shares_list=sold_shares_list,
                target_mode=target_mode,
            )
            positions.append(pos)
            # Place protective stop if none exists
            if not prot_order_id:
                place_protective_stop(pos)
                log(f"RECOVER: Placed protective stop for {sym} @ ${stop_price:.4f}")
            entry_checked.add(sym)
            daily_trades += 1
            events_log.append(f"{now_est.strftime('%H:%M:%S')} RECOVERED {sym} @ ${avg_entry:.4f} ({qty}sh, stop=${stop_price:.4f}, mode={target_mode})")
            log(f"RECOVER: {sym} restored -- stop=${stop_price:.4f}, targets={[round(t, 2) for t in targets]}, mode={target_mode}")
    except Exception as e:
        log(f"Position recovery error: {e}")

    # ── Start WebSocket stream ──
    global _stream_state
    stream_symbols = list(set(
        [c['symbol'] for c in candidates] +
        [p.symbol for p in positions]
    ))
    _stream_state = StreamState(
        accumulator=accumulator,
        positions_ref_fn=lambda: positions,
        candidates_ref_fn=lambda: candidates,
    )
    _stream_state.start(stream_symbols)

    # ── Main loop ──
    cutoff_time = dt.time(10, 0)
    reentry_cutoff_time = dt.time(int(REENTRY_CUTOFF[:2]), int(REENTRY_CUTOFF[3:]))
    force_close_started = {}

    def record_trade(pos, exit_price, exit_reason):
        nonlocal daily_trades
        daily_trades += 1
        orig = getattr(pos, 'original_shares', pos.remaining_shares)
        pnl = (exit_price - pos.entry_price) * orig
        trades_detail.append({
            "symbol": pos.symbol,
            "type": pos.trade_type,
            "entry": round(pos.entry_price, 4),
            "exit": round(exit_price, 4),
            "shares": orig,
            "exit_reason": exit_reason,
            "pnl": round(pnl, 2),
        })
        add_chart_event(pos.symbol, "sell", exit_price,
                        f"{exit_reason.replace('_', ' ').upper()} {orig}sh")

    while True:
        now_est = dt.datetime.now(tz=ZoneInfo("America/New_York"))
        now_time = now_est.time()
        poll_count += 1

        # ── Check pending buy fills ──
        for symbol in list(pending_buys.keys()):
            order_id, pos_data = pending_buys[symbol]
            if check_order_filled(order_id):
                filled_qty = get_order_filled_qty(order_id)
                # If partial fill, adjust shares to actual filled amount
                if filled_qty > 0 and filled_qty != pos_data.get("shares", filled_qty):
                    log(f"BUY PARTIAL FILL: {symbol} {filled_qty}/{pos_data.get('shares', '?')} shares")
                    pos_data["shares"] = filled_qty
                log(f"BUY FILLED: {symbol} order {order_id} ({filled_qty}sh)")
                pos = LivePosition(**pos_data)
                positions.append(pos)
                # Cancel bracket order's auto-created stop_loss/take_profit legs
                # to prevent double stop orders (we place our own below)
                try:
                    open_orders = trading_client.get_orders(filter={"status": "open", "symbols": symbol})
                    for o in open_orders:
                        if o.side == OrderSide.SELL and str(o.id) != order_id:
                            cancel_order(str(o.id))
                            log(f"CANCELLED bracket leg {o.id} for {symbol}")
                except Exception:
                    pass
                place_protective_stop(pos)
                events_log.append(f"{now_est.strftime('%H:%M:%S')} BUY FILLED {symbol} @ ${pos.entry_price:.4f}")
                add_chart_event(symbol, "buy", pos.entry_price,
                                f"BUY {pos.shares}sh" if pos.trade_type != "reentry" else f"RE-ENTRY BUY {pos.shares}sh")
                del pending_buys[symbol]
                # Update WebSocket subscriptions to include new position symbol
                if _stream_state:
                    _stream_state.update_symbols([symbol])
            elif check_order_canceled(order_id):
                log(f"BUY CANCELED: {symbol} order {order_id}")
                events_log.append(f"{now_est.strftime('%H:%M:%S')} BUY CANCELED {symbol}")
                del pending_buys[symbol]

        # ── Check pending sell (target tier) fills ──
        for order_id in list(pending_sells.keys()):
            info = pending_sells[order_id]
            symbol = info["symbol"]
            sell_shares = info["shares"]
            tier_idx = info.get("tier_idx")
            if check_order_filled(order_id):
                actual_filled = get_order_filled_qty(order_id)
                # Handle partial fills: if fewer shares filled than expected,
                # adjust remaining_shares to account for the difference
                if actual_filled > 0 and actual_filled < sell_shares:
                    shortfall = sell_shares - actual_filled
                    log(f"SELL LIMIT PARTIAL FILL: {symbol} {actual_filled}/{sell_shares}sh, adding {shortfall} back to remaining_shares")
                    pos = next((p for p in positions if p.symbol == symbol), None)
                    if pos:
                        pos.remaining_shares += shortfall
                log(f"SELL LIMIT FILLED: {symbol} {actual_filled}sh (T{tier_idx+1 if tier_idx is not None else '?'}) order {order_id}")
                del pending_sells[order_id]
                # Now that the sell is confirmed, replace protective stop for actual remaining shares
                pos = next((p for p in positions if p.symbol == symbol), None)
                if pos and pos.remaining_shares > 0:
                    replace_stop_for_remaining(pos)
            elif check_order_canceled(order_id):
                log(f"SELL LIMIT CANCELED: {symbol} order {order_id}, rolling back sold_shares_list")
                # Roll back the sold_shares_list AND reached_list entries since sell didn't execute
                pos = next((p for p in positions if p.symbol == symbol), None)
                affected_tiers = info.get("affected_tiers")
                if pos and affected_tiers:
                    for t in affected_tiers:
                        if t < len(pos.sold_shares_list):
                            pos.sold_shares_list[t] = 0
                        if t < len(pos.reached_list):
                            pos.reached_list[t] = False
                elif pos and tier_idx is not None and tier_idx < len(pos.sold_shares_list):
                    # Fallback for entries without affected_tiers (backward compat)
                    pos.sold_shares_list[tier_idx] = 0
                    if tier_idx < len(pos.reached_list):
                        pos.reached_list[tier_idx] = False
                if pos:
                    pos.remaining_shares += sell_shares
                    # For re-entry sells (tier_idx is None), also reset re-entry state
                    if tier_idx is None and pos.trade_type == "reentry":
                        pos.reached_target1 = False
                        pos.breakeven_active = False
                        pos.sold_partial1_shares = 0
                del pending_sells[order_id]

        # ── Check protective order fills ──
        for pos in positions[:]:
            if pos.remaining_shares <= 0:
                continue
            if pos.protective_order_id and check_order_filled(pos.protective_order_id):
                log(f"PROTECTIVE ORDER FILLED: {pos.symbol} order {pos.protective_order_id}")
                events_log.append(f"{now_est.strftime('%H:%M:%S')} PROTECTIVE FILLED {pos.symbol}")
                # Cancel any pending target sells for this symbol
                for oid in list(pending_sells.keys()):
                    if pending_sells[oid]["symbol"] == pos.symbol:
                        cancel_order(oid)
                        del pending_sells[oid]
                pos.remaining_shares = 0
                pos.protective_order_id = None
                record_trade(pos, pos.stop_price, "protective_stop")
                positions.remove(pos)
                continue

        # ── Force close ──
        if now_time >= force_close_time:
            log("Force close time reached!")
            cancel_all_orders()
            for symbol in list(force_close_started.keys()):
                del force_close_started[symbol]
            for pos in positions:
                if pos.remaining_shares > 0:
                    snap = get_snapshots([pos.symbol]).get(pos.symbol)
                    bid_price = float(snap.latest_trade.price) if snap and snap.latest_trade else 0
                    if bid_price > 0:
                        limit_price = round(bid_price * 0.99, 2)
                        place_sell_limit(pos.symbol, pos.remaining_shares, limit_price)
                        force_close_started[pos.symbol] = now_est
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} FORCE CLOSE LIMIT {pos.symbol} {pos.remaining_shares} @ ${limit_price:.2f}")
                        record_trade(pos, limit_price, "force_close")
                    else:
                        place_sell_market(pos.symbol, pos.remaining_shares)
                        record_trade(pos, bid_price or pos.entry_price, "force_close")
                        pos.remaining_shares = 0
            if force_close_started:
                _wait_force_close(force_close_started, positions)
            # Close any positions that still weren't sold (no duplicate — only unsold ones)
            try:
                remaining_alpaca = trading_client.get_all_positions()
                for ap in remaining_alpaca:
                    sym = ap.symbol
                    # Only close if we don't already have a pending sell for this symbol
                    if sym not in force_close_started:
                        log(f"EOD CLOSE (no prior sell): selling {ap.qty} {sym}")
                        trading_client.close_position(sym)
            except Exception as e:
                log(f"Final close positions error: {e}")
            # Stop WebSocket stream
            if _stream_state:
                _stream_state.stop()
            break

        # ── Collect snapshot data ──
        # 0.4.17: Include position symbols in stream
        stream_symbols = list(set(
            [c['symbol'] for c in candidates] +
            [p.symbol for p in positions]
        ))
        all_symbols = list(set(
            stream_symbols +
            list(pending_buys.keys())
        ))
        if not all_symbols:
            time.sleep(30)
            continue

        try:
            snaps = get_snapshots(all_symbols)
        except Exception as e:
            log(f"Snapshot error: {e}")
            time.sleep(30)
            continue

        # ── Accumulate minute bars ──
        for symbol in all_symbols:
            snap = snaps.get(symbol)
            if snap and snap.minute_bar:
                accumulator.add_bar(symbol, snap.minute_bar)

        # ── Track day highs ──
        for symbol in all_symbols:
            snap = snaps.get(symbol)
            if snap and snap.daily_bar:
                h = float(snap.daily_bar.high)
                day_highs[symbol] = max(day_highs.get(symbol, 0), h)

        # ── Pullback stop (15% from day high) -- per-stock, only HELD positions ──
        # Only sells the stock that triggered the stop, other positions continue
        for pos in positions[:]:
            if pos.remaining_shares <= 0:
                continue
            if pos.trade_type in ("recovered", "reentry"):
                continue  # Skip pullback stop for recovered/reentry positions
            symbol = pos.symbol
            snap = snaps.get(symbol)
            if not snap or not snap.daily_bar:
                continue
            dh = day_highs.get(symbol, 0)
            dl = float(snap.daily_bar.low)
            if dh > 0 and (dh - dl) / dh > config.PULLBACK_STOP_THRESHOLD:
                log(f"PULLBACK STOP: {symbol} dropped {(dh - dl) / dh:.1%} from high ${dh:.4f}")
                events_log.append(f"{now_est.strftime('%H:%M:%S')} PULLBACK STOP {symbol} -{(dh - dl) / dh:.1%}")
                sold = force_sell_position(symbol, pos.remaining_shares)
                if sold:
                    log(f"PULLBACK STOP FILLED: {symbol} {pos.remaining_shares} shares")
                record_trade(pos, dl, "pullback_stop")
                pos.remaining_shares = 0
                pos.protective_order_id = None
                # Remove this stock from candidates to prevent re-entry
                entry_checked.add(symbol)

        # Clean up zero-share positions
        positions = [p for p in positions if p.remaining_shares > 0]

        # ── Daily loss circuit breaker (separate from pullback stop) ──
        if daily_stopped:
            _force_close_remaining(positions)
            positions = [p for p in positions if p.remaining_shares > 0]
            save_state(positions, candidates, daily_trades, daily_stopped,
                       entry_checked, day_highs, accumulator, events_log)
            save_chart_data(accumulator, positions, chart_events, str(now_est.date()))
            time.sleep(30)
            continue

        # ── Check exits for held positions ──
        for pos in positions[:]:
            if pos.remaining_shares <= 0:
                continue

            snap = snaps.get(pos.symbol)
            if not snap or not snap.latest_trade:
                continue

            cur_price = float(snap.latest_trade.price)

            if cur_price > pos.highest:
                pos.highest = cur_price

            # ── Stop loss (polled fallback) ──
            if cur_price <= pos.stop_price:
                log(f"STOP LOSS (polled): {pos.symbol} @ ${pos.stop_price:.4f} (cur=${cur_price:.4f})")
                events_log.append(f"{now_est.strftime('%H:%M:%S')} STOP LOSS {pos.symbol} @ ${pos.stop_price:.4f}")
                if pos.protective_order_id:
                    cancel_order(pos.protective_order_id)
                    pos.protective_order_id = None
                sold = force_sell_position(pos.symbol, pos.remaining_shares)
                if not sold:
                    log(f"STOP LOSS FORCE SELL FAILED: {pos.symbol}")
                pos.remaining_shares = 0
                record_trade(pos, pos.stop_price, "stop_loss")
                positions.remove(pos)
                continue

            # ── First trade / recovered: 6-tier profit targets with skip-gap ──
            if pos.trade_type in ("first", "recovered"):
                need_replace_protective = False

                # 0.4.11: Time limit -- if no target hit in 40 min, sell at breakeven+
                # Only applies to first trade, NOT recovered positions
                if pos.trade_type == "first":
                    pos.bar_count += 1
                    time_limit = getattr(config, "FIRST_TRADE_TIME_LIMIT_BARS", 0)
                    if time_limit > 0 and not (pos.reached_list and pos.reached_list[0]) and pos.bar_count >= time_limit:
                        pos.time_limit_active = True
                if pos.trade_type == "first" and pos.time_limit_active and cur_price >= pos.entry_price and pos.remaining_shares > 0:
                    log(f"TIME LIMIT EXIT: {pos.symbol} @ ${cur_price:.4f} (no target in {time_limit * 5}min)")
                    events_log.append(f"{now_est.strftime('%H:%M:%S')} TIME LIMIT EXIT {pos.symbol} @ ${cur_price:.4f}")
                    add_chart_event(pos.symbol, "sell", cur_price, f"TIME LIMIT {pos.remaining_shares}sh")
                    if pos.protective_order_id:
                        cancel_order(pos.protective_order_id)
                        pos.protective_order_id = None
                    sold = force_sell_position(pos.symbol, pos.remaining_shares)
                    pos.remaining_shares = 0
                    record_trade(pos, cur_price, "time_limit_exit")
                    positions.remove(pos)
                    continue

                # 0.4.17: 6-tier exit logic with skip-gap
                # Check from highest to lowest tier; skip already-reached tiers
                n_tiers = len(pos.targets)
                for ti in range(n_tiers - 1, -1, -1):
                    if ti >= len(pos.reached_list) or ti >= len(pos.sell_ratios):
                        continue
                    if pos.reached_list[ti]:
                        continue  # already processed this tier
                    if pos.highest < pos.targets[ti]:
                        continue  # haven't reached this target yet

                    # Mark this tier and all lower tiers as reached (skip-gap)
                    for lower_ti in range(ti + 1):
                        if lower_ti < len(pos.reached_list):
                            pos.reached_list[lower_ti] = True

                    # Calculate shares to sell for all newly reached tiers
                    total_sell = 0
                    for sell_ti in range(ti + 1):
                        if sell_ti >= len(pos.sell_ratios) or sell_ti >= len(pos.sold_shares_list):
                            continue
                        if pos.sold_shares_list[sell_ti] == 0:
                            n_sell = max(1, int(pos.shares * pos.sell_ratios[sell_ti]))
                            pos.sold_shares_list[sell_ti] = n_sell
                            total_sell += n_sell

                    if total_sell > pos.remaining_shares:
                        total_sell = pos.remaining_shares
                    if total_sell > 0:
                        retracements = getattr(config, "PROFIT_RETRACEMENT_TIERS", [0.25, 0.50, 0.75, 1.00, 1.25, 1.50])
                        tier_pct = f"{int(retracements[ti]*100)}%" if ti < len(retracements) else f"T{ti+1}"
                        sell_price = round(pos.targets[ti] * (1 - TARGET_LIMIT_BUFFER), 2)
                        log(f"{tier_pct} TARGET: {pos.symbol} selling {total_sell} @ ${sell_price:.4f}")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} {tier_pct} TARGET {pos.symbol} sell {total_sell} @ ${sell_price:.4f}")
                        add_chart_event(pos.symbol, "sell", sell_price, f"TARGET_{tier_pct} {total_sell}sh")
                        if pos.protective_order_id:
                            cancel_order(pos.protective_order_id)
                            pos.protective_order_id = None
                        order = place_sell_limit(pos.symbol, total_sell, sell_price)
                        if order:
                            # Store ALL affected tier indices for proper rollback on cancel
                            affected_tiers = [sell_ti for sell_ti in range(ti + 1)
                                              if sell_ti < len(pos.sold_shares_list)
                                              and pos.sold_shares_list[sell_ti] > 0]
                            pending_sells[str(order.id)] = {
                                "symbol": pos.symbol, "shares": total_sell, "tier_idx": ti,
                                "affected_tiers": affected_tiers
                            }
                            pos.remaining_shares -= total_sell
                        else:
                            # Sell order failed — roll back sold_shares_list AND reached_list
                            log(f"SELL LIMIT FAILED for {pos.symbol}, rolling back tier sells")
                            for sell_ti in range(ti + 1):
                                if sell_ti < len(pos.sold_shares_list):
                                    pos.sold_shares_list[sell_ti] = 0
                                if sell_ti < len(pos.reached_list):
                                    pos.reached_list[sell_ti] = False
                        need_replace_protective = True

                    break  # Only process the highest newly-reached tier per poll

                # Replace protective stop only if no pending sells for this symbol
                # (otherwise wait for sell fill confirmation to get correct qty)
                has_pending = any(info["symbol"] == pos.symbol for info in pending_sells.values())
                if need_replace_protective and pos.remaining_shares > 0 and not has_pending:
                    replace_stop_for_remaining(pos)

                # ── Trailing stop (polled fallback) ──
                # 0.4.17: Use get_trailing_pct for generic N-tier lookup
                if pos.reached_list and any(pos.reached_list) and pos.remaining_shares > 0:
                    pct = get_trailing_pct(pos)
                    tsp = round(pos.highest * (1 - pct), 2)
                    tsp = max(tsp, pos.entry_price)
                    if cur_price <= tsp:
                        # Find highest reached tier for label
                        tier_label = "trailing"
                        if pos.reached_list:
                            for tidx in range(len(pos.reached_list) - 1, -1, -1):
                                if pos.reached_list[tidx]:
                                    retracements = getattr(config, "PROFIT_RETRACEMENT_TIERS", [0.25, 0.50, 0.75, 1.00, 1.25, 1.50])
                                    tier_label = f"{int(retracements[tidx]*100)}%" if tidx < len(retracements) else f"T{tidx+1}"
                                    break
                        log(f"TRAILING STOP({tier_label}) (polled): {pos.symbol} @ ${tsp:.4f} (high=${pos.highest:.4f})")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} TRAILING STOP({tier_label}) {pos.symbol} @ ${tsp:.4f}")
                        if pos.protective_order_id:
                            cancel_order(pos.protective_order_id)
                            pos.protective_order_id = None
                        sold = force_sell_position(pos.symbol, pos.remaining_shares)
                        if not sold:
                            log(f"TRAILING STOP FORCE SELL FAILED: {pos.symbol}")
                        pos.remaining_shares = 0
                        record_trade(pos, cur_price, "trailing_stop")
                        positions.remove(pos)
                        continue

            # ── Re-entry v2 profit targets ──
            elif pos.trade_type == "reentry":
                need_replace_protective = False
                pos.reentry_bar_count += 1
                trail_pct_2 = getattr(config, "REENTRY_TRAILING_PCT_2", 0.03)

                # Tier-1: sell 1/2 at target_1
                if not pos.reached_target1 and pos.highest >= pos.reentry_target:
                    pos.reached_target1 = True
                    sell_ratio_1 = getattr(config, "REENTRY_SELL_RATIO_1", 0.5)
                    n = int(pos.remaining_shares * sell_ratio_1)
                    if n > 0:
                        sell_price = round(pos.reentry_target * (1 - TARGET_LIMIT_BUFFER), 2)
                        log(f"RE-ENTRY TIER-1: {pos.symbol} selling {n} @ ${sell_price:.4f}")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} RE-ENTRY TIER-1 {pos.symbol} sell {n} @ ${sell_price:.4f}")
                        add_chart_event(pos.symbol, "sell", sell_price, f"TIER-1 {n}sh")
                        if pos.protective_order_id:
                            cancel_order(pos.protective_order_id)
                            pos.protective_order_id = None
                        order = place_sell_limit(pos.symbol, n, sell_price)
                        if order:
                            pending_sells[str(order.id)] = {
                                "symbol": pos.symbol, "shares": n, "tier_idx": None
                            }
                            pos.sold_partial1_shares = n
                            pos.remaining_shares -= n
                            pos.breakeven_active = True
                            need_replace_protective = True
                        else:
                            # Sell failed — don't activate breakeven or decrement shares
                            pos.reached_target1 = False

                # Breakeven stop after tier-1
                if pos.breakeven_active and cur_price <= pos.entry_price and pos.remaining_shares > 0:
                    log(f"RE-ENTRY BREAKEVEN: {pos.symbol} @ ${pos.entry_price:.4f}")
                    events_log.append(f"{now_est.strftime('%H:%M:%S')} RE-ENTRY BREAKEVEN {pos.symbol}")
                    if pos.protective_order_id:
                        cancel_order(pos.protective_order_id)
                        pos.protective_order_id = None
                    sold = force_sell_position(pos.symbol, pos.remaining_shares)
                    pos.remaining_shares = 0
                    record_trade(pos, pos.entry_price, "reentry_breakeven")
                    positions.remove(pos)
                    continue

                if need_replace_protective and pos.remaining_shares > 0:
                    if pos.reached_target1:
                        replace_with_trailing_stop(pos, trail_pct_2)
                    else:
                        place_protective_stop(pos)

                # Trailing stop after tier-1
                if pos.reached_target1 and pos.remaining_shares > 0:
                    tsp = round(pos.highest * (1 - trail_pct_2), 2)
                    tsp = max(tsp, pos.entry_price)
                    if cur_price <= tsp:
                        log(f"RE-ENTRY TRAILING (polled): {pos.symbol} @ ${tsp:.4f} (high=${pos.highest:.4f})")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} RE-ENTRY TRAILING {pos.symbol} @ ${tsp:.4f}")
                        if pos.protective_order_id:
                            cancel_order(pos.protective_order_id)
                            pos.protective_order_id = None
                        sold = force_sell_position(pos.symbol, pos.remaining_shares)
                        pos.remaining_shares = 0
                        record_trade(pos, tsp, "reentry_trailing")
                        positions.remove(pos)
                        continue

        # ── Check entries for candidates ──
        if now_time >= cutoff_time and poll_count == 1:
            log(f"Entry window closed (10:00 AM). No entries will be placed.")
        if now_time < cutoff_time and daily_trades < config.MAX_DAILY_TRADES and not daily_stopped:
            force_qty = getattr(config, "FORCE_QTY", 0)
            for cand in candidates:
                symbol = cand["symbol"]
                if symbol in entry_checked or symbol in pending_buys:
                    continue
                if symbol in [p.symbol for p in positions]:
                    continue

                # 0.4.10: Skip leveraged ETFs
                if is_leveraged_etf(symbol):
                    log(f"  {symbol}: leveraged ETF, skipping entry")
                    entry_checked.add(symbol)
                    continue

                entry_price, confirmed = check_entry(symbol, cand["open_price"], accumulator)
                if not confirmed or entry_price <= 0:
                    continue

                # 0.4.11: Skip if entry price >= open price (no chasing above open)
                # In FORCE_QTY test mode, allow momentum entries to verify cap-only targets
                if getattr(config, "ENTRY_BELOW_OPEN", True) and entry_price >= cand["open_price"] and force_qty == 0:
                    log(f"  {symbol}: entry ${entry_price:.4f} >= open ${cand['open_price']:.4f}, skipping")
                    entry_checked.add(symbol)
                    continue

                bars_5m = accumulator.get_5min_bars(symbol)
                if len(bars_5m) >= 2:
                    atr = calc_atr(bars_5m, period=14)
                else:
                    atr = get_prev_day_atr(symbol)

                stop = calc_stop_price(entry_price, atr)

                # 0.4.17: 6-tier targets via calc_targets
                targets, sell_ratios, trail_pcts, target_mode = calc_targets(entry_price, cand["open_price"])

                pos_size = min(pos_per_stock, config.MAX_POSITION_SIZE)
                # Check actual buying power before placing order
                try:
                    bp = float(trading_client.get_account().buying_power)
                    if bp < pos_size:
                        log(f"  {symbol}: buying power ${bp:.2f} < alloc ${pos_size:.2f}, skipping")
                        entry_checked.add(symbol)
                        continue
                    pos_size = min(pos_size, bp * 0.95)
                except Exception:
                    pass
                shares = int(pos_size / entry_price)
                if force_qty > 0:
                    shares = force_qty
                if shares <= 0:
                    entry_checked.add(symbol)
                    continue

                limit_price = round(entry_price * (1 + ENTRY_LIMIT_BUFFER), 2)
                order = place_bracket_entry(symbol, shares, entry_price, stop)
                if order:
                    pos_data = {
                        "symbol": symbol, "entry_price": entry_price, "shares": shares,
                        "stop_price": stop, "open_price": cand["open_price"],
                        "entry_time": now_est, "atr": atr,
                        "targets": targets, "sell_ratios": sell_ratios,
                        "trail_pcts": trail_pcts, "target_mode": target_mode,
                        "reached_list": [False] * len(targets),
                        "sold_shares_list": [0] * len(targets),
                    }
                    pending_buys[symbol] = (str(order.id), pos_data)
                    entry_checked.add(symbol)
                    log(f"BUY PENDING {symbol}: entry=${entry_price:.4f}, limit=${limit_price:.4f}, "
                        f"stop=${stop:.4f}, targets={[round(t, 2) for t in targets]}, mode={target_mode}, shares={shares}")
                    events_log.append(f"{now_est.strftime('%H:%M:%S')} BUY PENDING {symbol} @ ${limit_price:.4f}")

        # ── Check re-entry ──
        # 0.4.13: Re-entry v2 with half position, ATR stop, tier targets, NO time stop
        if now_time < reentry_cutoff_time and daily_trades < config.MAX_DAILY_TRADES and not daily_stopped:
            exited_symbols = entry_checked - {p.symbol for p in positions} - set(pending_buys.keys())
            for symbol in exited_symbols:
                if symbol in reentry_checked:
                    continue
                cand = next((c for c in candidates if c['symbol'] == symbol), None)
                if not cand:
                    continue

                bars_5m = accumulator.get_5min_bars(symbol)
                if len(bars_5m) < 3:
                    continue

                entry_price, prev_high, _, confirmed = find_reentry_point(bars_5m, cand["open_price"])
                if not confirmed or entry_price <= 0:
                    continue

                # 0.4.14: Minimum pullback from peak for re-entry
                reentry_min_pb = getattr(config, "REENTRY_MIN_PULLBACK", 0)
                if reentry_min_pb > 0 and prev_high > 0:
                    pb_pct = (prev_high - entry_price) / prev_high
                    if pb_pct < reentry_min_pb:
                        log(f"RE-ENTRY SKIP {symbol}: pullback {pb_pct:.1%} < min {reentry_min_pb:.0%}")
                        reentry_checked.add(symbol)
                        continue

                # 0.4.14: Daily loss circuit breaker
                max_daily_loss_pct = getattr(config, "MAX_DAILY_LOSS_PCT", 0)
                if max_daily_loss_pct > 0:
                    start_equity = capital
                    current_loss = sum(p.realized_pnl for p in positions if hasattr(p, 'realized_pnl') and p.realized_pnl < 0)
                    if current_loss <= -(start_equity * max_daily_loss_pct):
                        log(f"Daily loss circuit breaker triggered (${current_loss:,.2f}), no more re-entries")
                        daily_stopped = True
                        break

                # 0.4.10: ATR-based stop for re-entry
                atr = calc_atr(bars_5m, period=14) if len(bars_5m) >= 2 else get_prev_day_atr(symbol)
                if atr > 0:
                    stop = round(entry_price - 1.5 * atr, 2)
                    stop = max(stop, round(entry_price * 0.96, 2))
                else:
                    stop = round(entry_price * (1 - config.REENTRY_STOP_PCT), 2)

                # 0.4.14: Cap re-entry stop loss at max percentage
                stop_max_pct = getattr(config, "STOP_LOSS_MAX_PCT", 0)
                if stop_max_pct > 0:
                    min_stop = round(entry_price * (1 - stop_max_pct), 2)
                    stop = max(stop, min_stop)

                # 0.4.10: Tier-1 target using retracement
                retrace_1 = getattr(config, "REENTRY_PROFIT_RETRACEMENT_1", 0.75)
                target = round(entry_price + retrace_1 * (prev_high - entry_price), 2)

                # 0.4.10: Half position size for re-entry
                pos_size = min(pos_per_stock, config.MAX_POSITION_SIZE)
                reentry_pos_ratio = getattr(config, "REENTRY_POSITION_RATIO", 0.5)
                reentry_size = pos_size * reentry_pos_ratio
                # Check actual buying power before re-entry
                try:
                    bp = float(trading_client.get_account().buying_power)
                    if bp < reentry_size:
                        log(f"  {symbol}: re-entry skipped, buying power ${bp:.2f} < alloc ${reentry_size:.2f}")
                        reentry_checked.add(symbol)
                        continue
                    reentry_size = min(reentry_size, bp * 0.95)
                except Exception:
                    pass
                shares = int(reentry_size / entry_price)
                if force_qty > 0:
                    shares = 1  # Re-entry also 1 share in test mode
                if shares <= 0:
                    reentry_checked.add(symbol)
                    continue

                limit_price = round(entry_price * (1 + ENTRY_LIMIT_BUFFER), 2)
                order = place_buy_limit(symbol, shares, limit_price)
                if order:
                    # 0.4.17: Re-entry positions also use 6-tier target lists (empty targets for re-entry)
                    pos = LivePosition(
                        symbol=symbol, entry_price=entry_price, shares=shares,
                        stop_price=stop, open_price=cand["open_price"],
                        trade_type="reentry", prev_high=prev_high,
                        reentry_target=target, entry_time=now_est,
                        atr=atr,
                        targets=[], sell_ratios=[], trail_pcts=[],
                        reached_list=[], sold_shares_list=[],
                        target_mode="reentry",
                    )
                    positions.append(pos)
                    place_protective_stop(pos)
                    reentry_checked.add(symbol)
                    log(f"RE-ENTERED {symbol}: entry=${entry_price:.4f}, limit=${limit_price:.4f}, "
                        f"stop=${stop:.4f}, target=${target:.4f}, prev_high=${prev_high:.4f}, shares={shares}, atr=${atr:.4f}")
                    events_log.append(f"{now_est.strftime('%H:%M:%S')} RE-ENTERED {symbol} @ ${entry_price:.4f} (v2)")
                    # Update WebSocket subscriptions
                    if _stream_state:
                        _stream_state.update_symbols([symbol])
        elif now_time >= reentry_cutoff_time and poll_count == 1:
            log(f"Re-entry cutoff reached ({REENTRY_CUTOFF} EST). No more re-entries.")

        # ── Cleanup fully exited positions ──
        positions = [p for p in positions if p.remaining_shares > 0]

        # ── Save state ──
        save_state(positions, candidates, daily_trades, daily_stopped,
                   entry_checked, day_highs, accumulator, events_log)
        save_chart_data(accumulator, positions, chart_events, str(now_est.date()))

        # ── Status log ──
        if poll_count % 4 == 0 and positions:
            for pos in positions:
                snap = snaps.get(pos.symbol)
                cur = float(snap.latest_trade.price) if snap and snap.latest_trade else 0
                pnl = (cur - pos.entry_price) * pos.remaining_shares if cur > 0 else 0
                protective = f", prot={pos.protective_order_id[:8] if pos.protective_order_id else 'none'}"
                if pos.trade_type == "reentry":
                    tier_info = f", t1={'Y' if pos.reached_target1 else 'N'}, be={'Y' if pos.breakeven_active else 'N'}, bars={pos.reentry_bar_count}"
                    log(f"  {pos.symbol}({pos.trade_type}): {pos.remaining_shares} shares, "
                        f"entry=${pos.entry_price:.4f} cur=${cur:.4f} pnl=${pnl:.2f}{tier_info}{protective}")
                else:
                    # Show target mode and reached tiers
                    reached_tiers = [i+1 for i, r in enumerate(pos.reached_list) if r] if pos.reached_list else []
                    mode_info = f", mode={pos.target_mode}, tiers={reached_tiers}" if pos.targets else ""
                    log(f"  {pos.symbol}({pos.trade_type}): {pos.remaining_shares} shares, "
                        f"entry=${pos.entry_price:.4f} cur=${cur:.4f} pnl=${pnl:.2f}{mode_info}{protective}")

        if now_time < dt.time(9, 45):
            time.sleep(10)
        else:
            time.sleep(30)

    # ── End of day summary ──
    log("=" * 60)
    log("Trading day complete!")
    equity = 0
    try:
        acct = trading_client.get_account()
        equity = float(acct.equity)
        log(f"Account equity: ${equity:,.2f}")
    except Exception:
        pass
    log(f"Daily trades: {daily_trades}")
    log("=" * 60)

    events_log.append(f"EOD equity=${equity:,.2f} trades={daily_trades}")
    save_state(positions, candidates, daily_trades, daily_stopped,
               entry_checked, day_highs, accumulator, events_log)
    save_chart_data(accumulator, positions, chart_events, str(dt.datetime.now(tz=ZoneInfo("America/New_York")).date()))

    # Stop WebSocket stream on exit
    if _stream_state:
        _stream_state.stop()

    return {
        "daily_trades": daily_trades,
        "trades_detail": trades_detail,
        "candidates": [{"symbol": c["symbol"], "gap_pct": c["gap_pct"],
                         "open_price": c["open_price"]} for c in candidates],
        "events_log": events_log,
    }


def _wait_force_close(force_close_started: dict, positions: list[LivePosition]):
    deadline = dt.datetime.now() + dt.timedelta(seconds=FORCE_CLOSE_LIMIT_TIMEOUT)
    while dt.datetime.now() < deadline and force_close_started:
        time.sleep(5)
        for symbol in list(force_close_started.keys()):
            still_holding = any(p.symbol == symbol and p.remaining_shares > 0 for p in positions)
            if not still_holding:
                del force_close_started[symbol]
                continue
            try:
                open_orders = trading_client.get_orders(filter={
                    "status": "open",
                    "symbols": symbol,
                })
                sell_orders = [o for o in open_orders if o.side == OrderSide.SELL]
                if not sell_orders:
                    del force_close_started[symbol]
            except Exception:
                pass

    for symbol in list(force_close_started.keys()):
        cancel_all_orders()
        for pos in positions:
            if pos.symbol == symbol and pos.remaining_shares > 0:
                log(f"FORCE CLOSE MARKET FALLBACK: {symbol} {pos.remaining_shares} shares")
                order = place_sell_market(pos.symbol, pos.remaining_shares)
                if order:
                    _wait_order_filled(order.id, timeout=30)
                pos.remaining_shares = 0
        del force_close_started[symbol]
    _force_close_remaining(positions)


if __name__ == "__main__":
    run_live()
