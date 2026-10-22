"""stonewang_daytrade_momentum_surge_1.1 — Pre-market + Three-pronged intraday momentum strategy.

Pre-Market: Gap continuation (5%+, news catalyst, 7:00-9:25 AM)
Prong 1: Small Gap Scan (3% gap, RTG entry)
Prong 2: Intraday Momentum Scan (every 5 min) — breakout entry
Prong 3: Low-Open Reversal (gap-down -3% to -10% with high RVOL) — reversal entry

Exit: 5% hard stop + trailing stop (3% after +5% activation) + 10-min time limit
Daily loss circuit breaker: 8%
EOD force close at 15:59 EST.
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
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import MostActivesRequest, MarketMoversRequest
from alpaca.data.enums import MostActivesBy, MarketType
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

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
screener_client = ScreenerClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)

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
    rvol = min(rvol, getattr(config, "RVOL_SIZING_CAP", 10.0))
    tiers = getattr(config, "RVOL_SIZING_TIERS", [(10.0, 0.50), (5.0, 0.30), (0.0, 0.15)])
    for rvol_min, pct in tiers:
        if rvol >= rvol_min:
            return rvol_min, pct
    return 0.0, 0.15


def get_rvol_sizing(rvol, equity, same_tier_count=1):
    _, pct = _get_rvol_tier(rvol)
    split_pct = pct / max(same_tier_count, 1)
    return round(equity * split_pct, 2)


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
    rvol: float = 0.0
    atr: float = 0.0
    stop_pct: float = 0.0
    # Trailing stop fields
    highest: float = 0.0
    trail_active: bool = False
    trail_activate_pct: float = 0.0
    trail_pct: float = 0.0
    from_premarket: bool = False


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


_ws_thread = None


def start_ws_stream(symbols):
    global _ws_stream, _ws_thread
    if _stream_state["running"]:
        return
    try:
        from alpaca.data.live.stock import StockDataStream
        _ws_stream = StockDataStream(
            config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY,
            feed=DataFeed.IEX,
        )
        for sym in symbols:
            _ws_stream.subscribe_bars(_on_bar, sym)
        _stream_state["symbols"] = set(symbols)
        _stream_state["running"] = True
        _stream_state["last_bar_ts"] = time.time()

        def _run():
            try:
                _ws_stream.run()
            except Exception as e:
                log(f"WebSocket error: {e}")
            finally:
                _stream_state["running"] = False
                log("WebSocket thread exited")

        _ws_thread = threading.Thread(target=_run, daemon=True)
        _ws_thread.start()
        log(f"WebSocket stream started for {len(symbols)} symbols")
    except Exception as e:
        log(f"WebSocket start failed: {e}")
        _stream_state["running"] = False


def restart_ws_stream(symbols):
    global _ws_stream, _ws_thread
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
        except Exception as e:
            log(f"WebSocket stop error (non-fatal): {e}")
        _ws_stream = None
    if _ws_thread and _ws_thread.is_alive():
        _ws_thread.join(timeout=5)
    _ws_thread = None
    time.sleep(5)
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
        if "position not found" in str(e).lower():
            log(f"  Position {symbol} already gone (closed externally)")
            return shares, 0.0
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
            if "position not found" in str(e2).lower():
                return shares, 0.0
            return 0, 0.0


def place_buy_limit(symbol, shares, limit_price, extended_hours=True):
    if config.DRY_RUN:
        oid = f"DRY-LIMIT-{uuid4().hex[:8]}"
        log(f"[DRY] BUY LIMIT {symbol} {shares}sh @ ${limit_price:.4f} (ext_hours={extended_hours})")
        return type("Order", (), {"id": oid})()
    try:
        req = LimitOrderRequest(
            symbol=symbol,
            qty=shares,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=round(limit_price, 2),
            extended_hours=extended_hours,
        )
        order = trading_client.submit_order(req)
        log(f"BUY LIMIT submitted: {symbol} {shares}sh @ ${limit_price:.2f}, id={order.id}, ext_hours={extended_hours}")
        return order
    except Exception as e:
        log(f"BUY LIMIT rejected: {symbol} {shares}sh @ ${limit_price:.4f} - {e}")
        return None


def place_sell_limit(symbol, shares, limit_price, extended_hours=True):
    if config.DRY_RUN:
        log(f"[DRY] SELL LIMIT {symbol} {shares}sh @ ${limit_price:.4f} (ext_hours={extended_hours})")
        return shares, 0.0
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
        req = LimitOrderRequest(
            symbol=symbol,
            qty=shares,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            limit_price=round(limit_price, 2),
            extended_hours=extended_hours,
        )
        order = trading_client.submit_order(req)
        log(f"SELL LIMIT submitted: {symbol} {shares}sh @ ${limit_price:.2f}, id={order.id}, ext_hours={extended_hours}")
        filled, price = wait_order_filled(str(order.id), timeout=30)
        return filled, price
    except Exception as e:
        log(f"SELL LIMIT failed: {symbol} {shares}sh @ ${limit_price:.4f} - {e}")
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
        if "position not found" in str(e).lower():
            return "position_gone"
        try:
            trading_client.close_position(symbol_or_asset_id=symbol)
            return f"close_{symbol}"
        except Exception as e2:
            if "position not found" in str(e2).lower():
                return "position_gone"
            return None


def check_sell_filled(order_id, symbol, shares):
    if order_id == "dry_run":
        return shares, 0.0
    if order_id == "position_gone":
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
        if fill_price <= 0:
            bars = _accumulator.get_1min_bars(symbol)
            if bars:
                fill_price = float(bars[-1]["close"])
        if fill_price <= 0:
            try:
                from alpaca.data.requests import StockSnapshotRequest
                req = StockSnapshotRequest(symbol_or_symbols=symbol,
                                           feed=DataFeed.IEX)
                snap = data_client.get_stock_snapshot(req)
                if snap and symbol in snap:
                    fill_price = float(snap[symbol].daily_bar.close) if snap[symbol].daily_bar else 0.0
            except Exception:
                pass
        return shares, fill_price
    except Exception as e:
        log(f"Force close failed: {symbol} - {e}")
        if "position not found" in str(e).lower():
            fill_price = 0.0
            try:
                cutoff = dt.datetime.now(_EST) - dt.timedelta(minutes=10)
                orders = trading_client.get_orders_for_symbol(symbol)
                for o in orders:
                    if o.side == OrderSide.SELL and o.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                        submitted = getattr(o, "submitted_at", None)
                        if submitted and hasattr(submitted, "timestamp"):
                            submitted_dt = dt.datetime.fromtimestamp(submitted.timestamp(), tz=_EST)
                            if submitted_dt < cutoff:
                                continue
                        fill_price = float(o.filled_avg_price or 0)
                        if fill_price > 0:
                            break
            except Exception:
                pass
            return shares, fill_price
        return 0, 0.0


def save_state(state):
    with open(_state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _fetch_20d_avg_volumes(symbols, target_date):
    lookback = target_date - pd.Timedelta(days=45)
    avg_vols = {}
    atrs = {}
    atr_period = getattr(config, "ATR_PERIOD", 14)
    batch_size = 50
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch, timeframe=TimeFrame.Day,
                start=lookback, end=target_date - pd.Timedelta(days=1),
                adjustment=Adjustment.RAW,
                feed=DataFeed.IEX,
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
                        if len(sym_df) >= 2:
                            daily_bars = sym_df.sort_index().tail(atr_period + 1)
                            true_ranges = []
                            for j in range(1, len(daily_bars)):
                                curr = daily_bars.iloc[j]
                                prev = daily_bars.iloc[j - 1]
                                tr = max(
                                    float(curr["high"]) - float(curr["low"]),
                                    abs(float(curr["high"]) - float(prev["close"])),
                                    abs(float(curr["low"]) - float(prev["close"])),
                                )
                                true_ranges.append(tr)
                            if true_ranges:
                                atrs[sym] = sum(true_ranges) / len(true_ranges)
                except Exception:
                    pass
        except Exception as e:
            log(f"20d volume/ATR fetch error batch {i}: {e}")
    return avg_vols, atrs


def scan_gaps(target_date):
    log(f"Scanning for gap-up stocks on {target_date.date()}...")
    try:
        symbols = get_tradable_symbols()
    except Exception as e:
        log(f"Gap scan: get_tradable_symbols failed: {e}")
        return []
    symbols = [s for s in symbols if not is_leveraged_etf(s)]
    symbols = [s for s in symbols if not is_crypto_etf(s)]
    if EXCLUDE_SYMBOLS:
        symbols = [s for s in symbols if s not in EXCLUDE_SYMBOLS]

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
        log("No gap-up stocks found")
        return []
    results = pd.concat(all_results, ignore_index=True)

    gap_symbols = results["symbol"].tolist()
    avg_vols, atrs = _fetch_20d_avg_volumes(gap_symbols, target_date)

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
        atr = atrs.get(sym, 0)
        candidates.append({
            "symbol": sym, "open_price": float(row["open_price"]),
            "prev_close": float(row["prev_close"]), "gap_pct": float(row["gap_pct"]),
            "rvol": float(rvol), "atr": float(atr),
        })
    candidates.sort(key=lambda c: c["rvol"], reverse=True)
    candidates = candidates[:config.MAX_CANDIDATES]
    log(f"Top {len(candidates)} gap-up candidates by RVOL: {[c['symbol'] for c in candidates]}")
    for c in candidates:
        log(f"  {c['symbol']}: gap +{c['gap_pct']:.1%}, RVOL={c['rvol']:.1f}x, open=${c['open_price']:.4f}")
    return candidates


def scan_gaps_down(target_date):
    """Scan for gap-down stocks (-3% to -10%) with high RVOL for reversal entries."""
    log(f"Scanning for gap-down reversal stocks on {target_date.date()}...")
    try:
        symbols = get_tradable_symbols()
    except Exception as e:
        log(f"Gap-down scan: get_tradable_symbols failed: {e}")
        return []
    symbols = [s for s in symbols if not is_leveraged_etf(s)]
    symbols = [s for s in symbols if not is_crypto_etf(s)]
    if EXCLUDE_SYMBOLS:
        symbols = [s for s in symbols if s not in EXCLUDE_SYMBOLS]

    gap_min = getattr(config, "REVERSAL_GAP_MIN", -0.10)
    gap_max = getattr(config, "REVERSAL_GAP_MAX", -0.03)
    min_rvol = getattr(config, "REVERSAL_MIN_RVOL", 3.0)
    price_min = getattr(config, "REVERSAL_PRICE_MIN", 3.0)
    price_max = getattr(config, "REVERSAL_PRICE_MAX", 20.0)
    max_cands = getattr(config, "REVERSAL_MAX_CANDIDATES", 20)

    lookback_start = target_date - pd.Timedelta(days=45)
    lookback_end = target_date + pd.Timedelta(days=1)

    batch_size = 200
    candidates = []
    total_batches = (len(symbols) + batch_size - 1) // batch_size

    for batch_idx in range(total_batches):
        batch = symbols[batch_idx * batch_size: (batch_idx + 1) * batch_size]
        if batch_idx % 5 == 0:
            log(f"  Gap-down scan batch {batch_idx + 1}/{total_batches}...")
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch, timeframe=TimeFrame.Day,
                start=lookback_start, end=lookback_end,
                adjustment=Adjustment.RAW,
                feed=DataFeed.IEX,
            )
            bars = data_client.get_stock_bars(req)
            if bars.df.empty:
                continue
            df = bars.df

            for sym in batch:
                try:
                    if "symbol" in df.columns:
                        sym_df = df[df["symbol"] == sym].sort_index()
                    else:
                        sym_df = df.loc[sym].sort_index() if sym in df.index else pd.DataFrame()
                    if len(sym_df) < 2:
                        continue

                    curr = sym_df.iloc[-1]
                    prev = sym_df.iloc[-2]
                    prev_close = float(prev["close"])
                    open_price = float(curr["open"])
                    prev_vol = int(prev["volume"])
                    gap_day_vol = int(curr["volume"])
                    if prev_close <= 0:
                        continue
                    gap_pct = (open_price / prev_close) - 1.0
                    if not (gap_min <= gap_pct <= gap_max):
                        continue
                    if not (price_min <= open_price <= price_max):
                        continue
                    if prev_vol < config.MIN_VOLUME:
                        continue

                    # RVOL calculation
                    lookback_start_idx = max(0, len(sym_df) - config.RVOL_LOOKBACK_DAYS - 2)
                    prior_vols = [int(sym_df.iloc[j]["volume"]) for j in range(lookback_start_idx, len(sym_df) - 1)]
                    avg_vol_20d = sum(prior_vols) / len(prior_vols) if prior_vols else 0
                    rvol = gap_day_vol / avg_vol_20d if avg_vol_20d > 0 else 0
                    if rvol < min_rvol:
                        continue

                    # ATR
                    atr_period = getattr(config, "ATR_PERIOD", 14)
                    atr_start = max(0, len(sym_df) - atr_period - 2)
                    true_ranges = []
                    for j in range(atr_start + 1, len(sym_df) - 1):
                        if j < 1:
                            continue
                        bar_j = sym_df.iloc[j]
                        bar_jm1 = sym_df.iloc[j - 1]
                        tr = max(
                            float(bar_j["high"]) - float(bar_j["low"]),
                            abs(float(bar_j["high"]) - float(bar_jm1["close"])),
                            abs(float(bar_j["low"]) - float(bar_jm1["close"])),
                        )
                        true_ranges.append(tr)
                    atr = sum(true_ranges) / len(true_ranges) if true_ranges else 0

                    candidates.append({
                        "symbol": sym, "open_price": open_price,
                        "prev_close": prev_close, "gap_pct": gap_pct,
                        "rvol": rvol, "atr": atr,
                    })
                except (KeyError, IndexError):
                    continue
        except Exception as e:
            log(f"  Gap-down scan API error: {e}")
            continue

    candidates.sort(key=lambda c: c["rvol"], reverse=True)
    candidates = candidates[:max_cands]
    log(f"Gap-down reversal: {len(candidates)} candidates")
    for c in candidates:
        log(f"  {c['symbol']}: gap {c['gap_pct']:+.1%}, RVOL={c['rvol']:.1f}x, open=${c['open_price']:.4f}")
    return candidates


def check_news_catalyst(symbol, target_date):
    """Check if symbol has a news catalyst (FDA, earnings, M&A) within lookback hours."""
    news_required = getattr(config, "PREMARKET_NEWS_REQUIRED", True)
    if not news_required:
        return True, ""
    keywords = getattr(config, "PREMARKET_NEWS_KEYWORDS", [
        "FDA", "approval", "approved", "earnings", "EPS", "revenue", "beat",
        "acquisition", "acquire", "merger", "buyout", "takeover",
    ])
    lookback_hours = getattr(config, "PREMARKET_NEWS_LOOKBACK_HOURS", 12)
    try:
        news_client = NewsClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
        )
        start = target_date - pd.Timedelta(hours=lookback_hours)
        req = NewsRequest(
            symbols=symbol,
            start=start,
            end=target_date,
            limit=20,
            include_content=False,
        )
        news_set = news_client.get_news(req)
        news_list = news_set if isinstance(news_set, list) else list(news_set)
        for article in news_list:
            headline = getattr(article, "headline", "") or ""
            summary = getattr(article, "summary", "") or ""
            text = (headline + " " + summary).upper()
            for kw in keywords:
                if kw.upper() in text:
                    return True, kw
    except Exception as e:
        log(f"  News check error for {symbol}: {e}")
        # If news API fails, allow through (don't block trading)
        return True, "api_error"
    return False, ""


def scan_premarket_gaps(target_date):
    """Scan for pre-market gap-up stocks.

    Uses IEX snapshot for prev_close (daily_bar.close = yesterday's close),
    then IEX StockTradesRequest to get actual pre-market trade prices
    (snapshot latest_trade does NOT return pre-market data on IEX).
    """
    log(f"Pre-market scan for {target_date.date()}...")
    try:
        symbols = get_tradable_symbols()
    except Exception as e:
        log(f"Pre-market scan: get_tradable_symbols failed: {e}")
        return []
    symbols = [s for s in symbols if not is_leveraged_etf(s)]
    symbols = [s for s in symbols if not is_crypto_etf(s)]
    if EXCLUDE_SYMBOLS:
        symbols = [s for s in symbols if s not in EXCLUDE_SYMBOLS]

    gap_min = getattr(config, "PREMARKET_GAP_MIN", 0.05)
    min_vol = getattr(config, "PREMARKET_MIN_VOLUME", 50000)
    price_min = getattr(config, "PREMARKET_PRICE_MIN", 3.0)
    price_max = getattr(config, "PREMARKET_PRICE_MAX", 20.0)
    max_cands = getattr(config, "PREMARKET_MAX_CANDIDATES", 10)

    # Step 1: Get prev_close for all symbols via snapshot
    prev_closes = {}
    premarket_vols = {}
    batch_size = 200
    total_batches = (len(symbols) + batch_size - 1) // batch_size

    for batch_idx in range(total_batches):
        batch = symbols[batch_idx * batch_size: (batch_idx + 1) * batch_size]
        if batch_idx % 5 == 0:
            log(f"  Pre-market scan batch {batch_idx + 1}/{total_batches}...")
        try:
            from alpaca.data.requests import StockSnapshotRequest
            snap_req = StockSnapshotRequest(symbol_or_symbols=batch, feed=DataFeed.IEX)
            snapshots = data_client.get_stock_snapshot(snap_req)
            for sym, snap in snapshots.items():
                try:
                    if not snap or not snap.daily_bar:
                        continue
                    prev_close = float(snap.daily_bar.close)
                    vol = int(snap.daily_bar.volume) if snap.daily_bar.volume else 0
                    if prev_close > 0:
                        prev_closes[sym] = prev_close
                        premarket_vols[sym] = vol
                except Exception:
                    continue
        except Exception as e:
            log(f"  Pre-market scan batch error: {e}")
            continue

    # Step 2: Get pre-market trade prices via StockTradesRequest
    from alpaca.data.requests import StockTradesRequest
    now_est = dt.datetime.now(_EST)
    pm_start = dt.datetime.combine(target_date.date(), dt.time(4, 0), tzinfo=_EST)
    pm_end = min(now_est, dt.datetime.combine(target_date.date(), dt.time(9, 30), tzinfo=_EST))

    premarket_prices = {}
    trade_batch_size = 50
    syms_with_close = list(prev_closes.keys())
    trade_batches = (len(syms_with_close) + trade_batch_size - 1) // trade_batch_size

    for bi in range(trade_batches):
        batch = syms_with_close[bi * trade_batch_size: (bi + 1) * trade_batch_size]
        try:
            req = StockTradesRequest(
                symbol_or_symbols=batch,
                start=pm_start, end=pm_end,
                feed=DataFeed.IEX, limit=10,
            )
            trades = data_client.get_stock_trades(req)
            if trades.df.empty:
                continue
            for sym in batch:
                try:
                    if "symbol" in trades.df.columns:
                        sym_trades = trades.df[trades.df["symbol"] == sym]
                    else:
                        sym_trades = trades.df.loc[sym] if sym in trades.df.index else pd.DataFrame()
                    if not sym_trades.empty:
                        premarket_prices[sym] = float(sym_trades.iloc[-1]["price"])
                except Exception:
                    continue
        except Exception:
            continue

    # Step 3: Calculate gaps and filter
    candidates = []
    for sym, pm_price in premarket_prices.items():
        prev_close = prev_closes.get(sym, 0)
        vol = premarket_vols.get(sym, 0)
        if prev_close <= 0:
            continue
        gap_pct = (pm_price / prev_close) - 1.0
        if gap_pct < gap_min:
            continue
        if not (price_min <= pm_price <= price_max):
            continue
        if vol < min_vol:
            continue
        candidates.append({
            "symbol": sym,
            "prev_close": prev_close,
            "open_price": pm_price,
            "gap_pct": gap_pct,
            "prev_volume": vol,
            "rvol": 0,
            "atr": 0,
            "signal_type": "premarket_gap",
            "news_catalyst": None,
        })

    if candidates:
        log(f"  Pre-market raw: {len(candidates)} stocks with gap >= {gap_min:.0%}")
        for c in candidates[:10]:
            log(f"    {c['symbol']}: gap {c['gap_pct']:+.1%} @ ${c['open_price']:.2f}")
    candidates = candidates[:max_cands]

    # News catalyst filter
    if getattr(config, "PREMARKET_NEWS_REQUIRED", True):
        filtered = []
        for c in candidates:
            has_news, keyword = check_news_catalyst(c["symbol"], target_date)
            if has_news:
                c["news_catalyst"] = keyword
                filtered.append(c)
            else:
                log(f"  {c['symbol']}: no news catalyst, skipped")
        candidates = filtered

    log(f"Pre-market: {len(candidates)} candidates")
    for c in candidates:
        log(f"  {c['symbol']}: gap {c['gap_pct']:+.1%}, premarket=${c['open_price']:.4f}, vol={c['prev_volume']:,}, catalyst={c.get('news_catalyst', '?')}")
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
                feed=DataFeed.IEX,
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
        bc = bar["close"]
        bv = bar["volume"]
        pv = prev["volume"]
        if bc > open_price and pv > 0 and bv >= config.RTG_VOLUME_MULT * pv and bv >= min_volume:
            entry = round(open_price * 1.001, 4) if getattr(config, "RTG_ENTRY_AT_OPEN", True) else round(bc, 4)
            return entry, True, "rtg"
    return 0.0, False, ""


def check_rtg_entry_instant(symbol, open_price, bars, min_volume=None, require_above_open=True):
    if len(bars) < 2:
        return 0.0, False, ""
    if min_volume is None:
        min_volume = config.RTG_MIN_VOLUME
    ew_start_h, ew_start_m = (int(x) for x in config.ENTRY_WINDOW_START.split(":"))
    ew_end_h, ew_end_m = (int(x) for x in config.ENTRY_WINDOW_END.split(":"))
    entry_start = dt.time(ew_start_h, ew_start_m)
    entry_end = dt.time(ew_end_h, ew_end_m)
    start_idx = max(1, len(bars) - 3)
    for i in range(start_idx, len(bars)):
        bar = bars[i]
        prev = bars[i - 1]
        ts = bar.get("timestamp")
        if ts is None:
            continue
        bar_time = ts.time() if isinstance(ts, dt.datetime) else (ts.time() if hasattr(ts, "time") else None)
        if bar_time is None or not (entry_start <= bar_time <= entry_end):
            continue
        bc = bar["close"]
        bo = bar["open"]
        bv = bar["volume"]
        pv = prev["volume"]
        price_ok = (bc > open_price) if require_above_open else (bc > bo)
        if price_ok and pv > 0 and bv >= config.RTG_VOLUME_MULT * pv and bv >= min_volume:
            entry = round(open_price * 1.001, 4) if getattr(config, "RTG_ENTRY_AT_OPEN", True) else round(bc, 4)
            return entry, True, "rtg"
    return 0.0, False, ""


def check_reversal_entry(symbol, open_price, bars, min_volume=None):
    """Low-open reversal: close crosses above open_price + volume spike + bullish bar."""
    if len(bars) < 2:
        return 0.0, False, ""
    if min_volume is None:
        min_volume = config.RTG_MIN_VOLUME
    ew_start_h, ew_start_m = (int(x) for x in config.ENTRY_WINDOW_START.split(":"))
    ew_end_h, ew_end_m = (int(x) for x in config.ENTRY_WINDOW_END.split(":"))
    entry_start = dt.time(ew_start_h, ew_start_m)
    entry_end = dt.time(ew_end_h, ew_end_m)
    start_idx = max(1, len(bars) - 3)
    for i in range(start_idx, len(bars)):
        bar = bars[i]
        prev = bars[i - 1]
        ts = bar.get("timestamp")
        if ts is None:
            continue
        bar_time = ts.time() if isinstance(ts, dt.datetime) else (ts.time() if hasattr(ts, "time") else None)
        if bar_time is None or not (entry_start <= bar_time <= entry_end):
            continue
        bc = bar["close"]
        bo = bar["open"]
        bv = bar["volume"]
        pv = prev["volume"]
        # Must be bullish, cross above open_price, volume spike
        if bc <= bo:
            continue
        if bc <= open_price:
            continue
        if pv <= 0 or bv < config.RTG_VOLUME_MULT * pv:
            continue
        if bv < min_volume:
            continue
        entry = round(open_price * 1.001, 4) if getattr(config, "RTG_ENTRY_AT_OPEN", True) else round(bc, 4)
        return entry, True, "reversal"
    return 0.0, False, ""


def calc_support_level(bars, n=5):
    """Calculate support level as the lowest low of the last n bars."""
    if not bars or len(bars) < 1:
        return 0.0
    recent = bars[-n:] if len(bars) >= n else bars
    return min(b["low"] for b in recent)


def check_volume_exhaustion(bars, n=10):
    """Check volume exhaustion. Returns:
    "vol_divergence" — higher high but volume < 30% of peak → sell half, trail the rest
    "vol_dryup" — 3 declining vol bars, latest < 20% of peak → sell all
    "" — no exhaustion
    """
    if len(bars) < n:
        return ""
    recent = bars[-n:]
    peak_vol = max(b["volume"] for b in recent)
    if peak_vol <= 0:
        return ""
    last = bars[-1]
    # Signal 2 (more severe): 3 consecutive declining volume, latest < 20% of peak
    if len(bars) >= 3:
        v1, v2, v3 = bars[-3]["volume"], bars[-2]["volume"], bars[-1]["volume"]
        if v1 > v2 > v3 and v3 < 0.20 * peak_vol:
            return "vol_dryup"
    # Signal 1 (less severe): higher high + volume divergence
    if len(bars) >= 2 and last["high"] > bars[-2]["high"] and last["volume"] < 0.30 * peak_vol:
        return "vol_divergence"
    return ""


def check_premarket_entry(symbol, open_price, bars, gap_pct=0.0, min_volume=None):
    """Pre-market entry: fast rise (≥8% gap) → immediate buy; slow rise (5-8%) → 3-bar confirmation.
    Both require price to have settled (last 3 bars range < 5% of close)."""
    if min_volume is None:
        min_volume = getattr(config, "PREMARKET_MIN_VOLUME", 50000)
    fast_gap = getattr(config, "PREMARKET_FAST_GAP_PCT", 0.08)

    # Price stability gate: last 3 bars must have range < 5% of close
    # This prevents entry during opening bar volatility (e.g. 20% swings)
    if len(bars) < 3:
        return 0.0, False, ""
    last_3 = bars[-3:]
    for b in last_3:
        bar_range = (b["high"] - b["low"]) / b["close"] if b["close"] > 0 else 1
        if bar_range >= 0.05:
            return 0.0, False, ""

    # Fast rise: gap ≥ 8% → immediate entry on latest bar
    if gap_pct >= fast_gap:
        last = bars[-1]
        if last["volume"] < min_volume:
            return 0.0, False, ""
        entry = round(last["close"], 4)
        return entry, True, "premarket_fast"

    # Slow rise: gap 5-8% → 3 consecutive bars with higher highs + volume acceleration
    for i in range(1, len(last_3)):
        if last_3[i]["high"] <= last_3[i - 1]["high"]:
            return 0.0, False, ""
        if last_3[i]["close"] <= last_3[i - 1]["close"]:
            return 0.0, False, ""
    # Volume acceleration: last bar vol >= 1.5× second-to-last
    if last_3[-1]["volume"] < 1.5 * last_3[-2]["volume"]:
        return 0.0, False, ""
    if last_3[-1]["volume"] < min_volume:
        return 0.0, False, ""
    entry = round(last_3[-1]["close"], 4)
    return entry, True, "premarket_slow"


def backfill_premarket_bars(symbols, target_date):
    """Backfill 1-min bars for pre-market (4:00 AM - now)."""
    premarket_start = pd.Timestamp(f"{target_date.date()} 04:00", tz="America/New_York")
    now = pd.Timestamp.now(tz="America/New_York")
    end = min(now, pd.Timestamp(f"{target_date.date()} 09:30", tz="America/New_York"))
    if end <= premarket_start:
        return
    for sym in symbols:
        try:
            req = StockBarsRequest(
                symbol_or_symbols=sym, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                start=premarket_start, end=end, adjustment=Adjustment.RAW,
                feed=DataFeed.IEX,
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
                    "timestamp": ts.to_pydatetime(),
                    "open": float(bar["open"]),
                    "high": float(bar["high"]),
                    "low": float(bar["low"]),
                    "close": float(bar["close"]),
                    "volume": int(bar["volume"]),
                })
        except Exception:
            continue


def run_premarket_phase(target_date):
    """Run pre-market trading phase (7:00-9:25 AM)."""
    if not getattr(config, "PREMARKET_ENABLED", False):
        return {"positions": [], "daily_loss": 0.0, "trades_detail": []}

    log("=" * 60)
    log("PRE-MARKET PHASE (7:00-9:25 AM)")
    log("=" * 60)

    premarket_start_str = getattr(config, "PREMARKET_START", "07:00")
    entry_end_str = getattr(config, "PREMARKET_ENTRY_END", "09:20")
    transition_str = getattr(config, "PREMARKET_TRANSITION_TIME", "09:25")
    stop_pct = getattr(config, "PREMARKET_STOP_PCT", 0.03)
    trail_activate_pct = getattr(config, "PREMARKET_TRAIL_ACTIVATE_PCT", 0.03)
    trail_pct = getattr(config, "PREMARKET_TRAIL_PCT", 0.02)
    time_limit_sec = getattr(config, "PREMARKET_TIME_LIMIT_SEC", 1800)
    limit_buffer = getattr(config, "PREMARKET_LIMIT_BUFFER", 0.005)
    max_positions = getattr(config, "PREMARKET_MAX_POSITIONS", 1)

    premarket_start_time = _parse_time(premarket_start_str)
    entry_end_time = _parse_time(entry_end_str)
    transition_time = _parse_time(transition_str)

    # Wait until pre-market start
    now = dt.datetime.now(_EST)
    start_dt = dt.datetime.combine(target_date.date(), premarket_start_time, tzinfo=_EST)
    if now < start_dt:
        _smart_sleep_until(start_dt)

    # Scan for pre-market gaps
    candidates = scan_premarket_gaps(target_date)
    if not candidates:
        log("No pre-market candidates found")
        return {"positions": [], "daily_loss": 0.0, "trades_detail": []}

    syms = [c["symbol"] for c in candidates]
    backfill_premarket_bars(syms, target_date)
    restart_ws_stream(syms)

    positions = []
    entry_checked = set()
    daily_loss = 0.0
    trades_detail = []

    try:
        acct = trading_client.get_account()
        equity = float(acct.equity)
    except Exception:
        equity = config.INITIAL_CAPITAL

    log(f"Pre-market equity: ${equity:,.2f}")
    log(f"Pre-market params: stop={stop_pct:.0%}, trail={trail_activate_pct:.0%}/{trail_pct:.0%}, time={time_limit_sec}s")

    while True:
        now = dt.datetime.now(_EST)
        now_time = now.time()

        # Transition to regular session
        if now_time >= transition_time:
            log(f"Pre-market transition time reached ({transition_str})")
            break

        # Force close check
        force_close_time = _parse_time(config.FORCE_CLOSE_TIME)
        if now_time >= force_close_time:
            break

        # Entry window
        entry_active = premarket_start_time <= now_time < entry_end_time

        # Exit monitoring
        for pos in positions[:]:
            bars = _accumulator.get_1min_bars(pos.symbol)
            if not bars:
                continue
            latest = bars[-1]
            bar_low = latest["low"]
            bar_high = latest["high"]

            max_stop = round(pos.entry_price * (1 - pos.stop_pct), 4)
            # Support stop: use 5-bar support after 5 bars, else fixed %
            bars_held = len(bars)  # approximate bars since entry
            if bars_held >= 5:
                support = calc_support_level(bars, n=5)
                stop_price = max(support, max_stop) if support > 0 else max_stop
            else:
                support = 0
                stop_price = max_stop
            reason = None

            if bar_high > pos.highest:
                pos.highest = bar_high

            # Exit 1: Support-level stop loss (below 5-bar low, capped at max %)
            if bar_low <= stop_price:
                reason = "support_stop" if support > max_stop else "stop_loss"
            # Exit 2: Volume exhaustion
            vol_signal = check_volume_exhaustion(bars) if reason is None and bars_held >= 7 else ""
            if vol_signal == "vol_dryup":
                reason = "vol_dryup"  # Sell all — trend is dead
            elif vol_signal == "vol_divergence":
                # Sell half, activate trailing stop on remainder
                half_shares = pos.shares // 2
                if half_shares >= 1:
                    sell_price = round(latest["close"] * (1 - 0.005), 2)
                    sold, fill = place_sell_limit(pos.symbol, half_shares, sell_price, extended_hours=True)
                    if sold > 0:
                        pos.shares -= sold
                        if fill <= 0:
                            fill = latest["close"]
                        pnl = round((fill - pos.entry_price) * sold, 2)
                        trades_detail.append({"symbol": pos.symbol, "entry": pos.entry_price,
                            "exit": round(fill, 4), "shares": sold, "pnl": pnl,
                            "reason": "vol_divergence", "trade_type": pos.signal_type,
                            "entry_ts": dt.datetime.fromtimestamp(pos.entry_ts, tz=_EST).strftime("%H:%M:%S"),
                            "exit_ts": dt.datetime.now(_EST).strftime("%H:%M:%S")})
                        daily_loss += pnl
                        log(f"PRE-MARKET HALF SELL {pos.symbol} vol_divergence {sold}sh ${fill:.4f}, P&L=${pnl:+,.2f}, remaining {pos.shares}sh")
                    pos.trail_active = True  # Force trail on remaining shares
            # Exit 3: Trailing stop
            if reason is None:
                if not pos.trail_active and pos.highest >= pos.entry_price * (1 + pos.trail_activate_pct):
                    pos.trail_active = True
                if pos.trail_active:
                    trail_stop_price = round(pos.highest * (1 - pos.trail_pct), 4)
                    if bar_low <= trail_stop_price:
                        reason = "trailing_stop"
            # Exit 4: Time limit
            if reason is None and time_limit_sec > 0 and time.time() - pos.entry_ts >= time_limit_sec:
                reason = "time_limit"

            if reason is None:
                continue

            # Exit with limit sell (extended hours)
            sell_price = round(latest["close"] * (1 - 0.005), 4)
            sold, fill = place_sell_limit(pos.symbol, pos.shares, sell_price, extended_hours=True)
            if sold > 0:
                if fill <= 0:
                    fill = latest["close"]
                pnl = round((fill - pos.entry_price) * sold, 2)
                trades_detail.append({
                    "symbol": pos.symbol, "entry": pos.entry_price,
                    "exit": round(fill, 4), "shares": sold, "pnl": pnl,
                    "reason": reason, "trade_type": pos.signal_type,
                    "entry_ts": dt.datetime.fromtimestamp(pos.entry_ts, tz=_EST).strftime("%H:%M:%S"),
                    "exit_ts": dt.datetime.now(_EST).strftime("%H:%M:%S"),
                })
                daily_loss += pnl
                positions.remove(pos)
                log(f"PRE-MARKET EXIT0 {pos.symbol} {reason} ${fill:.4f}, P&L=${pnl:+,.2f}")
            else:
                # Fallback: market sell
                sold, fill = place_sell_market(pos.symbol, pos.shares)
                if sold > 0:
                    if fill <= 0:
                        fill = latest["close"]
                    pnl = round((fill - pos.entry_price) * sold, 2)
                    trades_detail.append({
                        "symbol": pos.symbol, "entry": pos.entry_price,
                        "exit": round(fill, 4), "shares": sold, "pnl": pnl,
                        "reason": reason, "trade_type": pos.signal_type,
                        "entry_ts": dt.datetime.fromtimestamp(pos.entry_ts, tz=_EST).strftime("%H:%M:%S"),
                        "exit_ts": dt.datetime.now(_EST).strftime("%H:%M:%S"),
                    })
                    daily_loss += pnl
                    positions.remove(pos)
                    log(f"PRE-MARKET EXIT {pos.symbol} {reason} ${fill:.4f}, P&L=${pnl:+,.2f}")

        # Entry monitoring (price stability checked in check_premarket_entry)
        if entry_active and len(positions) < max_positions:
            for c in candidates:
                if len(positions) >= max_positions:
                    break
                sym = c["symbol"]
                if any(p.symbol == sym for p in positions):
                    continue
                if sym in entry_checked:
                    continue
                if sym in EXCLUDE_SYMBOLS:
                    continue

                open_price = c["open_price"]
                bars = _accumulator.get_1min_bars(sym)
                entry_price, confirmed, signal_type = check_premarket_entry(sym, open_price, bars, gap_pct=c.get("gap_pct", 0))
                if not confirmed:
                    continue

                # Position sizing
                buy_power = equity
                shares = int(buy_power / (entry_price * (1 + limit_buffer)))
                if shares <= 0:
                    continue
                min_pos = getattr(config, "MIN_POSITION_SIZE", 40)
                if shares * entry_price < min_pos:
                    continue

                # Submit limit buy with buffer (round to cent — sub-penny rejected pre-market)
                limit_price = round(entry_price * (1 + limit_buffer), 2)
                order = place_buy_limit(sym, shares, limit_price, extended_hours=True)
                if order is None:
                    entry_checked.add(sym)
                    continue

                # Wait for fill
                filled, fill_price = wait_order_filled(str(order.id), timeout=60)
                if filled <= 0:
                    log(f"Pre-market BUY LIMIT not filled: {sym}")
                    entry_checked.add(sym)
                    try:
                        trading_client.cancel_order_by_id(str(order.id))
                    except Exception:
                        pass
                    continue

                actual_entry = fill_price if fill_price > 0 else entry_price
                pos = Position(
                    symbol=sym, shares=filled, entry_price=actual_entry,
                    entry_ts=time.time(), open_price=open_price,
                    gap_pct=c.get("gap_pct", 0), signal_type=signal_type,
                    rvol=c.get("rvol", 0), atr=c.get("atr", 0),
                    stop_pct=stop_pct, highest=actual_entry,
                    trail_active=False, trail_activate_pct=trail_activate_pct,
                    trail_pct=trail_pct, from_premarket=True,
                )
                positions.append(pos)
                entry_checked.add(sym)
                log(f"PRE-MARKET ENTRY {sym} {filled}sh @ ${actual_entry:.4f} (gap {c['gap_pct']:+.1%})")

        time.sleep(config.POLL_INTERVAL)

    # Return positions to transition into regular session
    log(f"Pre-market phase done. {len(positions)} positions to transition, P&L=${-daily_loss:+,.2f}")
    return {"positions": positions, "daily_loss": daily_loss, "trades_detail": trades_detail}


def _parse_time(t_str):
    h, m = (int(x) for x in t_str.split(":"))
    return dt.time(h, m)


def run_trading_day(target_date, premarket_results=None):
    log(f"Starting trading day: {target_date.date()} (close {config.MARKET_CLOSE}, force_close {config.FORCE_CLOSE_TIME})")
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

    # Prong 1: Gap-up scan
    candidates = scan_gaps(target_date)

    # Prong 3: Gap-down reversal scan
    rev_candidates = []
    if getattr(config, "REVERSAL_SCAN_ENABLED", False):
        rev_candidates = scan_gaps_down(target_date)

    # Combine: dedupe by symbol, sort by RVOL
    all_cands = {}
    for c in candidates:
        all_cands[c["symbol"]] = c
    for c in rev_candidates:
        if c["symbol"] not in all_cands:
            all_cands[c["symbol"]] = c
    candidates = sorted(all_cands.values(), key=lambda c: c["rvol"], reverse=True)

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
    _last_exit_ts = {}
    _stop_exit_ts = {}
    _sell_stuck_until = {}
    _pending_sells = {}

    # Transition pre-market positions into regular session
    if premarket_results and premarket_results.get("positions"):
        for pos in premarket_results["positions"]:
            # Adjust stop/trail to regular session params
            pos.stop_pct = config.RTG_STOP_PCT
            pos.trail_activate_pct = config.TRAILING_ACTIVATE_PCT
            pos.trail_pct = config.TRAILING_PCT
            positions.append(pos)
            entry_checked.add(pos.symbol)
            log(f"Transitioned pre-market position: {pos.symbol} {pos.shares}sh @ ${pos.entry_price:.4f} → regular session stops")

    # Restore existing Alpaca positions
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
                continue
            sp = prev_positions.get(sym, {})
            cand = next((c for c in candidates if c["symbol"] == sym), None)
            rvol = sp.get("rvol", cand.get("rvol", 0) if cand else 0)
            atr = sp.get("atr", cand.get("atr", 0) if cand else 0)
            stop_p = config.RTG_STOP_PCT
            trail_act = config.TRAILING_ACTIVATE_PCT if getattr(config, "TRAILING_STOP_ENABLED", False) else 0
            trail_p = config.TRAILING_PCT if getattr(config, "TRAILING_STOP_ENABLED", False) else 0
            pos = Position(
                symbol=sym, shares=int(float(ep.qty)),
                entry_price=float(ep.avg_entry_price), entry_ts=time.time(),
                open_price=sp.get("open_price", cand.get("open_price", 0) if cand else 0),
                gap_pct=sp.get("gap_pct", cand.get("gap_pct", 0) if cand else 0),
                signal_type=sp.get("signal_type", "rtg"),
                rvol=rvol, atr=atr, stop_pct=sp.get("stop_pct", stop_p),
                highest=float(ep.avg_entry_price),
                trail_active=sp.get("trail_active", False),
                trail_activate_pct=sp.get("trail_activate_pct", trail_act),
                trail_pct=sp.get("trail_pct", trail_p),
            )
            positions.append(pos)
            entry_checked.add(sym)
            entry_count[sym] = entry_count.get(sym, 0) + 1
        if positions:
            log(f"Restored {len(positions)} existing positions: {[p.symbol for p in positions]}")
            restored_syms = [p.symbol for p in positions if p.symbol not in syms]
            if restored_syms:
                syms.extend(restored_syms)
                backfill_1min_bars(restored_syms, target_date)
                restart_ws_stream(syms)
        for sp_sym in prev_positions:
            if sp_sym not in {p.symbol for p in positions}:
                _last_exit_ts[sp_sym] = time.time()
                entry_count[sp_sym] = entry_count.get(sp_sym, 0) + 1
    except Exception as e:
        log(f"Could not restore positions: {e}")

    # Restore exit tracking from trades_detail
    try:
        with open(_state_file) as f:
            _restart_state = json.load(f)
        today_str = str(target_date.date())
        for t in _restart_state.get("trades_detail", []):
            sym = t.get("symbol", "")
            trade_date = t.get("date", "")
            if trade_date and trade_date != today_str:
                continue
            if sym and sym not in _last_exit_ts:
                _last_exit_ts[sym] = time.time()
                entry_count[sym] = entry_count.get(sym, 0) + 1
            reason = t.get("exit_reason", t.get("reason", ""))
            if "stop" in reason.lower() and sym:
                _stop_exit_ts[sym] = time.time()
    except Exception:
        pass

    entry_rejected = set()
    entry_halted = set()
    daily_trades = 0
    trades_detail = []
    daily_loss = 0.0
    max_daily_loss = equity * config.MAX_DAILY_LOSS_PCT

    force_close_dt = dt.datetime.combine(target_date.date(), _parse_time(config.FORCE_CLOSE_TIME), tzinfo=_EST)
    entry_end_dt = dt.datetime.combine(target_date.date(), _parse_time(config.ENTRY_WINDOW_END), tzinfo=_EST)
    entry_start_dt = dt.datetime.combine(target_date.date(), _parse_time(config.ENTRY_WINDOW_START), tzinfo=_EST)
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
                    if fill <= 0:
                        try:
                            cutoff = dt.datetime.now(_EST) - dt.timedelta(minutes=10)
                            orders = trading_client.get_orders_for_symbol(pos.symbol)
                            for o in orders:
                                if o.side == OrderSide.SELL and o.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                                    submitted = getattr(o, "submitted_at", None)
                                    if submitted and hasattr(submitted, "timestamp"):
                                        submitted_dt = dt.datetime.fromtimestamp(submitted.timestamp(), tz=_EST)
                                        if submitted_dt < cutoff:
                                            continue
                                    fill = float(o.filled_avg_price or 0)
                                    if fill > 0:
                                        break
                        except Exception:
                            pass
                    pnl = (fill - pos.entry_price) * sold
                    trades_detail.append({"symbol": pos.symbol, "entry": pos.entry_price, "exit": fill,
                                          "shares": sold, "pnl": round(pnl, 2), "reason": "force_close",
                                          "trade_type": pos.signal_type,
                                          "entry_ts": dt.datetime.fromtimestamp(pos.entry_ts, tz=_EST).strftime("%H:%M:%S"),
                                          "exit_ts": dt.datetime.now(_EST).strftime("%H:%M:%S")})
                    daily_trades += 1
                    positions.remove(pos)
                    log(f"Force closed {pos.symbol}, P&L=${pnl:+,.2f}")
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

        # Daily loss circuit breaker
        if max_daily_loss > 0 and daily_loss <= -max_daily_loss:
            log(f"Daily realized loss ${daily_loss:,.2f} exceeded limit ${max_daily_loss:,.2f}")
            for pos in positions[:]:
                sold, fill = force_sell_position(pos.symbol, pos.shares)
                if sold > 0:
                    pnl = (fill - pos.entry_price) * sold
                    trades_detail.append({"symbol": pos.symbol, "entry": pos.entry_price, "exit": fill,
                                          "shares": sold, "pnl": round(pnl, 2), "reason": "circuit_breaker",
                                          "trade_type": pos.signal_type,
                                          "entry_ts": dt.datetime.fromtimestamp(pos.entry_ts, tz=_EST).strftime("%H:%M:%S"),
                                          "exit_ts": dt.datetime.now(_EST).strftime("%H:%M:%S")})
                    daily_trades += 1
                    log(f"Circuit breaker close {pos.symbol}, P&L=${pnl:+,.2f}")
            state["daily_stopped"] = True
            break

        # Exit monitoring — check pending async sells
        for pos in positions[:]:
            pending = _pending_sells.get(pos.symbol)
            if pending:
                order_id, reason, submit_time = pending["order_id"], pending["reason"], pending["submit_time"]
                if time.time() - submit_time > 60:
                    log(f"SELL timeout for {pos.symbol}, retrying...")
                    _pending_sells.pop(pos.symbol, None)
                    continue
                filled, fill_price = check_sell_filled(order_id, pos.symbol, pos.shares)
                if filled > 0:
                    if fill_price <= 0:
                        bars = _accumulator.get_1min_bars(pos.symbol)
                        fill_price = float(bars[-1]["close"]) if bars else pos.entry_price
                    pnl = round((fill_price - pos.entry_price) * filled, 2)
                    trades_detail.append({"symbol": pos.symbol, "entry": pos.entry_price,
                                          "exit": round(fill_price, 4), "shares": filled, "pnl": pnl,
                                          "reason": reason, "trade_type": pos.signal_type,
                                          "entry_ts": dt.datetime.fromtimestamp(pos.entry_ts, tz=_EST).strftime("%H:%M:%S"),
                                          "exit_ts": dt.datetime.now(_EST).strftime("%H:%M:%S")})
                    daily_loss += pnl
                    daily_trades += 1
                    positions.remove(pos)
                    _last_exit_ts[pos.symbol] = time.time()
                    entry_checked.discard(pos.symbol)
                    if reason == "stop_loss":
                        _stop_exit_ts[pos.symbol] = time.time()
                    _pending_sells.pop(pos.symbol, None)
                    log(f"EXIT {pos.symbol} {reason} ${fill_price:.4f}, P&L=${pnl:+,.2f}")
                elif filled < 0:
                    _pending_sells.pop(pos.symbol, None)

        # Exit monitoring — evaluate positions
        for pos in positions[:]:
            if _sell_stuck_until.get(pos.symbol, 0) > time.time():
                continue
            bars = _accumulator.get_1min_bars(pos.symbol)
            if not bars:
                continue
            latest = bars[-1]
            bar_low = latest["low"]
            bar_high = latest["high"]
            bar_close = latest["close"]

            max_stop = round(pos.entry_price * (1 - pos.stop_pct), 4)
            bars_held = len(bars)
            if bars_held >= 5:
                support = calc_support_level(bars, n=5)
                stop_price = max(support, max_stop) if support > 0 else max_stop
            else:
                support = 0
                stop_price = max_stop
            reason = None

            # Update highest for trailing stop
            if bar_high > pos.highest:
                pos.highest = bar_high

            # Exit 1: Support-level stop loss (below 5-bar low, capped at max %)
            if bar_low <= stop_price:
                reason = "support_stop" if support > max_stop else "stop_loss"
            # Exit 2: Volume exhaustion
            vol_signal = check_volume_exhaustion(bars) if reason is None and bars_held >= 7 else ""
            if vol_signal == "vol_dryup":
                reason = "vol_dryup"  # Sell all
            elif vol_signal == "vol_divergence":
                half_shares = pos.shares // 2
                if half_shares >= 1:
                    if pos.symbol in _pending_sells:
                        vol_signal = ""
                    else:
                        order_id = place_sell_async(pos.symbol, half_shares)
                        if order_id:
                            _pending_sells[pos.symbol] = {"order_id": order_id, "reason": "vol_divergence", "submit_time": time.time()}
                            log(f"Async HALF SELL for {pos.symbol} (vol_divergence), {half_shares}sh, order={order_id}")
                        pos.shares -= half_shares
                        pos.trail_active = True
            # Exit 3: Trailing stop
            if reason is None and getattr(config, "TRAILING_STOP_ENABLED", False):
                if not pos.trail_active and pos.highest >= pos.entry_price * (1 + pos.trail_activate_pct):
                    pos.trail_active = True
                if pos.trail_active:
                    trail_stop_price = round(pos.highest * (1 - pos.trail_pct), 4)
                    if bar_low <= trail_stop_price:
                        reason = "trailing_stop"
            # Exit 4: Time limit (10-min)
            if reason is None and config.RTG_TIME_LIMIT_SEC > 0 and time.time() - pos.entry_ts >= config.RTG_TIME_LIMIT_SEC:
                reason = "time_limit"

            if reason is None:
                continue

            if pos.symbol in _pending_sells:
                continue
            order_id = place_sell_async(pos.symbol, pos.shares)
            if order_id == "position_gone":
                log(f"EXIT {pos.symbol} position_gone (closed externally), P&L unknown")
                _last_exit_ts[pos.symbol] = time.time()
                positions = [p for p in positions if p.symbol != pos.symbol]
                continue
            elif order_id:
                _pending_sells[pos.symbol] = {"order_id": order_id, "reason": reason, "submit_time": time.time()}
                log(f"Async SELL submitted for {pos.symbol} ({reason}), order={order_id}")
            else:
                _sell_stuck_until[pos.symbol] = time.time() + 60

        # Entry monitoring — three-pronged
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
                if rvol < getattr(config, "RTG_MIN_RVOL", 0):
                    continue
                if any(p.symbol == sym for p in positions):
                    continue
                is_reentry = sym in _last_exit_ts
                if is_reentry and not config.RTG_REENTRY_ALLOWED:
                    continue
                if is_reentry and config.RTG_REENTRY_MAX > 0 and entry_count.get(sym, 0) > config.RTG_REENTRY_MAX:
                    continue
                if sym in EXCLUDE_SYMBOLS or sym in entry_halted or is_crypto_etf(sym):
                    continue
                # After stop_loss exit, block re-entry entirely
                if sym in _stop_exit_ts:
                    continue
                reentry_cd = getattr(config, "REENTRY_COOLDOWN_SEC", 60)
                if is_reentry and time.time() - _last_exit_ts.get(sym, 0) < reentry_cd:
                    continue
                after_time = _last_exit_ts.get(sym) if is_reentry else None
                if max_daily_loss > 0 and daily_loss <= -max_daily_loss:
                    break

                open_price = c["open_price"]
                gap_pct = c.get("gap_pct", 0)
                bars = _accumulator.get_1min_bars(sym)
                min_vol = config.RTG_MIN_VOLUME
                if rvol >= 10:
                    min_vol = max(config.RTG_MIN_VOLUME // 3, 5000)
                elif rvol >= 5:
                    min_vol = max(config.RTG_MIN_VOLUME // 2, 10000)

                # Dispatch entry by signal type
                is_morning = now.time() < dt.time(10, 30)
                if gap_pct < 0:
                    # Prong 3: reversal
                    entry_price, confirmed, signal_type = check_reversal_entry(
                        sym, open_price, bars, min_volume=min_vol)
                else:
                    # Prong 1: RTG
                    if is_morning and daily_trades == 0:
                        entry_price, confirmed, signal_type = check_rtg_entry(
                            sym, open_price, bars, after_time=after_time, min_volume=min_vol)
                    else:
                        entry_price, confirmed, signal_type = check_rtg_entry_instant(
                            sym, open_price, bars, min_volume=min_vol, require_above_open=is_morning)

                if not confirmed or entry_price <= 0:
                    continue

                # Re-entry price guard
                if is_reentry:
                    max_reentry_price = open_price * getattr(config, "REENTRY_MAX_PRICE_VS_OPEN", 1.15)
                    if entry_price > max_reentry_price:
                        continue
                    min_pullback = getattr(config, "REENTRY_MIN_PULLBACK", 0.0)
                    if min_pullback > 0 and bars:
                        day_high = max(b["high"] for b in bars)
                        if entry_price > day_high * (1 - min_pullback):
                            continue

                # Position sizing
                if config.MAX_POSITIONS <= 1:
                    slot = max(config.MIN_POSITION_SIZE, equity)
                else:
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
                    if "trading halt" in str(reject).lower():
                        entry_halted.add(sym)
                    else:
                        entry_rejected.add(sym)
                    continue
                filled, fill_price = wait_order_filled(str(order.id), timeout=15)
                if filled <= 0:
                    entry_checked.add(sym)
                    continue
                if fill_price <= 0:
                    fill_price = entry_price

                atr = c.get("atr", 0)
                stop_p = config.RTG_STOP_PCT
                trail_act = config.TRAILING_ACTIVATE_PCT if getattr(config, "TRAILING_STOP_ENABLED", False) else 0
                trail_p = config.TRAILING_PCT if getattr(config, "TRAILING_STOP_ENABLED", False) else 0
                sig_label = signal_type + ("_re" if is_reentry else "")
                pos = Position(symbol=sym, shares=filled, entry_price=fill_price,
                               entry_ts=time.time(), open_price=open_price,
                               gap_pct=gap_pct, signal_type=sig_label,
                               rvol=rvol, atr=atr, stop_pct=stop_p,
                               highest=fill_price, trail_active=False,
                               trail_activate_pct=trail_act, trail_pct=trail_p)
                positions.append(pos)
                entry_checked.add(sym)
                entry_count[sym] = entry_count.get(sym, 0) + 1
                daily_trades += 1
                live_bp -= fill_price * filled
                log(f"ENTRY {sym} [{sig_label}] {filled}sh @ ${fill_price:.4f} "
                    f"[RVOL={rvol:.1f}x stop={stop_p:.0%} trail={trail_act:.0%}/{trail_p:.0%} time={config.RTG_TIME_LIMIT_SEC}s]")

        # WS health
        ws_stale = time.time() - _stream_state["last_bar_ts"] > 120
        ws_thread_dead = _ws_thread is not None and not _ws_thread.is_alive()
        ws_needs_restart = not _stream_state["running"] or ws_thread_dead or ws_stale
        if ws_needs_restart and time.time() - _stream_state.get("last_restart_ts", 0) > 60:
            _stream_state["last_restart_ts"] = time.time()
            try:
                restart_ws_stream(syms)
            except Exception as e:
                log(f"WebSocket restart failed: {e}")

        # Save state
        state.update({
            "updated": dt.datetime.now().isoformat(), "ws_connected": _stream_state["running"],
            "daily_trades": daily_trades,
            "positions": [{"symbol": p.symbol, "shares": p.shares, "entry_price": p.entry_price,
                           "signal_type": p.signal_type, "rvol": p.rvol, "atr": p.atr,
                           "open_price": p.open_price, "gap_pct": p.gap_pct,
                           "stop_pct": p.stop_pct, "entry_ts": p.entry_ts,
                           "highest": p.highest, "trail_active": p.trail_active,
                           "trail_activate_pct": p.trail_activate_pct, "trail_pct": p.trail_pct} for p in positions],
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
        "candidates": candidates, "trades": trades_detail,
    }
    with open(os.path.join(report_dir, f"{target_date.date()}.json"), "w") as f:
        json.dump(report, f, indent=2, default=str)

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
            feed=DataFeed.IEX,
        )
        bars = data_client.get_stock_bars(req)
        if not bars.df.empty:
            log(f"  SPY bar received, OK!")
        log("Connectivity OK!")
        return True
    except Exception as e:
        log(f"Connectivity test failed (non-fatal): {e}")
        return True


def main():
    global _log_file
    _log_file = open(os.path.join(_ver_dir, "live_rtg.log"), "a")

    log(f"Using {config.DATA_FEED.upper()} data feed")
    log("=" * 60)
    log(f"stonewang Momentum Surge 1.1 Live Trading")
    log(f"Prong 1: Small Gap Scan (gap>={config.GAP_THRESHOLD:.0%}) — RTG entry")
    log(f"Prong 2: Intraday Momentum Scan (every {getattr(config, 'MOMENTUM_SCAN_INTERVAL_SEC', 300)}s)")
    log(f"Prong 3: Low-Open Reversal (gap {getattr(config, 'REVERSAL_GAP_MIN', -0.10):+.0%} to {getattr(config, 'REVERSAL_GAP_MAX', -0.03):+.0%})")
    log(f"Exit: {config.RTG_STOP_PCT:.0%} stop + trailing {config.TRAILING_ACTIVATE_PCT:.0%}/{config.TRAILING_PCT:.0%} + {config.RTG_TIME_LIMIT_SEC//60}min limit")
    log(f"Daily loss breaker: {config.MAX_DAILY_LOSS_PCT:.0%}")
    log(f"Window: {config.ENTRY_WINDOW_START}-{config.ENTRY_WINDOW_END} EST")
    log(f"Max concurrent: {config.MAX_POSITIONS} | Re-entry max: {config.RTG_REENTRY_MAX}")
    if getattr(config, "PREMARKET_ENABLED", False):
        log(f"Pre-market: {getattr(config, 'PREMARKET_START', '07:00')}-{getattr(config, 'PREMARKET_TRANSITION_TIME', '09:25')} (gap>={getattr(config, 'PREMARKET_GAP_MIN', 0.05):.0%})")
    log("=" * 60)

    if not test_connectivity():
        log("Connectivity failed, exiting")
        return

    premarket_enabled = getattr(config, "PREMARKET_ENABLED", False)
    wake_hour = 7 if premarket_enabled else 9
    wake_min = 0 if premarket_enabled else 15

    while True:
        now = dt.datetime.now(_EST)
        if now.weekday() >= 5:
            next_day = get_next_trading_day()
            log(f"Weekend. Next: {next_day.date()}")
            _smart_sleep_until(dt.datetime.combine(next_day.date(), dt.time(wake_hour, wake_min), tzinfo=_EST))
            continue

        market_open = dt.datetime.combine(now.date(), dt.time(9, 30), tzinfo=_EST)
        market_close = dt.datetime.combine(now.date(), dt.time(16, 0), tzinfo=_EST)
        if now >= market_close:
            next_day = get_next_trading_day()
            log(f"Market closed. Next: {next_day.date()}")
            _smart_sleep_until(dt.datetime.combine(next_day.date(), dt.time(wake_hour, wake_min), tzinfo=_EST))
            continue

        wake_time = dt.datetime.combine(now.date(), dt.time(wake_hour, wake_min), tzinfo=_EST)
        if now < wake_time:
            _smart_sleep_until(wake_time)

        target = pd.Timestamp(now.date(), tz="America/New_York")

        # Pre-market phase
        premarket_results = None
        if premarket_enabled:
            premarket_results = run_premarket_phase(target)

        # Regular session
        run_trading_day(target, premarket_results=premarket_results)

        next_day = get_next_trading_day()
        log(f"Next trading day: {next_day.date()}. Sleeping until {wake_hour}:{wake_min:02d}...")
        _smart_sleep_until(dt.datetime.combine(next_day.date(), dt.time(wake_hour, wake_min), tzinfo=_EST))


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
