"""stonewang_daytrade_getgreenbar_1.0 — Green Bar Momentum Live Trading.

Key design vs rtg_1.0:
  - GreenBarDetector processes real-time trade prints to detect bar direction
  - Entry: red->green bar transition + volume spike + above open_price
  - Exit: bar_turned_red / stop_loss / trail_stop / target (priority order)
  - Re-entry ALLOWED (up to MAX_DAILY_ENTRIES_PER_SYMBOL=6 per stock per day, 60s cooldown)
  - Full day window: 09:30-15:30
  - WebSocket trades stream feeds GreenBarDetector (not just BarAccumulator)
"""

import re
import json
import time
import datetime as dt
from zoneinfo import ZoneInfo
from dataclasses import dataclass
import threading
from collections import deque
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import Adjustment, DataFeed

import importlib.util, sys, os

_ver_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_ver_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

_spec = importlib.util.spec_from_file_location("config", os.path.join(_ver_dir, "config.py"))
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)
sys.modules["config"] = config

from scanner import get_tradable_symbols, scan_gaps_batch, scan_gaps_for_symbols, get_data_client

_EST = ZoneInfo("America/New_York")
_LEV_PATTERN = re.compile(r"(2X|3X|BULL|BEAR)$", re.IGNORECASE)
_LEV_PREFIXES = (
    "TQQQ", "SQQQ", "FNGU", "FNGD", "SOXL", "SOXS", "TECL", "TECS",
    "UDOW", "SDOW", "SPXU", "UPRO", "TNA", "TZA", "NUGT", "DUST",
    "JNUG", "JDST", "BOIL", "KOLD", "DRN", "DRV", "LABU", "LABD",
    "CURE", "YINN", "YANG", "UMDD", "SMDD", "CONL", "NAIL", "WEBL",
    "MSTU", "MSTZ", "UGL", "GLL", "DGP", "DGZ", "AXTU", "RDWU",
)

_ALPACA_PAPER = getattr(config, "ALPACA_PAPER", False)
trading_client = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=_ALPACA_PAPER)
data_client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)

_log_file = None
_state_file = os.path.join(_parent_dir, "live_state.json")


def is_leveraged_etf(symbol):
    if _LEV_PATTERN.search(symbol):
        return True
    if symbol.endswith(("BULL", "BEAR")):
        return True
    return any(symbol.startswith(p) for p in _LEV_PREFIXES)


def _get_rvol_tier(rvol):
    tiers = getattr(config, "RVOL_SIZING_TIERS", [(10.0, 0.50), (5.0, 0.30), (0.0, 0.15)])
    for rvol_min, pct in tiers:
        if rvol >= rvol_min:
            return rvol_min, pct
    return 0.0, 0.15


def get_rvol_sizing(rvol, equity, same_tier_count=1):
    _, pct = _get_rvol_tier(rvol)
    # Split tier equity evenly among same-tier candidates
    split_pct = pct / max(same_tier_count, 1)
    return round(equity * split_pct, 2)


def get_rvol_exit_params(rvol):
    tiers = getattr(config, "RVOL_EXIT_TIERS", [
        (10.0, 0.07, 0.30, 0.05, 0.03),
        (5.0,  0.05, 0.20, 0.05, 0.03),
        (0.0,  0.03, 0.10, 0.04, 0.02),
    ])
    for rvol_min, stop, target, trail_act, trail in tiers:
        if rvol >= rvol_min:
            return stop, target, trail_act, trail
    return 0.03, 0.15, 0.03, 0.02


def log(msg):
    ts = dt.datetime.now(_EST).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    if _log_file:
        _log_file.write(line + "\n")
        _log_file.flush()


@dataclass
class Position:
    symbol: str
    shares: int
    entry_price: float
    entry_ts: float = 0.0
    open_price: float = 0.0
    gap_pct: float = 0.0
    rvol: float = 0.0
    signal_type: str = ""         # "greenbar" or "greenbar_re"
    highest: float = 0.0
    trail_active: bool = False
    stop_pct: float = 0.0
    target_pct: float = 0.0
    trail_activate_pct: float = 0.0
    trail_pct: float = 0.0
    entry_count: int = 1


class GreenBarDetector:
    """Processes trade prints to detect bar direction in real-time.
    Detects red->green bar transitions with volume confirmation for entry,
    and green->red bar transitions for exit.
    """

    def __init__(self, symbol, open_price):
        self.symbol = symbol
        self.open_price = open_price
        self._lock = threading.Lock()
        self._current_minute = 0
        self._bar_open = 0.0
        self._bar_close = 0.0
        self._bar_high = 0.0
        self._bar_low = float('inf')
        self._bar_volume = 0
        self._bar_trade_count = 0
        self._is_green = False
        self._prev_bar = None
        self._consecutive_green = 0
        self._consecutive_red = 0
        self._bar_count = 0
        self._green_bar_count = 0

    def on_trade(self, price, size, ts_epoch):
        minute = (int(ts_epoch) // 60) * 60
        with self._lock:
            if minute != self._current_minute:
                self._complete_bar()
                self._current_minute = minute
                self._bar_open = price
                self._bar_close = price
                self._bar_high = price
                self._bar_low = price
                self._bar_volume = 0
                self._bar_trade_count = 0
            self._bar_close = price
            if price > self._bar_high:
                self._bar_high = price
            if price < self._bar_low:
                self._bar_low = price
            self._bar_volume += size
            self._bar_trade_count += 1
            if self._bar_trade_count >= config.GBAR_MIN_TRADES_IN_BAR:
                self._is_green = self._bar_close > self._bar_open

    def _complete_bar(self):
        if self._current_minute == 0 or self._bar_trade_count == 0:
            return
        is_green = self._bar_close > self._bar_open
        self._prev_bar = {
            "green": is_green,
            "volume": self._bar_volume,
            "close": self._bar_close,
            "open": self._bar_open,
            "high": self._bar_high,
            "low": self._bar_low,
        }
        self._bar_count += 1
        if is_green:
            self._green_bar_count += 1
            self._consecutive_green += 1
            self._consecutive_red = 0
        else:
            self._consecutive_red += 1
            self._consecutive_green = 0

    def should_enter(self) -> bool:
        with self._lock:
            if self._prev_bar is None:
                return False
            # Previous bar must be red (not green)
            if self._prev_bar["green"]:
                return False
            # Current bar must be green
            if not self._is_green:
                return False
            # Volume must exceed previous bar * multiplier (volume spike)
            if self._bar_volume < self._prev_bar["volume"] * config.GBAR_VOLUME_MULT:
                return False
            # Absolute volume floor
            if self._bar_volume < config.GBAR_MIN_VOLUME:
                return False
            # Price must be above open_price (confirming gap momentum)
            if self._bar_close < self.open_price:
                return False
            return True

    def should_exit(self, entry_price, stop_pct, trail_active, highest, trail_activate_pct, trail_pct, target_pct):
        with self._lock:
            cur = self._bar_close
            # Priority order: stop_loss > bar_turned_red > trail_stop > target
            if cur <= entry_price * (1 - stop_pct):
                return True, "stop_loss"
            if config.GBAR_EXIT_ON_RED_BAR and not self._is_green and self._bar_trade_count >= config.GBAR_RED_BAR_CONFIRM_TRADES:
                return True, "bar_turned_red"
            if trail_active and cur <= highest * (1 - trail_pct):
                return True, "trail_stop"
            if self._bar_high >= entry_price * (1 + target_pct):
                return True, "target"
            return False, ""

    def latest_price(self) -> float:
        with self._lock:
            return self._bar_close


class BarAccumulator:
    def __init__(self):
        self._bars = {}
        self._current = {}
        self._lock = threading.Lock()

    def add_bar(self, symbol, bar_dict):
        with self._lock:
            if symbol not in self._bars:
                self._bars[symbol] = deque(maxlen=500)
            self._bars[symbol].append(dict(bar_dict))

    def add_trade(self, symbol, price, size, ts):
        with self._lock:
            if symbol not in self._bars:
                self._bars[symbol] = deque(maxlen=500)
            bar_ts = ts.replace(second=0, microsecond=0)
            if symbol not in self._current:
                self._current[symbol] = {
                    "open": price, "high": price, "low": price,
                    "close": price, "volume": size, "timestamp": bar_ts,
                }
            else:
                cur = self._current[symbol]
                if bar_ts != cur["timestamp"]:
                    self._bars[symbol].append(dict(cur))
                    cur.update(open=price, high=price, low=price, close=price, volume=size, timestamp=bar_ts)
                else:
                    cur["high"] = max(cur["high"], price)
                    cur["low"] = min(cur["low"], price)
                    cur["close"] = price
                    cur["volume"] += size

    def get_1min_bars(self, symbol):
        with self._lock:
            bars = list(self._bars.get(symbol, []))
            if symbol in self._current:
                bars.append(dict(self._current[symbol]))
            return bars


_stream_state = {"running": False, "last_bar_ts": time.time(), "symbols": set()}
_accumulator = BarAccumulator()
_gbar_detectors: dict = {}
_ws_stream = None


async def _on_bar(bar):
    _stream_state["last_bar_ts"] = time.time()
    sym = bar.symbol
    ts = bar.timestamp
    if hasattr(ts, "timestamp"):
        ts = dt.datetime.fromtimestamp(ts.timestamp(), tz=_EST)
    _accumulator.add_bar(sym, {
        "open": float(bar.open), "high": float(bar.high),
        "low": float(bar.low), "close": float(bar.close),
        "volume": int(bar.volume), "timestamp": ts,
    })


async def _on_trade(trade):
    if _stream_state is None:
        return
    _stream_state["last_bar_ts"] = time.time()
    sym = trade.symbol
    ts = trade.timestamp.timestamp() if hasattr(trade.timestamp, 'timestamp') else time.time()
    # Feed BarAccumulator for WS heartbeat
    ts_dt = dt.datetime.fromtimestamp(ts, tz=_EST) if isinstance(ts, (int, float)) else ts
    _accumulator.add_trade(sym, float(trade.price), int(trade.size), ts_dt)
    # Feed GreenBarDetector if registered for this symbol
    detector = _gbar_detectors.get(sym)
    if detector is None:
        return  # only feed registered candidates
    detector.on_trade(float(trade.price), int(trade.size), ts)


def start_ws_stream(symbols):
    global _ws_stream
    if _stream_state["running"]:
        return
    try:
        from alpaca.data.live.stock import StockDataStream
        _ws_stream = StockDataStream(
            config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY,
            feed=getattr(config, "DATA_FEED_OBJ", DataFeed.SIP),
        )
        for sym in symbols:
            _ws_stream.subscribe_bars(_on_bar, sym)
            _ws_stream.subscribe_trades(_on_trade, sym)
        _stream_state["symbols"] = set(symbols)
        _stream_state["running"] = True
        _stream_state["last_bar_ts"] = time.time()

        def _run():
            try:
                _ws_stream.run()
            except Exception as e:
                log(f"WebSocket error: {e}")
            _stream_state["running"] = False

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        log(f"WebSocket stream started for {len(symbols)} symbols")
    except Exception as e:
        log(f"WebSocket start failed: {e}")


def restart_ws_stream(symbols):
    _stream_state["running"] = False
    if _ws_stream:
        try:
            # Non-blocking stop: run in thread with 5s timeout to avoid freezing main loop
            stop_result = []

            def _do_stop():
                try:
                    _ws_stream.stop()
                    stop_result.append(True)
                except Exception:
                    stop_result.append(False)

            stop_thread = threading.Thread(target=_do_stop, daemon=True)
            stop_thread.start()
            stop_thread.join(timeout=5)
            if stop_thread.is_alive():
                log("WebSocket stop timed out after 5s, proceeding anyway")
            elif stop_result and not stop_result[0]:
                log("WebSocket stop failed (non-fatal)")
        except Exception as e:
            log(f"WebSocket stop error (non-fatal): {e}")
    time.sleep(2)
    start_ws_stream(symbols)


def place_buy_market(symbol, shares):
    """Submit market buy order. Returns (order, None, None) on success or (None, None, reason) on rejection."""
    if config.DRY_RUN:
        oid = f"DRY-{uuid4().hex[:8]}"
        log(f"[DRY] BUY {symbol} {shares}sh")
        return type("Order", (), {"id": oid})(), None, None
    try:
        req = MarketOrderRequest(symbol=symbol, qty=shares, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        order = trading_client.submit_order(req)
        log(f"BUY order submitted: {symbol} {shares}sh, id={order.id}")
        return order, None, None
    except Exception as e:
        log(f"BUY rejected: {symbol} {shares}sh - {e}")
        return None, None, str(e)


def wait_order_filled(order_id, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            order = trading_client.get_order_by_id(str(order_id))
            if order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                return int(float(order.filled_qty)), float(order.filled_avg_price or 0)
            if order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
                return 0, 0.0
        except Exception:
            pass
        time.sleep(1)
    return 0, 0.0


def get_order_filled_qty(order_id):
    try:
        order = trading_client.get_order_by_id(str(order_id))
        return int(float(order.filled_qty))
    except Exception:
        return 0


def get_order_filled_price(order_id):
    try:
        order = trading_client.get_order_by_id(str(order_id))
        return float(order.filled_avg_price or 0)
    except Exception:
        return 0.0


def cancel_order(order_id):
    try:
        trading_client.cancel_order_by_id(str(order_id))
        return True
    except Exception:
        return False


def place_sell_market(symbol, shares):
    if config.DRY_RUN:
        log(f"[DRY] SELL {symbol} {shares}sh")
        return shares, 0.0
    try:
        req = MarketOrderRequest(symbol=symbol, qty=shares, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
        order = trading_client.submit_order(req)
        filled, price = wait_order_filled(str(order.id), timeout=30)
        return filled, price
    except Exception as e:
        log(f"SELL failed: {symbol} {shares}sh - {e}")
        return 0, 0.0


def force_sell_position(symbol, shares):
    try:
        trading_client.close_position(symbol_or_asset_id=symbol)
        # Wait for fill confirmation (up to 30s)
        time.sleep(1)
        for _ in range(30):
            try:
                pos = trading_client.get_open_position(symbol)
                remaining = int(float(pos.qty))
                if remaining <= 0:
                    break
            except Exception:
                break  # Position not found = fully closed
            time.sleep(1)
        # Get fill price from recent closed order (last 5 minutes only)
        fill_price = 0.0
        try:
            cutoff = dt.datetime.now(_EST) - dt.timedelta(minutes=5)
            orders = trading_client.get_orders_for_symbol(symbol)
            for o in orders:
                if o.side == OrderSide.SELL and o.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                    # Only match orders submitted in the last 5 minutes
                    submitted = getattr(o, "submitted_at", None)
                    if submitted and hasattr(submitted, "timestamp"):
                        submitted_dt = dt.datetime.fromtimestamp(submitted.timestamp(), tz=_EST)
                        if submitted_dt < cutoff:
                            continue
                    filled_qty = int(float(o.filled_qty))
                    if filled_qty >= shares:
                        fill_price = float(o.filled_avg_price or 0)
                        break
        except Exception:
            pass
        return shares, fill_price
    except Exception as e:
        log(f"Force close failed: {symbol} - {e}")
        return 0, 0.0


def save_state(state):
    with open(_state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _fetch_20d_avg_volumes(symbols, target_date):
    """Fetch 20-day average daily volume from Alpaca for RVOL calculation."""
    lookback = target_date - pd.Timedelta(days=30)
    avg_vols = {}
    batch_size = 50
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch, timeframe=TimeFrame.Day,
                start=lookback, end=target_date - pd.Timedelta(days=1),
                adjustment=Adjustment.RAW,
                feed=getattr(config, "DATA_FEED_OBJ", DataFeed.SIP),
            )
            bars = data_client.get_stock_bars(req)
            if bars.df.empty:
                continue
            df = bars.df
            for sym in batch:
                try:
                    if "symbol" in df.columns:
                        sym_df = df[df["symbol"] == sym]
                    else:
                        sym_df = df.loc[sym] if sym in df.index else pd.DataFrame()
                    if not sym_df.empty:
                        recent = sym_df["volume"].tail(getattr(config, "RVOL_LOOKBACK_DAYS", 20))
                        avg_vols[sym] = float(recent.mean())
                except Exception:
                    pass
        except Exception as e:
            log(f"20d volume fetch error batch {i}: {e}")
    return avg_vols


def scan_gaps(target_date):
    log(f"Scanning for gap stocks on {target_date.date()}...")
    symbols = get_tradable_symbols()
    symbols = [s for s in symbols if not is_leveraged_etf(s)]
    log(f"After leveraged ETF filter: {len(symbols)} symbols")

    # Parallel scan: 6 concurrent batch requests
    batch_size = 200
    batches = [(i, symbols[i:i + batch_size]) for i in range(0, len(symbols), batch_size)]
    total_batches = len(batches)
    all_results = []
    completed = 0

    def _scan_one(batch_idx, batch):
        nonlocal completed
        df = scan_gaps_for_symbols(data_client, target_date, batch)
        completed += 1
        if completed % 10 == 0 or completed == total_batches:
            log(f"  Scan progress: {completed}/{total_batches} batches")
        return df

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_scan_one, idx, batch): idx for idx, batch in batches}
        try:
            for future in as_completed(futures, timeout=300):
                try:
                    df = future.result(timeout=30)
                    if not df.empty:
                        all_results.append(df)
                except Exception as e:
                    log(f"  Scan batch error: {e}")
        except TimeoutError:
            log(f"  Scan timed out after 300s, got {len(all_results)}/{total_batches} batches")
            # Cancel remaining futures
            for f in futures:
                f.cancel()

    if not all_results:
        log("No gap stocks found")
        return []
    results = pd.concat(all_results, ignore_index=True)

    # Fetch real 20-day average volumes for proper RVOL calculation
    gap_symbols = results["symbol"].tolist()
    avg_vols = _fetch_20d_avg_volumes(gap_symbols, target_date)
    log(f"Fetched 20d avg volume for {len(avg_vols)}/{len(gap_symbols)} symbols")

    candidates = []
    for _, row in results.iterrows():
        sym = row["symbol"]
        avg_vol = avg_vols.get(sym, 0)
        prev_vol = row.get("prev_volume", 0)
        if avg_vol > 0:
            rvol = prev_vol / avg_vol
        elif row.get("avg_volume_20d", 0) > 0:
            rvol = prev_vol / row["avg_volume_20d"]
        else:
            rvol = 0.0
        candidates.append({
            "symbol": sym, "open_price": float(row["open_price"]),
            "prev_close": float(row["prev_close"]), "gap_pct": float(row["gap_pct"]),
            "rvol": float(rvol),
        })
    candidates.sort(key=lambda c: c["rvol"], reverse=True)
    candidates = candidates[:config.MAX_CANDIDATES]
    log(f"Top {len(candidates)} candidates by RVOL: {[c['symbol'] for c in candidates]}")
    for c in candidates:
        log(f"  {c['symbol']}: gap +{c['gap_pct']:.1%}, RVOL={c['rvol']:.1f}x, open=${c['open_price']:.4f}")
    return candidates


def backfill_1min_bars(symbols, target_date):
    mkt_open = pd.Timestamp(f"{target_date.date()} {config.MARKET_OPEN}", tz="America/New_York")
    now = pd.Timestamp.now(tz="America/New_York")
    end = min(now, pd.Timestamp(f"{target_date.date()} {config.MARKET_CLOSE}", tz="America/New_York"))
    if end <= mkt_open:
        return
    for sym in symbols:
        try:
            req = StockBarsRequest(
                symbol_or_symbols=sym, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                start=mkt_open, end=end, adjustment=Adjustment.RAW,
                feed=getattr(config, "DATA_FEED_OBJ", DataFeed.SIP),
            )
            bars = data_client.get_stock_bars(req)
            if bars.df.empty:
                continue
            df = bars.df
            if "symbol" in df.columns:
                df = df[df["symbol"] == sym]
            for i in range(len(df)):
                bar = df.iloc[i]
                idx = df.index[i]
                ts = idx[1] if isinstance(idx, tuple) else idx
                ts = pd.Timestamp(ts)
                if ts.tzinfo is None:
                    ts = ts.tz_localize("UTC")
                ts = ts.tz_convert("America/New_York")
                _accumulator.add_bar(sym, {
                    "open": float(bar["open"]), "high": float(bar["high"]),
                    "low": float(bar["low"]), "close": float(bar["close"]),
                    "volume": int(bar["volume"]), "timestamp": ts,
                })
        except Exception:
            pass


def _parse_time(t_str):
    h, m = (int(x) for x in t_str.split(":"))
    return dt.time(h, m)


def run_trading_day(target_date):
    log(f"Starting trading day: {target_date.date()} (close {config.MARKET_CLOSE}, force_close {config.FORCE_CLOSE_TIME})")
    try:
        acct = trading_client.get_account()
        equity = float(acct.equity)
    except Exception:
        equity = config.INITIAL_CAPITAL
    log(f"Account equity: ${equity:,.2f}")

    # Fast restart: if we have existing positions and previous state, skip scan and restore immediately
    existing_alpaca_positions = []
    try:
        existing_alpaca_positions = trading_client.get_all_positions()
    except Exception:
        pass
    prev_state_for_restart = {}
    try:
        with open(_state_file) as f:
            prev_state_for_restart = json.load(f)
    except Exception:
        pass

    candidates = []
    if existing_alpaca_positions and prev_state_for_restart.get("candidates"):
        candidates = prev_state_for_restart["candidates"]
        log(f"Fast restart: {len(existing_alpaca_positions)} positions + {len(candidates)} candidates from state, skipping scan")

    if not candidates:
        candidates = scan_gaps(target_date)
    if not candidates:
        # Try restoring candidates from previous state (survive restart during trading hours)
        prev_cands = prev_state_for_restart.get("candidates", [])
        if prev_cands:
            log(f"Scan found 0 candidates, restoring {len(prev_cands)} from previous state")
            candidates = prev_cands
    if not candidates:
        # Smart retry: every 2 minutes until 09:35, then give up
        retry_until = dt.datetime.combine(target_date.date(), dt.time(9, 35), tzinfo=_EST)
        while dt.datetime.now(_EST) < retry_until:
            log("No candidates yet, retrying in 2 minutes...")
            time.sleep(120)
            candidates = scan_gaps(target_date)
            if candidates:
                break
    if not candidates:
        log("No candidates after retries, waiting for force close")
        _wait_until(target_date, _parse_time(config.FORCE_CLOSE_TIME))
        return

    syms = [c["symbol"] for c in candidates]
    backfill_1min_bars(syms, target_date)

    # Create GreenBarDetectors for all candidates
    for c in candidates:
        sym = c["symbol"]
        open_price = c["open_price"]
        _gbar_detectors[sym] = GreenBarDetector(sym, open_price)

    restart_ws_stream(syms)

    positions = []
    entry_checked = set()  # Stocks that successfully entered or were confirmed no-signal

    # Restore existing Alpaca positions (survive restart)
    try:
        existing_positions = trading_client.get_all_positions()
        prev_state = {}
        try:
            with open(_state_file) as f:
                prev_state = json.load(f)
        except Exception:
            pass
        prev_positions = {p["symbol"]: p for p in prev_state.get("positions", [])}
        for ep in existing_positions:
            sym = ep.symbol
            sp = prev_positions.get(sym, {})
            cand = next((c for c in candidates if c["symbol"] == sym), None)
            rvol = sp.get("rvol", cand.get("rvol", 0) if cand else 0)
            stop_p, target_p, trail_act_p, trail_p = get_rvol_exit_params(rvol)
            pos = Position(
                symbol=sym, shares=int(float(ep.qty)),
                entry_price=float(ep.avg_entry_price), entry_ts=time.time(),
                open_price=sp.get("open_price", cand.get("open_price", 0) if cand else 0),
                gap_pct=sp.get("gap_pct", cand.get("gap_pct", 0) if cand else 0),
                rvol=rvol,
                signal_type=sp.get("signal_type", "greenbar"),
                highest=float(ep.current_price),
                trail_active=sp.get("trail_active", False),
                stop_pct=sp.get("stop_pct", stop_p),
                target_pct=sp.get("target_pct", target_p),
                trail_activate_pct=sp.get("trail_activate_pct", trail_act_p),
                trail_pct=sp.get("trail_pct", trail_p),
                entry_count=sp.get("entry_count", 1),
            )
            positions.append(pos)
            entry_checked.add(sym)
            # Restore entry tracking so re-entry limits survive restart
            _entry_count_today[sym] = pos.entry_count
        if positions:
            log(f"Restored {len(positions)} existing positions: {[p.symbol for p in positions]}")
        # Also restore exit tracking from previous state
        for sp_sym in prev_positions:
            if sp_sym not in {p.symbol for p in positions}:
                # This symbol was exited before restart -- mark it
                _last_exit_ts[sp_sym] = time.time()  # Approximate
                _entry_count_today[sp_sym] = prev_positions[sp_sym].get("entry_count", 1)
    except Exception as e:
        log(f"Could not restore positions: {e}")

    entry_rejected = set()  # Stocks rejected by Alpaca (retry when buying power frees)
    daily_trades = 0
    trades_detail = []
    daily_loss = 0.0
    max_daily_loss = equity * config.MAX_DAILY_LOSS_PCT
    _entry_count_today = {}  # symbol -> count of entries today (for re-entry)
    _last_exit_ts = {}       # symbol -> timestamp of last exit

    force_close_dt = dt.datetime.combine(target_date.date(), _parse_time(config.FORCE_CLOSE_TIME), tzinfo=_EST)
    entry_end_dt = dt.datetime.combine(target_date.date(), _parse_time(config.ENTRY_WINDOW_END), tzinfo=_EST)
    market_close_dt = dt.datetime.combine(target_date.date(), _parse_time(config.MARKET_CLOSE), tzinfo=_EST)

    state = {"version": config.VERSION_SHORT, "data_feed": config.DATA_FEED,
             "ws_connected": True, "daily_trades": 0, "candidates": candidates,
             "positions": [], "trades_detail": []}

    while True:
        now = dt.datetime.now(_EST)
        if now >= market_close_dt:
            break

        if now >= force_close_dt:
            log("Force close time reached!")
            for pos in positions[:]:
                sold, fill = force_sell_position(pos.symbol, pos.shares)
                if sold > 0:
                    pnl = (fill - pos.entry_price) * sold if fill > 0 else 0
                    trades_detail.append({"symbol": pos.symbol, "entry": pos.entry_price, "exit": fill,
                                          "shares": sold, "pnl": round(pnl, 2), "reason": "force_close",
                                          "trade_type": pos.signal_type})
                    daily_trades += 1
                    positions.remove(pos)
                    log(f"Force closed {pos.symbol}, P&L=${pnl:+,.2f}")
            break

        if max_daily_loss > 0:
            # Daily loss circuit breaker: only trigger on realized losses.
            # Unrealized drawdown is normal intra-trade -- positions have their own stop-loss.
            if daily_loss <= -max_daily_loss:
                log(f"Daily realized loss ${daily_loss:,.2f} exceeded limit ${max_daily_loss:,.2f}")
                # Close all positions before stopping
                for pos in positions[:]:
                    sold, fill = force_sell_position(pos.symbol, pos.shares)
                    if sold > 0:
                        pnl = (fill - pos.entry_price) * sold if fill > 0 else 0
                        trades_detail.append({"symbol": pos.symbol, "entry": pos.entry_price, "exit": fill,
                                              "shares": sold, "pnl": round(pnl, 2), "reason": "circuit_breaker",
                                              "trade_type": pos.signal_type})
                        daily_trades += 1
                        log(f"Circuit breaker close {pos.symbol}, P&L=${pnl:+,.2f}")
                state["daily_stopped"] = True
                break

        # Exit monitoring
        for pos in positions[:]:
            detector = _gbar_detectors.get(pos.symbol)
            if detector is None:
                continue

            # Update highest from detector's latest bar high
            with detector._lock:
                if detector._bar_high > pos.highest:
                    pos.highest = detector._bar_high

            # Activate trailing stop if price has moved enough
            if not pos.trail_active:
                if pos.highest >= pos.entry_price * (1 + pos.trail_activate_pct):
                    pos.trail_active = True

            # Check exit conditions via detector
            should_exit, reason = detector.should_exit(
                pos.entry_price, pos.stop_pct, pos.trail_active,
                pos.highest, pos.trail_activate_pct, pos.trail_pct, pos.target_pct
            )

            if not should_exit:
                continue

            sold, fill = place_sell_market(pos.symbol, pos.shares)
            # Retry once if sell failed
            if sold <= 0:
                log(f"SELL failed for {pos.symbol}, retrying in 2s...")
                time.sleep(2)
                sold, fill = place_sell_market(pos.symbol, pos.shares)
            if sold > 0:
                if fill <= 0:
                    fill = detector.latest_price()
                pnl = round((fill - pos.entry_price) * sold, 2)
                trades_detail.append({"symbol": pos.symbol, "entry": pos.entry_price,
                                      "exit": round(fill, 4), "shares": sold, "pnl": pnl,
                                      "reason": reason, "trade_type": pos.signal_type})
                daily_loss += pnl
                daily_trades += 1
                positions.remove(pos)
                _last_exit_ts[pos.symbol] = time.time()
                entry_checked.discard(pos.symbol)
                log(f"EXIT {pos.symbol} {reason} ${fill:.4f}, P&L=${pnl:+,.2f}")

        # Entry monitoring
        if now < entry_end_dt and len(positions) < config.MAX_POSITIONS:
            # Read live buying power from Alpaca before sizing
            live_bp = 0
            try:
                acct_live = trading_client.get_account()
                live_bp = float(acct_live.buying_power)
                equity = float(acct_live.equity)
            except Exception:
                live_bp = equity  # Fallback to cached equity

            # Pre-compute same-tier counts for fair sizing split
            tier_counts = {}
            for c in candidates:
                rvol_c = c.get("rvol", 0)
                tier_key = _get_rvol_tier(rvol_c)[0]
                tier_counts[tier_key] = tier_counts.get(tier_key, 0) + 1

            for c in candidates:
                # Re-check position limit inside loop after each entry
                if len(positions) >= config.MAX_POSITIONS:
                    break
                sym = c["symbol"]
                rvol = c.get("rvol", 0)
                open_price = c["open_price"]

                # Already in position?
                if any(p.symbol == sym for p in positions):
                    continue

                # Re-entry checks with cooldown
                is_reentry = sym in _last_exit_ts
                if is_reentry:
                    # Cooldown check
                    reentry_cd = getattr(config, "GBAR_REENTRY_COOLDOWN_SEC", 60)
                    if time.time() - _last_exit_ts.get(sym, 0) < reentry_cd:
                        continue

                # Daily entry count limit per symbol
                max_entries = getattr(config, "MAX_DAILY_ENTRIES_PER_SYMBOL", 6)
                if _entry_count_today.get(sym, 0) >= max_entries:
                    continue

                # Daily trades limit
                if config.MAX_DAILY_TRADES > 0 and daily_trades >= config.MAX_DAILY_TRADES:
                    break
                # Daily loss circuit breaker
                if max_daily_loss > 0 and daily_loss <= -max_daily_loss:
                    break

                # Get or create GreenBarDetector for this symbol
                detector = _gbar_detectors.get(sym)
                if detector is None:
                    detector = GreenBarDetector(sym, open_price)
                    _gbar_detectors[sym] = detector

                # Check entry signal
                if not detector.should_enter():
                    continue

                # RVOL-weighted sizing, split evenly among same-tier candidates
                same_tier = tier_counts.get(_get_rvol_tier(rvol)[0], 1)
                slot = max(config.MIN_POSITION_SIZE, get_rvol_sizing(rvol, equity, same_tier_count=same_tier))
                slot = min(slot, live_bp * 0.95)  # Cap to 95% of buying power
                # Use latest market price from detector for sizing
                sizing_price = detector.latest_price()
                if sizing_price <= 0:
                    continue
                shares = int(slot / sizing_price)
                if shares <= 0:
                    continue

                order, _, reject = place_buy_market(sym, shares)
                if order is None:
                    log(f"Entry rejected: {sym} - {reject}")
                    entry_rejected.add(sym)
                    continue
                filled, fill_price = wait_order_filled(str(order.id), timeout=15)
                if filled <= 0:
                    entry_checked.add(sym)
                    continue
                if fill_price <= 0:
                    fill_price = detector.latest_price()

                # Get adaptive exit params
                stop_p, target_p, trail_act_p, trail_p = get_rvol_exit_params(rvol)
                # Signal type: "greenbar" for first entry, "greenbar_re" for re-entry
                sig_label = "greenbar_re" if is_reentry else "greenbar"
                entry_count = _entry_count_today.get(sym, 0) + 1
                pos = Position(
                    symbol=sym, shares=filled, entry_price=fill_price,
                    entry_ts=time.time(), open_price=open_price,
                    gap_pct=c["gap_pct"], rvol=rvol, signal_type=sig_label,
                    highest=fill_price, trail_active=False,
                    stop_pct=stop_p, target_pct=target_p,
                    trail_activate_pct=trail_act_p, trail_pct=trail_p,
                    entry_count=entry_count,
                )
                positions.append(pos)
                entry_checked.add(sym)
                _entry_count_today[sym] = entry_count
                daily_trades += 1
                live_bp -= fill_price * filled  # Track remaining buying power
                log(f"ENTRY {sym} [{sig_label}] {filled}sh @ ${fill_price:.4f} "
                    f"[RVOL={rvol:.1f}x stop={stop_p:.0%} tgt={target_p:.0%}]")

        # WS health -- restart if not running OR no bars for 60s
        # Add 30s cooldown between restarts to avoid tight loop
        ws_stale = time.time() - _stream_state["last_bar_ts"] > 60
        ws_needs_restart = not _stream_state["running"] or ws_stale
        if ws_needs_restart and time.time() - _stream_state.get("last_restart_ts", 0) > 30:
            if not _stream_state["running"]:
                log("WebSocket: not running, restarting...")
            else:
                log("WebSocket: no bars for 60s, restarting...")
            _stream_state["last_restart_ts"] = time.time()
            try:
                restart_ws_stream(syms)
            except Exception as e:
                log(f"WebSocket restart failed (will retry next cycle): {e}")

        # Save state
        state.update({
            "updated": dt.datetime.now().isoformat(), "ws_connected": _stream_state["running"],
            "daily_trades": daily_trades,
            "positions": [{"symbol": p.symbol, "shares": p.shares, "entry_price": p.entry_price,
                           "signal_type": p.signal_type, "rvol": p.rvol,
                           "open_price": p.open_price, "gap_pct": p.gap_pct,
                           "stop_pct": p.stop_pct, "target_pct": p.target_pct,
                           "trail_activate_pct": p.trail_activate_pct, "trail_pct": p.trail_pct,
                           "highest": p.highest, "trail_active": p.trail_active,
                           "entry_count": p.entry_count} for p in positions],
            "trades_detail": trades_detail,
        })
        save_state(state)
        time.sleep(config.POLL_INTERVAL)

    # End of day
    log("=" * 60)
    log("Trading day complete!")
    final_equity = equity + daily_loss
    log(f"Equity: ${final_equity:,.2f} | Daily P&L: ${daily_loss:+,.2f} | Trades: {daily_trades}")
    log("=" * 60)

    report_dir = os.path.join(_ver_dir, "daily_reports")
    os.makedirs(report_dir, exist_ok=True)
    report = {
        "date": str(target_date.date()), "version": config.VERSION_SHORT,
        "account_equity_start": equity, "account_equity_end": final_equity,
        "daily_pnl": round(daily_loss, 2), "daily_trades": daily_trades,
        "candidate": candidates, "trades": trades_detail,
    }
    with open(os.path.join(report_dir, f"{target_date.date()}.json"), "w") as f:
        json.dump(report, f, indent=2, default=str)
    log(f"Daily report saved")

    state.update({"updated": dt.datetime.now().isoformat(), "daily_trades": daily_trades,
                  "positions": [], "trades_detail": trades_detail})
    save_state(state)


def _wait_until(target_date, target_time):
    target_dt = dt.datetime.combine(target_date.date(), target_time, tzinfo=_EST)
    while dt.datetime.now(_EST) < target_dt:
        time.sleep(60)


def get_next_trading_day():
    now = dt.datetime.now(_EST)
    for delta in range(1, 7):
        candidate = now + dt.timedelta(days=delta)
        if candidate.weekday() < 5:
            return pd.Timestamp(candidate.date(), tz="America/New_York")
    return None


def test_connectivity():
    log("Testing data connectivity...")
    try:
        req = StockBarsRequest(
            symbol_or_symbols="SPY", timeframe=TimeFrame.Day,
            start=pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(days=5),
            end=pd.Timestamp.now(tz="America/New_York"),
            feed=getattr(config, "DATA_FEED_OBJ", DataFeed.SIP),
        )
        bars = data_client.get_stock_bars(req)
        if not bars.df.empty:
            log(f"  SPY bar received, OK!")
        log("Connectivity OK!")
        return True
    except Exception as e:
        log(f"Connectivity failed: {e}")
        return False


def main():
    global _log_file
    _log_file = open(os.path.join(_ver_dir, "getgreenbar.log"), "a")

    log(f"Using {config.DATA_FEED.upper()} data feed")
    log("=" * 60)
    log(f"stonewang GreenBar 1.0 Live Trading -- Green Bar Momentum Strategy")
    log(f"Entry: red->green bar + volume spike (>= {config.GBAR_VOLUME_MULT}x prior) + above open_price")
    log(f"Exit: bar_turned_red / stop_loss / trail_stop / target")
    log(f"Window: {config.ENTRY_WINDOW_START}-{config.ENTRY_WINDOW_END} EST")
    sizing_str = "/".join(f"{p:.0%}" for _, p in config.RVOL_SIZING_TIERS)
    max_entries = getattr(config, "MAX_DAILY_ENTRIES_PER_SYMBOL", 6)
    log(f"Sizing: RVOL-weighted ({sizing_str}) | max {config.MAX_POSITIONS} concurrent | re-entry max {max_entries}/symbol")
    log(f"Re-entry cooldown: {getattr(config, 'GBAR_REENTRY_COOLDOWN_SEC', 60)}s")
    log("=" * 60)

    if not test_connectivity():
        log("Connectivity failed, exiting")
        return

    while True:
        now = dt.datetime.now(_EST)
        if now.weekday() >= 5:
            next_day = get_next_trading_day()
            log(f"Weekend. Next: {next_day.date()}")
            _smart_sleep_until(dt.datetime.combine(next_day.date(), dt.time(9, 15), tzinfo=_EST))
            continue

        market_open = dt.datetime.combine(now.date(), dt.time(9, 30), tzinfo=_EST)
        market_close = dt.datetime.combine(now.date(), dt.time(16, 0), tzinfo=_EST)
        if now >= market_close:
            next_day = get_next_trading_day()
            log(f"Market closed. Next: {next_day.date()}")
            _smart_sleep_until(dt.datetime.combine(next_day.date(), dt.time(9, 20), tzinfo=_EST))
            continue

        pre_open = dt.datetime.combine(now.date(), dt.time(9, 15), tzinfo=_EST)
        if now < pre_open:
            _smart_sleep_until(pre_open)

        target = pd.Timestamp(now.date(), tz="America/New_York")
        run_trading_day(target)

        next_day = get_next_trading_day()
        log(f"Next trading day: {next_day.date()}. Sleeping until 9:15...")
        _smart_sleep_until(dt.datetime.combine(next_day.date(), dt.time(9, 15), tzinfo=_EST))


def _smart_sleep_until(target_time):
    while True:
        remaining = (target_time - dt.datetime.now(_EST)).total_seconds()
        if remaining <= 0:
            break
        if remaining < 120:
            log(f"Starting in {remaining / 60:.1f} min...")
            time.sleep(max(1, remaining - 1))
        else:
            log(f"Next event in {int(remaining / 60)} min, sleeping...")
            time.sleep(600)


if __name__ == "__main__":
    main()
