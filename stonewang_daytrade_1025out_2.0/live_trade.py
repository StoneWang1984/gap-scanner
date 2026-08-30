"""stonewang_daytrade_1025out_2.0 — RTG + RVOL-Adaptive Stop + 10:25 Exit Live Trading.

Improvements over 1.0:
  - MIN_RVOL_TO_TRADE = 3x: filter out low-RVOL stocks (no momentum)
  - RVOL-adaptive stop loss: high RVOL → 7% stop, medium → 5%, low → 3%
  - Entry confirmation: require bar gain >= 1% above open_price
  - 10:25 exit captures opening drive (same as 1.0)

Strategy:
  - Pre-market scan for gap-up stocks, rank by RVOL, select top 40
  - Entry: RTG signal (close > open*(1+1%) + vol >= 1.5x prior) in 09:30-10:24
  - Exit: 10:25 EST market sell (time-based) or RVOL-adaptive stop loss
  - No trailing stop, no target — 10:25 exit captures opening drive
  - Position: RVOL-weighted, max 8 concurrent
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
_state_file = os.path.join(_parent_dir, "live_state.json")


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


def _get_rvol_tier(rvol):
    tiers = getattr(config, "RVOL_SIZING_TIERS", [(10.0, 0.50), (5.0, 0.30), (0.0, 0.15)])
    for rvol_min, pct in tiers:
        if rvol >= rvol_min:
            return rvol_min, pct
    return 0.0, 0.15


def get_rvol_sizing(rvol, equity, same_tier_count=1):
    _, pct = _get_rvol_tier(rvol)
    split_pct = pct / max(same_tier_count, 1)
    return round(equity * split_pct, 2)


def _get_rvol_exit_tier(rvol):
    """Get adaptive stop/target based on RVOL tier. Returns (stop_pct, target_pct)."""
    tiers = getattr(config, "RVOL_EXIT_TIERS", [
        (10.0, 0.07, 0.50),
        (5.0,  0.05, 0.30),
        (0.0,  0.03, 0.15),
    ])
    for rvol_min, stop, target in tiers:
        if rvol >= rvol_min:
            return stop, target
    return 0.03, 0.15


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
    rvol: float = 0.0
    stop_pct: float = 0.03  # RVOL-adaptive (set at entry time)


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
        time.sleep(1)
    except Exception:
        pass
    try:
        req = MarketOrderRequest(symbol=symbol, qty=shares, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
        order = trading_client.submit_order(req)
        filled, price = wait_order_filled(str(order.id), timeout=30)
        return filled, price
    except Exception as e:
        log(f"SELL failed: {symbol} {shares}sh. - {e}")
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


def check_rtg_entry(symbol, open_price, bars, after_time=None, min_volume=None):
    if len(bars) < 2:
        return 0.0, False, ""
    if min_volume is None:
        min_volume = config.RTG_MIN_VOLUME
    min_bar_gain_pct = getattr(config, "RTG_MIN_BAR_GAIN_PCT", 0.0)
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
        if (bc > open_price
                and bc >= open_price * (1 + min_bar_gain_pct)
                and pv > 0 and bv >= config.RTG_VOLUME_MULT * pv and bv >= min_volume):
            entry = round(open_price * 1.001, 4) if getattr(config, "RTG_ENTRY_AT_OPEN", True) else round(bc, 4)
            return entry, True, "rtg"
        if pc > po and pv >= config.GAPGO_MIN_FIRST_BAR_VOL and bh > ph and bv >= config.GAPGO_MIN_BREAKOUT_VOL:
            return round(ph, 4), True, "gapgo"
    return 0.0, False, ""


def _parse_time(t_str):
    h, m = (int(x) for x in t_str.split(":"))
    return dt.time(h, m)


def run_trading_day(target_date):
    log(f"Starting trading day: {target_date.date()} (exit at {config.EXIT_TIME}, RVOL-adaptive stop)")
    try:
        acct = trading_client.get_account()
        equity = float(acct.equity)
    except Exception:
        equity = config.INITIAL_CAPITAL
    log(f"Account equity: ${equity:,.2f}")

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
    candidates = scan_gaps(target_date)
    # Filter by MIN_RVOL_TO_TRADE
    min_rvol = getattr(config, "MIN_RVOL_TO_TRADE", 3.0)
    if candidates:
        pre_filter = len(candidates)
        candidates = [c for c in candidates if c.get("rvol", 0) >= min_rvol]
        filtered = pre_filter - len(candidates)
        if filtered > 0:
            log(f"MIN_RVOL filter: removed {filtered} with RVOL < {min_rvol}x, {len(candidates)} remain")
    if not candidates:
        if prev_state_for_restart.get("candidates"):
            candidates = prev_state_for_restart["candidates"]
            log(f"Scan found 0 candidates, using {len(candidates)} from previous state")

    if not candidates:
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
    restart_ws_stream(syms)

    positions = []
    entry_checked = set()
    entry_count = {}

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
            if sym in EXCLUDE_SYMBOLS:
                log(f"Skip {sym} — in EXCLUDE_SYMBOLS (managed externally)")
                continue
            sp = prev_positions.get(sym, {})
            cand = next((c for c in candidates if c["symbol"] == sym), None)
            rvol = sp.get("rvol", cand.get("rvol", 0) if cand else 0)
            stop_pct, _ = _get_rvol_exit_tier(rvol)
            pos = Position(
                symbol=sym, shares=int(float(ep.qty)),
                entry_price=float(ep.avg_entry_price), entry_ts=time.time(),
                open_price=sp.get("open_price", cand.get("open_price", 0) if cand else 0),
                gap_pct=sp.get("gap_pct", cand.get("gap_pct", 0) if cand else 0),
                signal_type=sp.get("signal_type", "rtg"),
                highest=float(ep.current_price),
                rvol=rvol,
                stop_pct=sp.get("stop_pct", stop_pct),
            )
            positions.append(pos)
            entry_checked.add(sym)
            entry_count[sym] = entry_count.get(sym, 0) + 1
        if positions:
            log(f"Restored {len(positions)} existing positions: {[p.symbol for p in positions]}")
        for sp_sym in prev_positions:
            if sp_sym not in {p.symbol for p in positions}:
                _last_exit_ts[sp_sym] = time.time()
                entry_count[sp_sym] = entry_count.get(sp_sym, 0) + 1
    except Exception as e:
        log(f"Could not restore positions: {e}")
    entry_rejected = set()
    daily_trades = 0
    trades_detail = []
    daily_loss = 0.0
    max_daily_loss = equity * config.MAX_DAILY_LOSS_PCT
    _last_exit_ts = {}
    _stop_exit_ts = {}
    _sell_stuck_until = {}

    force_close_dt = dt.datetime.combine(target_date.date(), _parse_time(config.FORCE_CLOSE_TIME), tzinfo=_EST)
    entry_end_dt = dt.datetime.combine(target_date.date(), _parse_time(config.ENTRY_WINDOW_END), tzinfo=_EST)
    entry_start_dt = dt.datetime.combine(target_date.date(), _parse_time(config.ENTRY_WINDOW_START), tzinfo=_EST)
    market_close_dt = dt.datetime.combine(target_date.date(), _parse_time(config.MARKET_CLOSE), tzinfo=_EST)

    # 10:25 exit time
    exit_h, exit_m = (int(x) for x in config.EXIT_TIME.split(":"))
    exit_time = dt.time(exit_h, exit_m)

    state = {"version": config.VERSION_SHORT, "data_feed": config.DATA_FEED,
             "ws_connected": True, "daily_trades": 0, "candidates": candidates,
             "positions": [], "trades_detail": []}

    while True:
        now = dt.datetime.now(_EST)
        if now >= market_close_dt:
            break

        if now >= force_close_dt:
            log("Exit time reached — closing all positions!")
            for pos in positions[:]:
                sold, fill = force_sell_position(pos.symbol, pos.shares)
                if sold > 0:
                    if fill <= 0:
                        try:
                            snap = trading_client.get_open_position(pos.symbol)
                            fill = float(snap.current_price) if snap else 0
                        except Exception:
                            pass
                    if fill <= 0:
                        bars_tmp = _accumulator.get_1min_bars(pos.symbol)
                        fill = bars_tmp[-1]["close"] if bars_tmp else 0
                    pnl = (fill - pos.entry_price) * sold if fill > 0 else 0
                    trades_detail.append({"symbol": pos.symbol, "entry": pos.entry_price, "exit": round(fill, 4),
                                          "shares": sold, "pnl": round(pnl, 2), "reason": "10:25_exit",
                                          "trade_type": pos.signal_type})
                    daily_trades += 1
                    positions.remove(pos)
                    log(f"10:25 EXIT {pos.symbol}, P&L=${pnl:+,.2f}")
            # Also close any orphan positions in Alpaca not in tracked list
            try:
                alpaca_pos = trading_client.get_all_positions()
                tracked_syms = {p.symbol for p in positions}
                for ap in alpaca_pos:
                    if ap.symbol not in tracked_syms and ap.symbol not in EXCLUDE_SYMBOLS:
                        log(f"Force close orphan: {ap.symbol} {int(float(ap.qty))}sh")
                        force_sell_position(ap.symbol, int(float(ap.qty)))
            except Exception as e:
                log(f"Orphan close error: {e}")
            break

        if max_daily_loss > 0:
            if daily_loss <= -max_daily_loss:
                log(f"Daily realized loss ${daily_loss:,.2f} exceeded limit ${max_daily_loss:,.2f}")
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

        # Exit monitoring: 3% stop loss or 10:25 time-based exit
        for pos in positions[:]:
            if _sell_stuck_until.get(pos.symbol, 0) > time.time():
                continue
            bars = _accumulator.get_1min_bars(pos.symbol)
            if not bars:
                continue
            latest = bars[-1]
            bar_low = latest["low"]
            bar_high = latest["high"]
            cur_price = latest["close"]
            if bar_high > pos.highest:
                pos.highest = bar_high

            # Get bar timestamp for 10:25 check
            bar_ts = latest.get("timestamp")
            bar_time = None
            if bar_ts is not None:
                bar_time = bar_ts.time() if isinstance(bar_ts, dt.datetime) else (bar_ts.time() if hasattr(bar_ts, "time") else None)

            stop_price = round(pos.entry_price * (1 - pos.stop_pct), 4)
            reason = None

            # 1. Stop loss check (RVOL-adaptive)
            if bar_low <= stop_price:
                reason = "stop_loss"
            # 2. 10:25 time-based exit
            elif bar_time is not None and bar_time >= exit_time:
                reason = "10:25_exit"

            if reason is None:
                continue

            sold, fill = place_sell_market(pos.symbol, pos.shares)
            if sold <= 0:
                log(f"SELL failed for {pos.symbol}, retrying in 2s...")
                time.sleep(2)
                sold, fill = place_sell_market(pos.symbol, pos.shares)
            if sold <= 0:
                _sell_stuck_until[pos.symbol] = time.time() + 60
                log(f"SELL stuck for {pos.symbol} (locked shares), throttling 60s")
                continue
            if fill <= 0:
                fill = cur_price
            pnl = round((fill - pos.entry_price) * sold, 2)
            trades_detail.append({"symbol": pos.symbol, "entry": pos.entry_price,
                                  "exit": round(fill, 4), "shares": sold, "pnl": pnl,
                                  "reason": reason, "trade_type": pos.signal_type})
            daily_loss += pnl
            daily_trades += 1
            positions.remove(pos)
            _last_exit_ts[pos.symbol] = time.time()
            entry_checked.discard(pos.symbol)
            if reason == "stop_loss":
                _stop_exit_ts[pos.symbol] = time.time()
            log(f"EXIT {pos.symbol} {reason} ${fill:.4f}, P&L=${pnl:+,.2f}")

        # Entry monitoring (09:30-09:59)
        if entry_start_dt <= now < entry_end_dt and len(positions) < config.MAX_POSITIONS:
            live_bp = 0
            try:
                acct_live = trading_client.get_account()
                live_bp = float(acct_live.buying_power)
                equity = float(acct_live.equity)
            except Exception:
                live_bp = equity

            tier_counts = {}
            for c in candidates:
                rvol_c = c.get("rvol", 0)
                tier_key = _get_rvol_tier(rvol_c)[0]
                tier_counts[tier_key] = tier_counts.get(tier_key, 0) + 1

            for c in candidates:
                if len(positions) >= config.MAX_POSITIONS:
                    break
                sym = c["symbol"]
                rvol = c.get("rvol", 0)
                if any(p.symbol == sym for p in positions):
                    continue
                is_reentry = sym in _last_exit_ts
                if is_reentry and not config.RTG_REENTRY_ALLOWED:
                    continue
                if is_reentry and sym in _stop_exit_ts:
                    continue
                if is_reentry and entry_count.get(sym, 0) > config.RTG_REENTRY_MAX:
                    continue
                if sym in EXCLUDE_SYMBOLS:
                    entry_checked.add(sym)
                    continue
                if is_crypto_etf(sym):
                    entry_checked.add(sym)
                    continue
                reentry_cd = getattr(config, "REENTRY_COOLDOWN_SEC", 120)
                if is_reentry and time.time() - _last_exit_ts.get(sym, 0) < reentry_cd:
                    continue
                after_time = _last_exit_ts.get(sym) if is_reentry else None
                if config.MAX_DAILY_TRADES > 0 and daily_trades >= config.MAX_DAILY_TRADES:
                    break
                if max_daily_loss > 0 and daily_loss <= -max_daily_loss:
                    break
                open_price = c["open_price"]
                bars = _accumulator.get_1min_bars(sym)
                min_vol = config.RTG_MIN_VOLUME
                if rvol >= 10:
                    min_vol = max(config.RTG_MIN_VOLUME // 3, 5000)
                elif rvol >= 5:
                    min_vol = max(config.RTG_MIN_VOLUME // 2, 10000)
                entry_price, confirmed, signal_type = check_rtg_entry(sym, open_price, bars, after_time=after_time, min_volume=min_vol)
                if not confirmed or entry_price <= 0:
                    continue
                if is_reentry:
                    max_reentry_price = open_price * getattr(config, "REENTRY_MAX_PRICE_VS_OPEN", 1.15)
                    if entry_price > max_reentry_price:
                        continue
                    min_pullback = getattr(config, "REENTRY_MIN_PULLBACK", 0.03)
                    if bars:
                        day_high = max(b["high"] for b in bars)
                        if entry_price > day_high * (1 - min_pullback):
                            continue
                same_tier = tier_counts.get(_get_rvol_tier(rvol)[0], 1)
                slot = max(config.MIN_POSITION_SIZE, get_rvol_sizing(rvol, equity, same_tier_count=same_tier))
                slot = min(slot, live_bp * 0.95)
                latest_bar = _accumulator.get_1min_bars(sym)
                sizing_price = latest_bar[-1]["close"] if latest_bar else entry_price
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
                    fill_price = entry_price
                sig_label = signal_type + ("_re" if is_reentry else "")
                stop_pct, _ = _get_rvol_exit_tier(rvol)
                pos = Position(symbol=sym, shares=filled, entry_price=fill_price,
                               entry_ts=time.time(), open_price=open_price,
                               gap_pct=c["gap_pct"], signal_type=sig_label, highest=fill_price,
                               rvol=rvol, stop_pct=stop_pct)
                positions.append(pos)
                entry_checked.add(sym)
                entry_count[sym] = entry_count.get(sym, 0) + 1
                daily_trades += 1
                live_bp -= fill_price * filled
                log(f"ENTRY {sym} [{sig_label}] {filled}sh @ ${fill_price:.4f} "
                    f"[RVOL={rvol:.1f}x stop={stop_pct:.0%}]")

        # WS health check
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
                           "stop_pct": p.stop_pct, "highest": p.highest} for p in positions],
            "trades_detail": trades_detail,
        })
        save_state(state)
        time.sleep(config.POLL_INTERVAL)

    # End of day (for 1025out, this is after 10:25)
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
        "candidates": candidates, "trades": trades_detail,
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
    _log_file = open(os.path.join(_ver_dir, "live_1025out.log"), "a")

    log(f"Using {config.DATA_FEED.upper()} data feed")
    log("=" * 60)
    log(f"stonewang 1025out 2.0 Live Trading — RTG + RVOL-Adaptive Stop + {config.EXIT_TIME} Exit")
    log(f"Entry: RTG (close >= open*(1+{getattr(config, 'RTG_MIN_BAR_GAIN_PCT', 0.01):.0%}) + vol >= {config.RTG_VOLUME_MULT}x prior) in {config.ENTRY_WINDOW_START}-{config.ENTRY_WINDOW_END}")
    min_rvol = getattr(config, "MIN_RVOL_TO_TRADE", 3.0)
    log(f"MIN_RVOL_TO_TRADE: {min_rvol}x | Exit: {config.EXIT_TIME} market sell or RVOL-adaptive stop")
    exit_tiers = getattr(config, "RVOL_EXIT_TIERS", [])
    if exit_tiers:
        tier_str = ", ".join(f"RVOL>{r:.0f}x→{s:.0%}stop" for r, s, t in exit_tiers)
        log(f"Exit tiers: {tier_str}")
    sizing_str = "/".join(f"{p:.0%}" for _, p in config.RVOL_SIZING_TIERS)
    log(f"Sizing: RVOL-weighted ({sizing_str}) | max {config.MAX_POSITIONS} concurrent")
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
