"""stonewang_daytrade_rtg_3.0 — Cycle-based All-in RTG with 1% Trailing Stop.

Core loop: scan → pick best → all-in buy → 1% trail monitor → force close → scan again
  - Single position at a time (MAX_POSITIONS=1)
  - All-in: buy with ALL available buying power
  - Fixed 1% trailing stop (not progressive, not RVOL-tiered)
  - Force close before next scan cycle
  - Scan every 5 minutes
  - Try top 3 candidates per cycle (by RVOL, min 3.0×)
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
EXCLUDE_SYMBOLS = getattr(config, "EXCLUDE_SYMBOLS", set())
trading_client = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=_ALPACA_PAPER)
data_client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)

_log_file = None
_state_file = os.path.join(_parent_dir, "live_rtg3_state.json")


def is_leveraged_etf(symbol):
    if _LEV_PATTERN.search(symbol):
        return True
    if symbol.endswith(("BULL", "BEAR")):
        return True
    return any(symbol.startswith(p) for p in _LEV_PREFIXES)


_CRYPTO_ETF_NAMES = {"BITX", "BITU", "XRPI", "UXRP", "XRPC", "XRPZ", "BTF", "BTFG",
                      "XRP", "ETHW", "SOLX", "DEFI", "BKCH", "CRPT", "STCE"}
_CRYPTO_ETF_PREFIXES = ("XRP", "BTC", "BIT", "ETH", "SOL", "DOGE", "LTC", "ADA")

def is_crypto_etf(symbol):
    if symbol in _CRYPTO_ETF_NAMES:
        return True
    if any(symbol.startswith(k) and len(symbol) <= 6 for k in _CRYPTO_ETF_PREFIXES):
        return True
    return False


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
    signal_type: str = ""
    highest: float = 0.0
    trail_active: bool = False
    rvol: float = 0.0
    stop_pct: float = 0.0
    target_pct: float = 0.0
    trail_activate_pct: float = 0.0
    trail_pct: float = 0.0


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
    _stream_state["last_bar_ts"] = time.time()
    sym = trade.symbol
    ts = trade.timestamp
    if hasattr(ts, "timestamp"):
        ts = dt.datetime.fromtimestamp(ts.timestamp(), tz=_EST)
    _accumulator.add_trade(sym, float(trade.price), int(trade.size), ts)


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


def place_sell_market(symbol, shares):
    if config.DRY_RUN:
        log(f"[DRY] SELL {symbol} {shares}sh")
        return shares, 0.00
    try:
        orders = trading_client.get_orders()
        for o in orders:
            if o.symbol == symbol and o.side == OrderSide.SELL and o.status == OrderStatus.OPEN:
                try:
                    trading_client.cancel_order_by_id(o.id)
                except Exception:
                    pass
        time.sleep(0.5)
    except Exception:
        pass
    try:
        req = MarketOrderRequest(symbol=symbol, qty=shares, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
        order = trading_client.submit_order(req)
        filled, price = wait_order_filled(str(order.id), timeout=30)
        return filled, price
    except Exception as e:
        log(f"SELL market failed: {symbol} {shares}sh. - {e}")
        try:
            log(f"  Retrying {symbol} via close_position()...")
            trading_client.close_position(symbol_or_asset_id=symbol)
            time.sleep(1)
            for _ in range(30):
                try:
                    pos = trading_client.get_open_position(symbol)
                    remaining = int(float(pos.qty))
                    if remaining <= 0:
                        break
                except Exception:
                    break
            fill_price = 0.0
            try:
                orders = trading_client.get_orders_for_symbol(symbol)
                for o in orders:
                    if o.side == OrderSide.SELL and o.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                        if int(float(o.filled_qty)) >= shares:
                            fill_price = float(o.filled_avg_price or 0)
                            break
            except Exception:
                pass
            return shares, fill_price
        except Exception as e2:
            log(f"SELL close_position also failed: {symbol} - {e2}")
            return 0, 0.0


def place_sell_async(symbol, shares):
    if config.DRY_RUN:
        log(f"[DRY] SELL {symbol} {shares}sh")
        return "dry_run"
    try:
        orders = trading_client.get_orders()
        for o in orders:
            if o.symbol == symbol and o.side == OrderSide.SELL and o.status == OrderStatus.OPEN:
                try:
                    trading_client.cancel_order_by_id(o.id)
                except Exception:
                    pass
    except Exception:
        pass
    try:
        req = MarketOrderRequest(symbol=symbol, qty=shares, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
        order = trading_client.submit_order(req)
        log(f"SELL submitted: {symbol} {shares}sh, order_id={order.id}")
        return str(order.id)
    except Exception as e:
        log(f"SELL market failed: {symbol} {shares}sh. - {e}")
        try:
            trading_client.close_position(symbol_or_asset_id=symbol)
            log(f"  close_position() submitted for {symbol}")
            return f"close_{symbol}"
        except Exception as e2:
            log(f"SELL close_position also failed: {symbol} - {e2}")
            return None


def check_sell_filled(order_id, symbol, shares):
    if order_id == "dry_run":
        return shares, 0.0
    if order_id and order_id.startswith("close_"):
        try:
            pos = trading_client.get_open_position(symbol)
            remaining = int(float(pos.qty))
            sold = shares - remaining
            if sold > 0:
                return sold, float(pos.current_price)
            return 0, 0.0
        except Exception:
            return shares, 0.0
    try:
        order = trading_client.get_order_by_id(str(order_id))
        if order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            return int(float(order.filled_qty)), float(order.filled_avg_price or 0)
        if order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
            return -1, 0.0
        return 0, 0.0
    except Exception:
        return 0, 0.0


def force_sell_position(symbol, shares):
    try:
        orders = trading_client.get_orders()
        for o in orders:
            if o.symbol == symbol and o.side == OrderSide.SELL and o.status == OrderStatus.OPEN:
                try:
                    trading_client.cancel_order_by_id(o.id)
                    log(f"  Cancelled order {o.id} for {symbol} to release locked shares")
                except Exception:
                    pass
        time.sleep(1)
    except Exception:
        pass
    try:
        trading_client.close_position(symbol_or_asset_id=symbol)
        time.sleep(1)
        for _ in range(30):
            try:
                pos = trading_client.get_open_position(symbol)
                remaining = int(float(pos.qty))
                if remaining <= 0:
                    break
            except Exception:
                break
            time.sleep(1)
        fill_price = 0.0
        try:
            cutoff = dt.datetime.now(_EST) - dt.timedelta(minutes=5)
            orders = trading_client.get_orders_for_symbol(symbol)
            for o in orders:
                if o.side == OrderSide.SELL and o.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
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

    symbols = [s for s in symbols if not is_crypto_etf(s)]
    log(f"After crypto ETF filter: {len(symbols)} symbols")

    if EXCLUDE_SYMBOLS:
        before = len(symbols)
        symbols = [s for s in symbols if s not in EXCLUDE_SYMBOLS]
        log(f"After EXCLUDE_SYMBOLS filter: {len(symbols)} symbols (removed {before - len(symbols)})")

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
            for f in futures:
                f.cancel()

    if not all_results:
        log("No gap stocks found")
        return []
    results = pd.concat(all_results, ignore_index=True)

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
        log(f"  {c['symbol']}: gap +{c['gap_pct']:.1%}, RVOL={c['rvol']:.1f}×, open=${c['open_price']:.4f}")
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


def check_rtg_entry(symbol, open_price, bars, after_time=None, min_volume=None):
    if len(bars) < 2:
        return 0.0, False, ""
    if min_volume is None:
        min_volume = config.RTG_MIN_VOLUME
    ew_start_h, ew_start_m = (int(x) for x in config.ENTRY_WINDOW_START.split(":"))
    ew_end_h, ew_end_m = (int(x) for x in config.ENTRY_WINDOW_END.split(":"))
    entry_start = dt.time(ew_start_h, ew_start_m)
    entry_end = dt.time(ew_end_h, ew_end_m)
    for i in range(1, len(bars)):
        bar = bars[i]
        prev = bars[i - 1]
        ts = bar.get("timestamp")
        if ts is None:
            continue
        bar_time = ts.time() if isinstance(ts, dt.datetime) else (ts.time() if hasattr(ts, "time") else None)
        if bar_time is None or not (entry_start <= bar_time <= entry_end):
            continue
        if after_time is not None:
            if isinstance(after_time, float):
                after_dt = dt.datetime.fromtimestamp(after_time, tz=_EST)
            else:
                after_dt = after_time
            if isinstance(ts, dt.datetime):
                if ts < after_dt:
                    continue
            elif hasattr(ts, "time"):
                bar_dt = dt.datetime.combine(after_dt.date(), ts, tzinfo=after_dt.tzinfo)
                if bar_dt < after_dt:
                    continue
        bc = bar["close"]
        bh = bar["high"]
        bv = bar["volume"]
        pv = prev["volume"]
        ph = prev["high"]
        po = prev["open"]
        pc = prev["close"]
        if bc > open_price and pv > 0 and bv >= config.RTG_VOLUME_MULT * pv and bv >= min_volume:
            # Check consecutive green bars before entry (don't chase)
            max_green = getattr(config, "MAX_GREEN_BARS_TO_ENTER", 2)
            consecutive_green = 0
            for j in range(i - 1, -1, -1):
                if bars[j]["close"] > bars[j]["open"]:
                    consecutive_green += 1
                else:
                    break
            if consecutive_green >= max_green:
                continue  # Already riding high, skip this entry
            entry = round(open_price * 1.001, 4) if getattr(config, "RTG_ENTRY_AT_OPEN", True) else round(bc, 4)
            return entry, True, "rtg"
        if pc > po and pv >= config.GAPGO_MIN_FIRST_BAR_VOL and bh > ph and bv >= config.GAPGO_MIN_BREAKOUT_VOL:
            return round(ph, 4), True, "gapgo"
    return 0.0, False, ""


def check_breakout_entry(symbol, bars, min_volume=None):
    """Detect intraday breakout: current bar makes new day high + volume spike.

    Signal: bar.close > all previous bars' highs AND volume >= 1.5x prev bar
    This catches afternoon breakouts that RTG signal misses.
    """
    if not getattr(config, "BREAKOUT_ENABLED", True):
        return 0.0, False, ""
    min_bars = getattr(config, "BREAKOUT_MIN_BARS", 5)
    if len(bars) < min_bars + 1:
        return 0.0, False, ""
    if min_volume is None:
        min_volume = config.RTG_MIN_VOLUME
    vol_mult = getattr(config, "BREAKOUT_VOLUME_MULT", 1.5)
    ew_start_h, ew_start_m = (int(x) for x in config.ENTRY_WINDOW_START.split(":"))
    ew_end_h, ew_end_m = (int(x) for x in config.ENTRY_WINDOW_END.split(":"))
    entry_start = dt.time(ew_start_h, ew_start_m)
    entry_end = dt.time(ew_end_h, ew_end_m)
    # Track day high as we scan forward
    day_high = max(b["high"] for b in bars[:min_bars])
    for i in range(min_bars, len(bars)):
        bar = bars[i]
        prev = bars[i - 1]
        ts = bar.get("timestamp")
        if ts is None:
            continue
        bar_time = ts.time() if isinstance(ts, dt.datetime) else (ts.time() if hasattr(ts, "time") else None)
        if bar_time is None or not (entry_start <= bar_time <= entry_end):
            day_high = max(day_high, bar["high"])
            continue
        bc = bar["close"]
        bv = bar["volume"]
        pv = prev["volume"]
        # Breakout: close exceeds all previous highs + volume spike
        if bc > day_high and pv > 0 and bv >= vol_mult * pv and bv >= min_volume:
            # Check consecutive green bars before entry (don't chase)
            max_green = getattr(config, "MAX_GREEN_BARS_TO_ENTER", 2)
            consecutive_green = 0
            for j in range(i - 1, -1, -1):
                if bars[j]["close"] > bars[j]["open"]:
                    consecutive_green += 1
                else:
                    break
            if consecutive_green >= max_green:
                day_high = max(day_high, bar["high"])
                continue  # Already riding high, skip this entry
            entry = round(bc * 1.001, 4) if getattr(config, "BREAKOUT_ENTRY_AT_CLOSE", True) else round(bc, 4)
            return entry, True, "breakout"
        day_high = max(day_high, bar["high"])
    return 0.0, False, ""


def _parse_time(t_str):
    h, m = (int(x) for x in t_str.split(":"))
    return dt.time(h, m)


# ══════════════════════════════════════════════════════════════════
# RTG 3.0: Cycle-based trading
# ══════════════════════════════════════════════════════════════════

def compute_cycle_times(target_date):
    """Compute scan schedule: every SCAN_INTERVAL_SEC from market open to force close."""
    scan_sec = getattr(config, "SCAN_INTERVAL_SEC", 300)
    mkt_open = dt.datetime.combine(target_date.date(), _parse_time(config.MARKET_OPEN), tzinfo=_EST)
    fc_time = dt.datetime.combine(target_date.date(), _parse_time(config.FORCE_CLOSE_TIME), tzinfo=_EST)
    # Last cycle must start at least 30s before force close
    latest_start = fc_time - dt.timedelta(seconds=30)
    times = []
    t = mkt_open
    while t <= latest_start:
        times.append(t)
        t += dt.timedelta(seconds=scan_sec)
    return times


def select_best_candidates(candidates, max_n=3):
    """Select top N candidates by RVOL, filtered by MIN_RVOL_TO_TRADE."""
    min_rvol = getattr(config, "MIN_RVOL_TO_TRADE", 3.0)
    qualified = [
        c for c in candidates
        if c["rvol"] >= min_rvol
        and c["symbol"] not in EXCLUDE_SYMBOLS
        and not is_crypto_etf(c["symbol"])
        and not is_leveraged_etf(c["symbol"])
    ]
    qualified.sort(key=lambda c: c["rvol"], reverse=True)
    return qualified[:max_n]


def wait_for_entry_signal(symbol, open_price, rvol, deadline):
    """Poll 1-min bars for entry signal (RTG or Breakout) until deadline.
    Returns (entry_price, True, signal_type) on signal, or (0, False, "") on timeout."""
    while dt.datetime.now(_EST) < deadline:
        bars = _accumulator.get_1min_bars(symbol)
        # RVOL-adaptive min volume
        min_vol = config.RTG_MIN_VOLUME
        if rvol >= 10:
            min_vol = max(config.RTG_MIN_VOLUME // 3, 5000)
        elif rvol >= 5:
            min_vol = max(config.RTG_MIN_VOLUME // 2, 10000)

        # Check RTG signal first (opening drive)
        entry_price, confirmed, signal_type = check_rtg_entry(
            symbol, open_price, bars, min_volume=min_vol)
        if confirmed and entry_price > 0:
            return entry_price, True, signal_type

        # Check breakout signal (intraday momentum)
        entry_price, confirmed, signal_type = check_breakout_entry(
            symbol, bars, min_volume=min_vol)
        if confirmed and entry_price > 0:
            return entry_price, True, signal_type

        # WS health check
        ws_stale = time.time() - _stream_state["last_bar_ts"] > 60
        if (not _stream_state["running"] or ws_stale) and \
           time.time() - _stream_state.get("last_restart_ts", 0) > 30:
            _stream_state["last_restart_ts"] = time.time()
            restart_ws_stream([symbol])

        time.sleep(config.POLL_INTERVAL)

    return 0.0, False, ""


def monitor_position(position, force_close_at):
    """Monitor single position with bar-based exit rules.
    Exit rules (priority order):
      1. First bar after entry is red → red_bar_exit
      2. Green-to-red transition → green_to_red
      3. 3 consecutive green bars → three_green_bars
      4. 3% hard stop (backstop for extreme moves)
      5. 50% target (safety valve)
    Returns (reason, fill_price) on exit, or None if still open at force_close_at."""
    _pending_sell = None
    _last_bar_ts = None  # Track which bar we've already processed
    _is_first_bar = True  # Is this the first completed bar after entry?
    _green_bar_count = 0  # Consecutive green bars counter

    while dt.datetime.now(_EST) < force_close_at:
        # Check pending async sell
        if _pending_sell:
            order_id = _pending_sell["order_id"]
            reason = _pending_sell["reason"]
            submit_time = _pending_sell["submit_time"]

            if time.time() - submit_time > 60:
                log(f"SELL timeout for {position.symbol}, retrying...")
                _pending_sell = None
            else:
                filled, fill_price = check_sell_filled(order_id, position.symbol, position.shares)
                if filled > 0:
                    if fill_price <= 0:
                        bars = _accumulator.get_1min_bars(position.symbol)
                        fill_price = float(bars[-1]["close"]) if bars else position.entry_price
                    return reason, fill_price
                elif filled < 0:
                    _pending_sell = None
                    log(f"SELL order failed for {position.symbol}, will retry")
                else:
                    time.sleep(config.POLL_INTERVAL)
                    continue

        # Get latest bar
        bars = _accumulator.get_1min_bars(position.symbol)
        if not bars:
            time.sleep(config.POLL_INTERVAL)
            continue

        latest = bars[-1]
        bar_ts = latest.get("timestamp")

        # Only process completed bars (skip if same bar as last check)
        if bar_ts is not None and bar_ts == _last_bar_ts:
            time.sleep(config.POLL_INTERVAL)
            continue

        bar_open = latest["open"]
        bar_close = latest["close"]
        bar_low = latest["low"]
        bar_high = latest["high"]
        is_green = bar_close >= bar_open  # Green bar (close >= open)
        is_red = not is_green  # Red bar (close < open)

        if bar_ts is not None:
            _last_bar_ts = bar_ts

        # Update highest
        if bar_high > position.highest:
            position.highest = bar_high

        reason = None

        # Exit rule 1: First bar after entry is red → immediate exit
        if _is_first_bar and is_red and getattr(config, "EXIT_ON_RED_BAR", True):
            reason = "red_bar_exit"
            log(f"First bar is RED for {position.symbol} (o=${bar_open:.4f} c=${bar_close:.4f})")

        # Exit rule 2: Green-to-red transition → sell
        if reason is None and not _is_first_bar and is_red and getattr(config, "EXIT_ON_GREEN_TO_RED", True):
            reason = "green_to_red"
            log(f"Green→Red for {position.symbol} (o=${bar_open:.4f} c=${bar_close:.4f})")

        # Exit rule 3: 3 consecutive green bars → sell
        if reason is None and is_green and getattr(config, "EXIT_ON_THREE_GREEN", True):
            _green_bar_count += 1
            if _green_bar_count >= 3:
                reason = "three_green_bars"
                log(f"3 green bars for {position.symbol} (count={_green_bar_count})")
        elif reason is None and is_red:
            _green_bar_count = 0  # Reset on red bar

        # Exit rule 4: Hard stop loss (3% backstop)
        if reason is None:
            stop_price = round(position.entry_price * (1 - position.stop_pct), 4)
            if bar_low <= stop_price:
                reason = "stop_loss"

        # Exit rule 5: Target (safety valve)
        if reason is None:
            target_price = round(position.entry_price * (1 + position.target_pct), 4)
            if bar_high >= target_price:
                reason = "target"

        # Mark first bar as processed
        if _is_first_bar:
            _is_first_bar = False

        if reason is not None and _pending_sell is None:
            order_id = place_sell_async(position.symbol, position.shares)
            if order_id:
                _pending_sell = {"order_id": order_id, "reason": reason, "submit_time": time.time()}
                log(f"Async SELL submitted for {position.symbol} ({reason}), order={order_id}")
            else:
                log(f"SELL stuck for {position.symbol}, retrying next cycle")

        # WS health
        ws_stale = time.time() - _stream_state["last_bar_ts"] > 60
        if (not _stream_state["running"] or ws_stale) and \
           time.time() - _stream_state.get("last_restart_ts", 0) > 30:
            _stream_state["last_restart_ts"] = time.time()
            restart_ws_stream([position.symbol])

        time.sleep(config.POLL_INTERVAL)

    # force_close_at reached — return None so caller force-closes
    return None


def handle_orphan_position():
    """At startup, force-close any position from a previous cycle/day that survived a crash."""
    try:
        alpaca_pos = trading_client.get_all_positions()
        for ap in alpaca_pos:
            sym = ap.symbol
            if sym in EXCLUDE_SYMBOLS:
                continue
            qty = int(float(ap.qty))
            log(f"Found orphan position {sym} {qty}sh from previous run, force closing")
            sold, fill = force_sell_position(sym, qty)
            if sold > 0:
                pnl = (fill - float(ap.avg_entry_price)) * sold if fill > 0 else 0
                log(f"Orphan closed: {sym}, P&L=${pnl:+,.2f}")
            else:
                log(f"CRITICAL: Failed to close orphan {sym}!")
    except Exception as e:
        log(f"Orphan check error: {e}")


def run_trading_day(target_date):
    log(f"Starting rtg_3.0 trading day: {target_date.date()}")
    try:
        acct = trading_client.get_account()
        equity = float(acct.equity)
    except Exception:
        equity = config.INITIAL_CAPITAL
    log(f"Account equity: ${equity:,.2f}")

    # Force-close any orphan positions from previous run/crash
    handle_orphan_position()

    force_close_dt = dt.datetime.combine(target_date.date(), _parse_time(config.FORCE_CLOSE_TIME), tzinfo=_EST)
    market_close_dt = dt.datetime.combine(target_date.date(), _parse_time(config.MARKET_CLOSE), tzinfo=_EST)
    # Entry deadline: 30s before force close
    entry_deadline_dt = force_close_dt - dt.timedelta(seconds=30)

    position = None
    daily_trades = 0
    daily_pnl = 0.0
    trades_detail = []
    candidates = []
    max_daily_loss = equity * config.MAX_DAILY_LOSS_PCT
    scan_count = 0

    state = {
        "version": config.VERSION_SHORT, "data_feed": config.DATA_FEED,
        "ws_connected": True, "daily_trades": 0,
        "cycle_index": 0, "next_scan_time": None,
        "current_cycle": None, "candidates": [],
        "position": None, "trades_detail": [],
    }

    # ── Event-driven main loop ──
    while dt.datetime.now(_EST) < force_close_dt:
        now = dt.datetime.now(_EST)

        # Daily loss circuit breaker
        if max_daily_loss > 0 and daily_pnl <= -max_daily_loss:
            log(f"Daily loss ${daily_pnl:,.2f} exceeded limit ${max_daily_loss:,.2f}, stopping")
            state["daily_stopped"] = True
            break

        if position is not None:
            # ── HAVE POSITION: monitor until exit ──
            force_close_start = force_close_dt - dt.timedelta(seconds=30)
            exit_result = monitor_position(position, force_close_start)

            if exit_result:
                reason, fill_price = exit_result
                pnl = round((fill_price - position.entry_price) * position.shares, 2)
                hold_dur = round(time.time() - position.entry_ts)
                trades_detail.append({
                    "cycle_index": scan_count, "symbol": position.symbol,
                    "entry": position.entry_price, "exit": round(fill_price, 4),
                    "shares": position.shares, "pnl": pnl,
                    "reason": reason, "trade_type": position.signal_type,
                    "hold_duration_sec": hold_dur,
                })
                daily_pnl += pnl
                log(f"EXIT {position.symbol} {reason} @ ${fill_price:.4f}, P&L=${pnl:+,.2f} (held {hold_dur}s)")
                position = None
                # Immediately continue → will scan again in next iteration
            else:
                # monitor_position returned None → force close time reached
                log("Force close time reached during monitoring")
                break

            # Save state after exit
            state.update({
                "updated": dt.datetime.now().isoformat(),
                "ws_connected": _stream_state["running"],
                "daily_trades": daily_trades,
                "cycle_index": scan_count,
                "position": None,
                "trades_detail": trades_detail,
            })
            save_state(state)
            continue

        # ── NO POSITION: scan → select → wait signal → buy ──
        # Check if we still have time to enter
        if dt.datetime.now(_EST) >= entry_deadline_dt:
            log("Too late for new entries today")
            break

        scan_count += 1
        log(f"═══ Scan #{scan_count} at {now.strftime('%H:%M:%S')} ═══")

        # Scan for gap stocks
        try:
            candidates = scan_gaps(target_date)
        except Exception as e:
            log(f"Scan error: {e}")
            candidates = []

        best_candidates = select_best_candidates(
            candidates, max_n=getattr(config, "MAX_ENTRY_ATTEMPTS", 3))

        if not best_candidates:
            if candidates:
                best_rvol = max(c["rvol"] for c in candidates)
                log(f"No qualified candidates (max RVOL={best_rvol:.1f} < {config.MIN_RVOL_TO_TRADE})")
            else:
                log(f"No gap stocks found in scan")
            state["current_cycle"] = {"scan_time": now.isoformat(), "best_candidate": None,
                                       "qualified_count": 0, "skipped_reason": "no_qualified"}
            save_state(state)
            # Wait before re-scanning
            time.sleep(config.SCAN_INTERVAL_SEC)
            continue

        log(f"Top candidate: {best_candidates[0]['symbol']} (RVOL={best_candidates[0]['rvol']:.1f}×, "
            f"gap=+{best_candidates[0]['gap_pct']:.1%})")

        # Try entry for best candidates
        # Deadline: min of entry_deadline_dt and 5 min from now (don't wait forever per scan)
        signal_deadline = min(
            entry_deadline_dt,
            now + dt.timedelta(seconds=config.SCAN_INTERVAL_SEC),
        )

        entered = False
        for cand in best_candidates:
            if dt.datetime.now(_EST) >= entry_deadline_dt:
                break

            sym = cand["symbol"]
            open_price = cand["open_price"]
            rvol = cand["rvol"]

            log(f"Watching {sym} for entry signal (RVOL={rvol:.1f}×)")

            # Start WS + backfill for this symbol
            restart_ws_stream([sym])
            backfill_1min_bars([sym], target_date)
            time.sleep(2)

            # Wait for entry signal (RTG or Breakout)
            entry_price, confirmed, signal_type = wait_for_entry_signal(
                sym, open_price, rvol, signal_deadline)

            if not confirmed or entry_price <= 0:
                log(f"No entry signal for {sym}")
                continue

            # ── ALL-IN BUY ──
            try:
                acct_live = trading_client.get_account()
                buying_power = float(acct_live.buying_power)
                equity = float(acct_live.equity)
            except Exception:
                buying_power = equity

            all_in_ratio = getattr(config, "ALL_IN_BP_RATIO", 0.95)
            capital = buying_power * all_in_ratio
            shares = int(capital / entry_price)
            if shares <= 0:
                log(f"No buying power for {sym} (bp=${buying_power:.2f})")
                continue

            order, _, reject = place_buy_market(sym, shares)
            if order is None:
                log(f"BUY rejected: {sym} - {reject}")
                continue

            filled, fill_price = wait_order_filled(str(order.id), timeout=15)
            if filled <= 0:
                log(f"BUY not filled: {sym}")
                continue

            if fill_price <= 0:
                fill_price = entry_price

            # Check slippage
            slippage = abs(fill_price - entry_price) / entry_price
            if slippage > 0.02:
                log(f"Excessive slippage {slippage:.2%}, selling immediately")
                place_sell_market(sym, filled)
                continue

            # Position opened!
            position = Position(
                symbol=sym, shares=filled, entry_price=fill_price,
                entry_ts=time.time(), open_price=open_price,
                gap_pct=cand["gap_pct"], signal_type=signal_type,
                highest=fill_price, trail_active=False,
                rvol=rvol,
                stop_pct=config.STOP_PCT,
                target_pct=config.TARGET_PCT,
                trail_activate_pct=config.TRAIL_ACTIVATE_PCT,
                trail_pct=config.TRAIL_PCT,
            )
            entered = True
            log(f"ENTRY {sym} [{signal_type}] {filled}sh @ ${fill_price:.4f} "
                f"[ALL-IN ${capital:.2f}, RVOL={rvol:.1f}×]")
            break  # Don't try more candidates

        if not entered:
            log(f"No entry this scan")
            state["current_cycle"] = {
                "scan_time": now.isoformat(),
                "best_candidate": best_candidates[0] if best_candidates else None,
                "qualified_count": len(best_candidates),
                "skipped_reason": "no_signal",
            }
            state.update({
                "candidates": candidates,
                "position": None,
            })
            save_state(state)
            # Wait before re-scanning
            time.sleep(config.SCAN_INTERVAL_SEC)
            continue

        # Save state after entry
        state.update({
            "updated": dt.datetime.now().isoformat(),
            "ws_connected": _stream_state["running"],
            "daily_trades": daily_trades,
            "cycle_index": scan_count,
            "candidates": candidates,
            "position": {
                "symbol": position.symbol, "shares": position.shares,
                "entry_price": position.entry_price, "signal_type": position.signal_type,
                "rvol": position.rvol, "open_price": position.open_price,
                "gap_pct": position.gap_pct, "highest": position.highest,
                "trail_active": position.trail_active,
                "stop_pct": position.stop_pct, "target_pct": position.target_pct,
                "trail_activate_pct": position.trail_activate_pct, "trail_pct": position.trail_pct,
            },
            "trades_detail": trades_detail,
        })
        save_state(state)

    # ── End of day: force close any remaining position ──
    if position is not None:
        log("EOD: Force closing remaining position")
        sold, fill = force_sell_position(position.symbol, position.shares)
        if sold > 0:
            pnl = (fill - position.entry_price) * sold if fill > 0 else 0
            trades_detail.append({
                "cycle_index": -1, "symbol": position.symbol,
                "entry": position.entry_price, "exit": round(fill, 4),
                "shares": sold, "pnl": round(pnl, 2),
                "reason": "eod_force_close", "trade_type": position.signal_type,
                "hold_duration_sec": round(time.time() - position.entry_ts),
            })
            daily_pnl += pnl
            daily_trades += 1
            position = None

    # Save daily report
    log("=" * 60)
    log(f"Trading day complete!")
    final_equity = equity + daily_pnl
    log(f"Equity: ${final_equity:,.2f} | Daily P&L: ${daily_pnl:+,.2f} | Trades: {daily_trades}")
    log("=" * 60)

    report_dir = os.path.join(_ver_dir, "daily_reports")
    os.makedirs(report_dir, exist_ok=True)
    report = {
        "date": str(target_date.date()), "version": config.VERSION_SHORT,
        "account_equity_start": equity, "account_equity_end": final_equity,
        "daily_pnl": round(daily_pnl, 2), "daily_trades": daily_trades,
        "candidates": candidates, "trades": trades_detail,
    }
    with open(os.path.join(report_dir, f"{target_date.date()}.json"), "w") as f:
        json.dump(report, f, indent=2, default=str)
    log(f"Daily report saved")

    state.update({
        "updated": dt.datetime.now().isoformat(),
        "daily_trades": daily_trades,
        "position": None, "trades_detail": trades_detail,
    })
    save_state(state)


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


def main():
    global _log_file
    _log_file = open(os.path.join(_ver_dir, "live_rtg3.log"), "a")

    log(f"Using {config.DATA_FEED.upper()} data feed")
    log("=" * 60)
    log(f"stonewang RTG 3.0 Live Trading — Cycle-based All-in with 1% Trailing Stop")
    log(f"Entry: RTG (vol >= {config.RTG_VOLUME_MULT}x prior) + Breakout (new high + vol) / GapGo DISABLED")
    log(f"Exit: 1% trailing stop (fixed) | 3% hard stop backstop")
    log(f"Window: {config.ENTRY_WINDOW_START}-{config.ENTRY_WINDOW_END} EST")
    log(f"Sizing: ALL-IN ({getattr(config, 'ALL_IN_BP_RATIO', 0.95):.0%} of BP) | 1 position max")
    log(f"Scan: every {config.SCAN_INTERVAL_SEC}s | Min RVOL: {getattr(config, 'MIN_RVOL_TO_TRADE', 3.0):.1f}×")
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


if __name__ == "__main__":
    main()
