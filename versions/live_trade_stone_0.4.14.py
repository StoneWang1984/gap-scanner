"""Stone 0.4.14 Live Paper Trading — based on 0.4.13 + safety improvements.

Changes over 0.4.13:
- Leveraged ETF filter (hardcoded list + suffix/pattern matching)
- Stop loss max cap: 10% (STOP_LOSS_MAX_PCT = 0.10)
- Daily loss circuit breaker: 5% (MAX_DAILY_LOSS_PCT = 0.05)
- Re-entry min pullback: 3% from peak (REENTRY_MIN_PULLBACK = 0.03)
- Scanner: PRICE_MIN = $1.0 (aligned with 0.4.10)

Pre-market scanning:
- Gap scan runs at 9:20 EST (10 min before 9:30 open)
- Scan finishes by ~9:25, candidates ready before market open
- Open prices are refreshed at 9:30 with regular-session data

Data feed:
- Default: IEX (free, real-time, ~2-3% market volume)
- Optional: SIP ($99/mo, consolidated tape, all exchanges)
- Set DATA_FEED = "sip" in config to upgrade
"""

import re
import json
import time
import datetime as dt
from zoneinfo import ZoneInfo
from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest, MarketOrderRequest,
    StopLimitOrderRequest, TrailingStopOrderRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType, QueryOrderStatus
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
_spec = importlib.util.spec_from_file_location("config", os.path.join(_ver_dir, "config_stone_0.4.14.py"))
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


_LOG_FILE = os.path.join(_ver_dir, "live_0414.log")
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


# ── Position tracking ──────────────────────────────────────────────
@dataclass
class LivePosition:
    symbol: str
    entry_price: float
    shares: int
    stop_price: float
    target_75: float
    target_1125: float
    target_150: float
    open_price: float
    trade_type: str = "first"
    reached_75: bool = False
    reached_1125: bool = False
    reached_150: bool = False
    sold_75_shares: int = 0
    sold_1125_shares: int = 0
    sold_150_shares: int = 0
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

    def __post_init__(self):
        self.remaining_shares = self.shares
        self.highest = self.entry_price


# ── State export ────────────────────────────────────────────────────
def save_state(positions, candidates, daily_trades, daily_stopped,
               entry_checked, day_highs, accumulator, events_log):
    all_syms = set([c["symbol"] for c in candidates] + [p.symbol for p in positions])
    state = {
        "updated": dt.datetime.now().isoformat(),
        "version": "0.4.14",
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
                "target_75": p.target_75, "target_1125": p.target_1125,
                "target_150": p.target_150,
                "highest": p.highest, "trade_type": p.trade_type,
                "reached_75": p.reached_75, "reached_1125": p.reached_1125,
                "reached_150": p.reached_150,
                "open_price": p.open_price,
                "entry_time": p.entry_time.isoformat() if p.entry_time else None,
                "reentry_target": p.reentry_target, "prev_high": p.prev_high,
                "protective_order_id": p.protective_order_id,
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
                sym_entry["targets"] = {
                    "75%": round(pos.target_75, 4),
                    "112.5%": round(pos.target_1125, 4),
                    "150%": round(pos.target_150, 4),
                }
                if pos.trade_type == "reentry" and pos.reentry_target > 0:
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


# ── Data feed selection ───────────────────────────────────────────────
# IEX: free, real-time, but only IEX exchange (~2-3% market volume)
# SIP: $99/mo, consolidated tape from all exchanges, better for small/mid-cap
DATA_FEED = DataFeed.IEX
_cfg_feed = getattr(config, "DATA_FEED", "iex").lower()
if _cfg_feed == "sip":
    DATA_FEED = DataFeed.SIP
    log("Using SIP data feed (consolidated, all exchanges)")
else:
    log("Using IEX data feed (free, IEX exchange only — ~2-3% market volume)")


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
        # Use configured data feed (IEX or SIP)
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
                    if abs(new_open - old_open) / old_open > 0.005:
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
    # Method 1: Alpaca close_position API (atomic cancel + sell)
    try:
        result = trading_client.close_position(symbol, cancel_orders=True)
        if result:
            log(f"FORCE SELL (close_position): {symbol} {qty} shares")
            return True
    except Exception as e:
        log(f"close_position failed for {symbol}: {e}")

    # Method 2: Cancel all pending orders first, then market sell
    try:
        cancel_all_orders()
        time.sleep(1)
        order = place_sell_market(symbol, qty)
        if order:
            filled = _wait_order_filled(order.id, timeout=30)
            if filled:
                log(f"FORCE SELL (market): {symbol} {qty} shares")
                return True
    except Exception as e:
        log(f"market sell failed for {symbol}: {e}")

    # Method 3: Cancel all, wait longer, retry market sell
    try:
        cancel_all_orders()
        time.sleep(3)
        order = place_sell_market(symbol, qty)
        if order:
            filled = _wait_order_filled(order.id, timeout=30)
            if filled:
                log(f"FORCE SELL (retry): {symbol} {qty} shares")
                return True
    except Exception as e:
        log(f"All sell methods failed for {symbol}: {e}")

    return False


def check_order_filled(order_id) -> bool:
    try:
        order = trading_client.get_order_by_id(order_id)
        return order.status == OrderStatus.FILLED
    except Exception as e:
        log(f"check_order_filled error for {order_id}: {e}")
        return False


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
        pos.protective_order_id = order.id
        return order.id
    return None


def replace_with_trailing_stop(pos: LivePosition, trail_pct: float) -> str | None:
    if pos.protective_order_id:
        cancel_order(pos.protective_order_id)
        pos.protective_order_id = None
    order = place_trailing_stop_sell(pos.symbol, pos.remaining_shares, trail_pct * 100)
    if order:
        pos.protective_order_id = order.id
        return order.id
    log(f"Trailing stop failed for {pos.symbol}, falling back to stop-limit")
    return place_protective_stop(pos)


def replace_stop_for_remaining(pos: LivePosition) -> str | None:
    if pos.protective_order_id:
        cancel_order(pos.protective_order_id)
        pos.protective_order_id = None

    if pos.remaining_shares <= 0:
        return None

    if pos.reached_75:
        if pos.trade_type == "first":
            if pos.reached_150:
                trail_pct = config.TRAILING_STOP_PCT_150
            elif pos.reached_1125:
                trail_pct = config.TRAILING_STOP_PCT_1125
            else:
                trail_pct = config.TRAILING_STOP_PCT_75
            return replace_with_trailing_stop(pos, trail_pct)
        elif pos.trade_type == "reentry" and pos.reached_150:
            return replace_with_trailing_stop(pos, config.REENTRY_TRAILING_PCT)

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
            f"${t.get('entry',0):.2f}→${t.get('exit',0):.2f}  "
            f"{t.get('exit_reason','?'):20s} {pnl_s}")
    log("=" * 50)

    return report


# ── Main scheduler ────────────────────────────────────────────────
def run_live():
    log("=" * 60)
    log("Stone 0.4.14 Live Paper Trading — Auto Scheduler")
    log(f"Capital: ${config.INITIAL_CAPITAL:,.0f} | Max daily trades: {config.MAX_DAILY_TRADES}")
    log(f"Entry buffer: +{ENTRY_LIMIT_BUFFER:.1%} | Stop-limit buffer: -{STOP_LIMIT_BUFFER:.1%}")
    log(f"Target buffer: -{TARGET_LIMIT_BUFFER:.1%} | Force-close timeout: {FORCE_CLOSE_LIMIT_TIMEOUT}s")
    log(f"Re-entry cutoff: {REENTRY_CUTOFF} EST | Leveraged ETF filter: ON")
    log(f"Re-entry v2: half-pos, ATR stop, tier-1 target + trailing, breakeven, NO time stop")
    log(f"0.4.14: Based on 0.4.13, added leveraged ETF filter, stop cap 10%, daily loss 5% circuit breaker, re-entry min pullback 3%")
    log(f"Pre-market scan: runs at 9:20 EST, candidates ready by ~9:25")
    log(f"Data feed: {'SIP (consolidated)' if DATA_FEED == DataFeed.SIP else 'IEX (free, ~2-3% market volume)'}")
    log("=" * 60)

    if not test_connectivity():
        log("Data connectivity failed. Cannot trade.")
        return

    # Main scheduling loop — runs forever
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
                     - dt.timedelta(minutes=30)
            log(f"Today ({today}) is NOT a trading day. "
                f"Next trading day: {next_day['date']} (open {next_day['open']} EST)")
            smart_sleep_until(target)
            continue

        # Today is a trading day — get close time and force close
        close_str = today_info["close"]
        force_close_str = calc_force_close_time(close_str)
        close_h, close_m = int(close_str[:2]), int(close_str[3:5])
        fc_h, fc_m = int(force_close_str[:2]), int(force_close_str[3:5])
        open_h, open_m = int(today_info["open"][:2]), int(today_info["open"][3:5])

        force_close_time = dt.time(fc_h, fc_m)
        open_time = dt.time(open_h, open_m)
        pre_open_time = dt.time(open_h, open_m - 30 if open_m >= 30 else 0,
                                open_m - 30 + 60 if open_m < 30 else 0)

        # If already past force close, wait for next trading day
        if now_est.time() >= force_close_time:
            next_day = get_next_trading_day(trading_client, today + dt.timedelta(days=1))
            next_date = dt.date.fromisoformat(next_day["date"])
            n_open_h, n_open_m = int(next_day["open"][:2]), int(next_day["open"][3:5])
            target = dt.datetime(next_date.year, next_date.month, next_date.day,
                                 n_open_h, n_open_m, tzinfo=ZoneInfo("America/New_York")) \
                     - dt.timedelta(minutes=10)
            log(f"Market already closed for today. Next trading day: {next_day['date']}")
            smart_sleep_until(target)
            continue

        # Pre-open at 9:20 EST (10 min before 9:30 open) — scan finishes by ~9:25
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
            version="0.4.14",
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

    capital = config.INITIAL_CAPITAL

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
        # Wait for market open and try again (some stocks may not have pre-market bars)
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

    deployable = calc_position_size(capital)
    pos_per_stock = min(deployable, config.MAX_POSITION_SIZE)
    max_stocks = max(config.MAX_POSITIONS_PER_DAY, int(deployable / pos_per_stock))
    candidates = candidates[:max_stocks]

    log(f"Candidates: {[c['symbol'] for c in candidates]}")
    for c in candidates:
        log(f"  {c['symbol']}: gap +{c['gap_pct']:.1%}, open=${c['open_price']:.4f}")
        day_highs[c['symbol']] = c['open_price']

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
                log(f"BUY FILLED: {symbol} order {order_id}")
                pos = LivePosition(**pos_data)
                positions.append(pos)
                place_protective_stop(pos)
                events_log.append(f"{now_est.strftime('%H:%M:%S')} BUY FILLED {symbol} @ ${pos.entry_price:.4f}")
                add_chart_event(symbol, "buy", pos.entry_price,
                                f"BUY {pos.shares}sh" if pos.trade_type != "reentry" else f"RE-ENTRY BUY {pos.shares}sh")
                del pending_buys[symbol]
            elif check_order_canceled(order_id):
                log(f"BUY CANCELED: {symbol} order {order_id}")
                events_log.append(f"{now_est.strftime('%H:%M:%S')} BUY CANCELED {symbol}")
                del pending_buys[symbol]

        # ── Check protective order fills ──
        for pos in positions[:]:
            if pos.remaining_shares <= 0:
                continue
            if pos.protective_order_id and check_order_filled(pos.protective_order_id):
                log(f"PROTECTIVE ORDER FILLED: {pos.symbol} order {pos.protective_order_id}")
                events_log.append(f"{now_est.strftime('%H:%M:%S')} PROTECTIVE FILLED {pos.symbol}")
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
            close_all_positions()
            break

        # ── Collect snapshot data ──
        all_symbols = list(set(
            [c['symbol'] for c in candidates] +
            [p.symbol for p in positions] +
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

        # ── Pullback stop (15% from day high) ──
        if not daily_stopped:
            for symbol in all_symbols:
                snap = snaps.get(symbol)
                if not snap or not snap.daily_bar:
                    continue
                dh = day_highs.get(symbol, 0)
                dl = float(snap.daily_bar.low)
                if dh > 0 and (dh - dl) / dh > config.PULLBACK_STOP_THRESHOLD:
                    daily_stopped = True
                    log(f"PULLBACK STOP: {symbol} dropped {(dh - dl) / dh:.1%} from high ${dh:.4f}")
                    events_log.append(f"{now_est.strftime('%H:%M:%S')} PULLBACK STOP {symbol} -{(dh - dl) / dh:.1%}")
                    for pos in positions:
                        if pos.remaining_shares > 0:
                            sold = force_sell_position(pos.symbol, pos.remaining_shares)
                            if sold:
                                log(f"PULLBACK STOP FILLED: {pos.symbol} {pos.remaining_shares} shares")
                            record_trade(pos, dl, "pullback_stop")
                            pos.remaining_shares = 0
                            pos.protective_order_id = None
                    break

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

            # ── First trade profit targets ──
            if pos.trade_type == "first":
                need_replace_protective = False

                # 0.4.11: Time limit — if no target hit in 40 min, sell at breakeven+
                pos.bar_count += 1
                time_limit = getattr(config, "FIRST_TRADE_TIME_LIMIT_BARS", 0)
                if time_limit > 0 and not pos.reached_75 and pos.bar_count >= time_limit:
                    pos.time_limit_active = True
                if pos.time_limit_active and cur_price >= pos.entry_price and pos.remaining_shares > 0:
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

                if not pos.reached_150 and pos.highest >= pos.target_150:
                    pos.reached_150 = pos.reached_1125 = pos.reached_75 = True
                    n75 = pos.shares // 4
                    n1125 = (pos.shares - n75) // 3
                    n150 = (pos.shares - n75 - n1125) // 3
                    total_sell = n75 + n1125 + n150
                    if total_sell > 0 and total_sell <= pos.remaining_shares:
                        sell_price = round(pos.target_150 * (1 - TARGET_LIMIT_BUFFER), 2)
                        log(f"150% TARGET: {pos.symbol} selling {total_sell} @ ${sell_price:.4f}")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} 150% TARGET {pos.symbol} sell {total_sell} @ ${sell_price:.4f}")
                        add_chart_event(pos.symbol, "sell", sell_price, f"TARGET_150 {total_sell}sh")
                        if pos.protective_order_id:
                            cancel_order(pos.protective_order_id)
                            pos.protective_order_id = None
                        place_sell_limit(pos.symbol, total_sell, sell_price)
                        pos.sold_75_shares += n75
                        pos.sold_1125_shares += n1125
                        pos.sold_150_shares += n150
                        pos.remaining_shares -= total_sell
                        need_replace_protective = True

                elif not pos.reached_1125 and pos.highest >= pos.target_1125:
                    pos.reached_1125 = pos.reached_75 = True
                    n75 = pos.shares // 4
                    n1125 = (pos.shares - n75) // 3
                    total_sell = n75 + n1125
                    if total_sell > 0 and total_sell <= pos.remaining_shares:
                        sell_price = round(pos.target_1125 * (1 - TARGET_LIMIT_BUFFER), 2)
                        log(f"112.5% TARGET: {pos.symbol} selling {total_sell} @ ${sell_price:.4f}")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} 112.5% TARGET {pos.symbol} sell {total_sell} @ ${sell_price:.4f}")
                        add_chart_event(pos.symbol, "sell", sell_price, f"TARGET_1125 {total_sell}sh")
                        if pos.protective_order_id:
                            cancel_order(pos.protective_order_id)
                            pos.protective_order_id = None
                        place_sell_limit(pos.symbol, total_sell, sell_price)
                        pos.sold_75_shares += n75
                        pos.sold_1125_shares += n1125
                        pos.remaining_shares -= total_sell
                        need_replace_protective = True

                elif not pos.reached_75 and pos.highest >= pos.target_75:
                    pos.reached_75 = True
                    n75 = pos.shares // 4
                    if n75 > 0 and n75 <= pos.remaining_shares:
                        sell_price = round(pos.target_75 * (1 - TARGET_LIMIT_BUFFER), 2)
                        log(f"75% TARGET: {pos.symbol} selling {n75} @ ${sell_price:.4f}")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} 75% TARGET {pos.symbol} sell {n75} @ ${sell_price:.4f}")
                        add_chart_event(pos.symbol, "sell", sell_price, f"TARGET_75 {n75}sh")
                        if pos.protective_order_id:
                            cancel_order(pos.protective_order_id)
                            pos.protective_order_id = None
                        place_sell_limit(pos.symbol, n75, sell_price)
                        pos.sold_75_shares = n75
                        pos.remaining_shares -= n75
                        need_replace_protective = True

                if need_replace_protective and pos.remaining_shares > 0:
                    replace_stop_for_remaining(pos)

                # ── Trailing stop (polled fallback) ──
                if pos.reached_75 and pos.remaining_shares > 0:
                    if pos.reached_150:
                        pct = config.TRAILING_STOP_PCT_150
                    elif pos.reached_1125:
                        pct = config.TRAILING_STOP_PCT_1125
                    else:
                        pct = config.TRAILING_STOP_PCT_75
                    tsp = round(pos.highest * (1 - pct), 2)
                    tsp = max(tsp, pos.entry_price)
                    if cur_price <= tsp:
                        tier = "150%" if pos.reached_150 else "112.5%" if pos.reached_1125 else "75%"
                        log(f"TRAILING STOP({tier}) (polled): {pos.symbol} @ ${tsp:.4f} (high=${pos.highest:.4f})")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} TRAILING STOP({tier}) {pos.symbol} @ ${tsp:.4f}")
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
                        place_sell_limit(pos.symbol, n, sell_price)
                        pos.sold_partial1_shares = n
                        pos.remaining_shares -= n
                        pos.breakeven_active = True
                        need_replace_protective = True

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
                if getattr(config, "ENTRY_BELOW_OPEN", True) and entry_price >= cand["open_price"]:
                    log(f"  {symbol}: entry ${entry_price:.4f} >= open ${cand['open_price']:.4f}, skipping")
                    entry_checked.add(symbol)
                    continue

                bars_5m = accumulator.get_5min_bars(symbol)
                if len(bars_5m) >= 2:
                    atr = calc_atr(bars_5m, period=14)
                else:
                    atr = get_prev_day_atr(symbol)

                stop = calc_stop_price(entry_price, atr)
                target_75 = calc_price_at_retracement(entry_price, cand["open_price"], config.PROFIT_RETRACEMENT_75)
                target_1125 = calc_price_at_retracement(entry_price, cand["open_price"], config.PROFIT_RETRACEMENT_1125)
                target_150 = calc_price_at_retracement(entry_price, cand["open_price"], config.PROFIT_RETRACEMENT_150)

                pos_size = min(calc_position_size(capital), config.MAX_POSITION_SIZE)
                shares = int(pos_size / entry_price)
                if shares <= 0:
                    entry_checked.add(symbol)
                    continue

                limit_price = round(entry_price * (1 + ENTRY_LIMIT_BUFFER), 2)
                order = place_buy_limit(symbol, shares, limit_price)
                if order:
                    pos_data = {
                        "symbol": symbol, "entry_price": entry_price, "shares": shares,
                        "stop_price": stop, "target_75": target_75, "target_1125": target_1125,
                        "target_150": target_150, "open_price": cand["open_price"],
                        "entry_time": now_est, "atr": atr,
                    }
                    pending_buys[symbol] = (order.id, pos_data)
                    entry_checked.add(symbol)
                    log(f"BUY PENDING {symbol}: entry=${entry_price:.4f}, limit=${limit_price:.4f}, "
                        f"stop=${stop:.4f}, target75=${target_75:.4f}, target150=${target_150:.4f}, shares={shares}")
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
                    start_equity = capital  # capital is current equity
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
                pos_size = min(calc_position_size(capital), config.MAX_POSITION_SIZE)
                reentry_pos_ratio = getattr(config, "REENTRY_POSITION_RATIO", 0.5)
                shares = int((pos_size * reentry_pos_ratio) / entry_price)
                if shares <= 0:
                    reentry_checked.add(symbol)
                    continue

                limit_price = round(entry_price * (1 + ENTRY_LIMIT_BUFFER), 2)
                order = place_buy_limit(symbol, shares, limit_price)
                if order:
                    pos = LivePosition(
                        symbol=symbol, entry_price=entry_price, shares=shares,
                        stop_price=stop, target_75=0, target_1125=0,
                        target_150=0, open_price=cand["open_price"],
                        trade_type="reentry", prev_high=prev_high,
                        reentry_target=target, entry_time=now_est,
                        atr=atr,
                    )
                    positions.append(pos)
                    place_protective_stop(pos)
                    reentry_checked.add(symbol)
                    log(f"RE-ENTERED {symbol}: entry=${entry_price:.4f}, limit=${limit_price:.4f}, "
                        f"stop=${stop:.4f}, target=${target:.4f}, prev_high=${prev_high:.4f}, shares={shares}, atr=${atr:.4f}")
                    events_log.append(f"{now_est.strftime('%H:%M:%S')} RE-ENTERED {symbol} @ ${entry_price:.4f} (v2)")
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
                    log(f"  {pos.symbol}({pos.trade_type}): {pos.remaining_shares} shares, "
                        f"entry=${pos.entry_price:.4f} cur=${cur:.4f} pnl=${pnl:.2f}{protective}")

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
