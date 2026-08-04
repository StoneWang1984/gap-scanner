"""rossway_daytrade_0.1 — 简洁交易系统

核心哲学: 一次入场、一个OCO、固定盈亏比2.5:1
- 扫描选股: 继承 stonewang_daytrade_1.3 scanner.py + strategy.py
- 入场确认: 3-bar pullback (from stonewang 1.2 style)
- 止损: max($0.15, entry_price × 1.5%)
- 止盈: 止损 × 2.5 (固定盈亏比)
- 退出: 单次OCO单 (take_profit + stop_loss)
- 多次入场: OCO成交后slot释放，可再次入场
- 不持仓过夜: EOD 15:50强制平仓
"""

# ── ANSI colors ──
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"

import re
import json
import time
import datetime as dt
from zoneinfo import ZoneInfo
from collections import defaultdict
from dataclasses import dataclass, field
import threading
from uuid import uuid4

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest, MarketOrderRequest,
    GetOrdersRequest, TakeProfitRequest, StopLossRequest,
)
from alpaca.trading.enums import (
    OrderSide, TimeInForce, QueryOrderStatus, OrderStatus, OrderClass,
    OrderType,
)
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

import importlib.util, sys, os

# Add parent dir to path
_ver_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_ver_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

# Load version-specific config
_spec = importlib.util.spec_from_file_location("config", os.path.join(_ver_dir, "config.py"))
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)
sys.modules["config"] = config

from scanner import get_tradable_symbols, scan_gaps_for_symbols, scan_gaps_batch
from strategy import calc_atr

# ── Constants from config ──
STOP_TIERS = getattr(config, "STOP_TIERS", None)
STOP_LIMIT_BUFFER = getattr(config, "STOP_LIMIT_BUFFER", 0.03)
MAX_POSITIONS = getattr(config, "MAX_POSITIONS", 5)
MIN_POSITION_SIZE = getattr(config, "MIN_POSITION_SIZE", 40)
MAX_POSITION_SIZE = getattr(config, "MAX_POSITION_SIZE", 200)
MAX_DAILY_LOSS_PCT = getattr(config, "MAX_DAILY_LOSS_PCT", 0.05)
POLL_INTERVAL = getattr(config, "POLL_INTERVAL", 3)
ENTRY_BUFFER_PCT = getattr(config, "ENTRY_BUFFER_PCT", 0.01)
MAX_ENTRY_SLIPPAGE = getattr(config, "MAX_ENTRY_SLIPPAGE", 0.04)
RESCAN_TIMES = getattr(config, "RESCAN_TIMES", ["10:30", "11:30"])

_LEV_PATTERN = re.compile(r'(2X|3X|BULL|BEAR)$', re.IGNORECASE)
_LEV_SUFFIXES = getattr(config, "LEVERAGED_ETF_SUFFIXES", ("BULL", "BEAR"))

# ── Clients ──
_ALPACA_PAPER = getattr(config, "ALPACA_PAPER", False)
trading_client = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=_ALPACA_PAPER)
data_client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)

_LOG_FILE = os.path.join(_ver_dir, "live_trade.log")

# ── Data feed ──
DATA_FEED = DataFeed.IEX
_cfg_feed = getattr(config, "DATA_FEED", "iex").lower()
if _cfg_feed == "sip":
    DATA_FEED = DataFeed.SIP


# ── Logging ──
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


def is_leveraged_etf(symbol: str) -> bool:
    if _LEV_PATTERN.search(symbol):
        return True
    if _LEV_SUFFIXES and len(symbol) > 3 and symbol[-1] in _LEV_SUFFIXES:
        return True
    if any(symbol.startswith(p) for p in (
        "TQQQ", "SQQQ", "UPRO", "SPXU", "TNA", "TZA", "MSTU", "MSTZ",
        "CONL", "NAIL", "WEBL", "FNGU", "FNGD", "SOXL", "SOXS", "TECL",
        "TECS", "UDOW", "SDOW", "UMDD", "SMDD", "TQQ", "SQQ", "YINN",
        "YANG", "CURE", "LABD", "LABU", "DRN", "DRV", "DGP", "DGZ",
        "BOIL", "KOLD", "NUGT", "DUST", "JNUG", "JDST", "GLL", "UGL",
    )):
        return True
    return False


# ── Rejection analysis ──
def analyze_alpaca_rejection(error_msg: str) -> dict:
    msg = str(error_msg).lower()
    result = {"category": "unknown", "action": "skip", "retry": False, "detail": str(error_msg)}

    if "insufficient buying power" in msg or "not enough buying power" in msg:
        result = {"category": "buying_power", "action": "reduce_size", "retry": True,
                  "detail": "资金不足"}
    elif "pattern day trader" in msg or "pdt" in msg:
        result = {"category": "pdt", "action": "stop_trading", "retry": False,
                  "detail": "PDT规则限制"}
    elif "not tradable" in msg or "not fractionable" in msg or "halted" in msg:
        result = {"category": "not_tradable", "action": "remove_symbol", "retry": False,
                  "detail": "股票不可交易"}
    elif "too small" in msg or "minimum quantity" in msg:
        result = {"category": "qty_small", "action": "increase_qty", "retry": True,
                  "detail": "数量太小"}
    elif "not allowed to short" in msg or ("short" in msg and "not allowed" in msg):
        result = {"category": "no_position", "action": "clear_position", "retry": False,
                  "detail": "无仓位可卖(Alpaca已清仓)"}
    elif "insufficient qty" in msg or "insufficient quantity" in msg:
        result = {"category": "insufficient_qty", "action": "sync_qty", "retry": True,
                  "detail": "Alpaca实际股数不足"}
    elif "429" in msg or "rate limit" in msg:
        result = {"category": "rate_limit", "action": "wait_retry", "retry": True,
                  "detail": "API限频"}
    elif "limit_price must be" in msg and "stop_price" in msg:
        result = {"category": "oco_invalid", "action": "skip", "retry": False,
                  "detail": "OCO limit_price<=stop_price"}
    elif "market is closed" in msg:
        result = {"category": "market_closed", "action": "skip", "retry": False,
                  "detail": "市场已关闭"}
    return result


# ── Position dataclass (9 fields vs stonewang's 30+) ──
@dataclass
class Position:
    symbol: str
    shares: int
    entry_price: float
    stop_price: float       # max($0.15, entry × 1.5%)
    target_price: float     # entry + (entry - stop) × 2.5
    oco_order_id: str = None
    entry_time: dt.datetime = None
    gap_pct: float = 0.0
    trade_type: str = "first"  # "first" or "reentry"
    highest: float = 0.0

    def __post_init__(self):
        if self.highest == 0.0:
            self.highest = self.entry_price


# ── DRY_RUN mode ──
DRY_RUN = getattr(config, "DRY_RUN", False)

@dataclass
class MockOrder:
    id: str
    symbol: str
    qty: int
    side: str
    order_type: str  # "market" / "oco"
    limit_price: float = 0.0
    stop_price: float = 0.0
    status: str = "new"
    filled_qty: int = 0
    filled_price: float = 0.0

dry_run_orders: dict[str, MockOrder] = {}


def _dry_run_get_price(symbol):
    from alpaca.data.requests import StockLatestTradeRequest
    try:
        trade = data_client.get_stock_latest_trade(StockLatestTradeRequest(
            symbol_or_symbols=symbol, feed=DATA_FEED))
        if isinstance(trade, dict):
            return float(trade[symbol].price)
        return float(trade.price)
    except Exception:
        return None


# ── Stop/Target calculation ──
def calc_stop_and_target(entry_price: float) -> tuple[float, float]:
    """按价格分档止损止盈"""
    if STOP_TIERS:
        for min_p, max_p, stop_pct, target_pct in STOP_TIERS:
            if min_p <= entry_price < max_p:
                stop_price = round(entry_price * (1 - stop_pct), 2)
                target_price = round(entry_price * (1 + target_pct), 2)
                return stop_price, target_price
    # Fallback
    return round(entry_price * 0.97, 2), round(entry_price * 1.03, 2)


# ── Bar accumulator ──
class BarAccumulator:
    def __init__(self):
        self._lock = threading.Lock()
        self._seen_ts = defaultdict(set)
        self._minute_bars = defaultdict(list)

    def add_bar(self, symbol, bar):
        with self._lock:
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
            return True

    def get_1min_bars(self, symbol):
        with self._lock:
            return list(self._minute_bars.get(symbol, []))


# ── WebSocket ──
class _Bar:
    pass

class StreamState:
    def __init__(self, accumulator):
        self.accumulator = accumulator
        self._stream = None
        self._running = False
        self._last_bar_time = 0

    def start(self, symbols):
        try:
            from alpaca.data.live.stock import StockDataStream
            self._stream = StockDataStream(
                config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, feed=DATA_FEED)
            for sym in symbols:
                self._stream.subscribe_bars(_on_bar, sym)
                self._stream.subscribe_trades(_on_trade, sym)
            self._running = True
            t = threading.Thread(target=self._stream.run, daemon=True)
            t.start()
            log(f"WebSocket stream started for {len(symbols)} symbols")
        except ImportError:
            log("StockDataStream not available, falling back to polling only")
        except Exception as e:
            log(f"WebSocket start error: {e}, falling back to polling only")

    def stop(self):
        self._running = False
        if self._stream:
            try:
                self._stream.stop()
            except Exception:
                pass

    def update_symbols(self, symbols):
        if not self._running or not self._stream:
            return
        try:
            for sym in symbols:
                self._stream.subscribe_bars(_on_bar, sym)
                self._stream.subscribe_trades(_on_trade, sym)
        except Exception as e:
            log(f"WebSocket subscribe error: {e}")

    def restart(self, symbols):
        log("WebSocket: restarting stream...")
        self.stop()
        time.sleep(2)
        self._last_bar_time = time.time()
        self.start(symbols)


_stream_state: StreamState | None = None

async def _on_bar(bar):
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
        _stream_state._last_bar_time = time.time()

async def _on_trade(trade):
    pass


# ── Data helpers ──
def get_snapshots(symbols):
    request = StockSnapshotRequest(symbol_or_symbols=symbols, feed=DATA_FEED)
    return data_client.get_stock_snapshot(request)


def _get_account_equity():
    try:
        acct = trading_client.get_account()
        return float(acct.equity)
    except Exception:
        return 0.0


def _get_buying_power():
    try:
        acct = trading_client.get_account()
        return float(acct.buying_power)
    except Exception:
        return 0.0


# ── Order helpers ──
def _wait_order_filled(order_id: str, timeout: float = 10) -> bool:
    """Wait for order to reach filled/canceled/rejected state."""
    if DRY_RUN:
        mo = dry_run_orders.get(order_id)
        return mo and mo.status == "filled"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            order = trading_client.get_order_by_id(order_id)
            if order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                return True
            if order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
                return False
        except Exception:
            pass
        time.sleep(0.5)
    return False


def get_order_filled_qty(order_id: str) -> int:
    if DRY_RUN:
        mo = dry_run_orders.get(order_id)
        return mo.filled_qty if mo else 0
    try:
        order = trading_client.get_order_by_id(order_id)
        return int(float(order.filled_qty)) if order.filled_qty else 0
    except Exception:
        return 0


def get_order_filled_price(order_id: str) -> float:
    if DRY_RUN:
        mo = dry_run_orders.get(order_id)
        return mo.filled_price if mo else 0.0
    try:
        order = trading_client.get_order_by_id(order_id)
        return float(order.filled_avg_price) if order.filled_avg_price else 0.0
    except Exception:
        return 0.0


def cancel_order(order_id: str) -> bool:
    if DRY_RUN:
        mo = dry_run_orders.get(order_id)
        if mo:
            mo.status = "canceled"
        return True
    try:
        trading_client.cancel_order_by_id(order_id)
        return True
    except Exception:
        return False


# ── Entry detection: 3-bar confirmation (from stonewang 1.2) ──
def check_entry_1min(symbol, open_price, accumulator, is_reentry=False):
    """3-bar pullback confirmation: bottom bar + 3 confirm bars (low>bottom, close>bottom, >=1 bullish).
    is_reentry=True: find any local pullback (not necessarily below open_price)."""
    bars = accumulator.get_1min_bars(symbol)
    if len(bars) < 4:
        return 0, False

    if is_reentry:
        # Re-entry: find local pullback (lowest point in recent bars)
        pullback_idx = 0
        pullback_price = bars[0]["low"]
        for i in range(1, len(bars)):
            if bars[i]["low"] < pullback_price:
                pullback_idx = i
                pullback_price = bars[i]["low"]
        # No pullback if all bars same low
        if pullback_idx >= len(bars) - 3:
            return 0, False
    else:
        # First entry: must break below open_price
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

    # Look for 3 confirmation bars after the pullback
    confirm_count = 0
    bullish_count = 0
    for i in range(pullback_idx + 1, len(bars)):
        bar = bars[i]
        bar_low = bar["low"]
        bar_close = bar["close"]
        bar_open = bar.get("open", 0.0)

        if is_reentry:
            # Deeper local bottom → reset
            if bar_low < pullback_price:
                pullback_idx = i
                pullback_price = bar_low
                confirm_count = 0
                bullish_count = 0
                continue
        else:
            # Deeper bottom below open_price → reset
            if bar_low < open_price and bar_low < pullback_price:
                pullback_idx = i
                pullback_price = bar_low
                confirm_count = 0
                bullish_count = 0
                continue

        # Still in bottom zone → skip
        if bar_low <= pullback_price or bar_close <= pullback_price:
            continue

        # Valid confirmation bar
        confirm_count += 1
        if bar_close > bar_open:
            bullish_count += 1

        # Need 3 confirm bars with at least 1 bullish
        if confirm_count >= 3 and bullish_count >= 1:
            return pullback_price, True

    return 0, False


# ── Buy ──
def place_buy_market(symbol, shares):
    """Market buy. Returns (order, actual_shares, reject_category)."""
    if DRY_RUN:
        oid = f"DRY-BM-{uuid4().hex[:8]}"
        price = _dry_run_get_price(symbol) or 0
        fill_price = round(price * 1.005, 2) if price else 0
        mock = MockOrder(id=oid, symbol=symbol, qty=shares, side="buy",
                         order_type="market", status="filled",
                         filled_qty=shares, filled_price=fill_price)
        dry_run_orders[oid] = mock
        log(f"[DRY] BUY {symbol} {shares} @ ~${fill_price:.2f}")
        return mock, shares, None

    for attempt in range(3):
        try:
            order = trading_client.submit_order(MarketOrderRequest(
                symbol=symbol, qty=shares, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            ))
            log(f"BUY {symbol} {shares} -> order {order.id}")
            filled = _wait_order_filled(str(order.id), timeout=15)
            if filled:
                filled_qty = get_order_filled_qty(str(order.id))
                fill_price = get_order_filled_price(str(order.id))
                actual = filled_qty if filled_qty > 0 else shares
                log(f"BUY CONFIRMED: {symbol} {actual}sh @ ${fill_price:.4f}")
                return order, actual, None
            else:
                log(f"{YELLOW}BUY TIMEOUT: {symbol} order {order.id}{RESET}")
                return order, shares, None
        except Exception as e:
            analysis = analyze_alpaca_rejection(e)
            last_reject = analysis["category"]
            log(f"BUY REJECTED ({attempt+1}/3) {symbol}: {analysis['detail']}")
            if analysis["category"] == "pdt":
                return None, shares, "pdt"
            if analysis["category"] == "buying_power" and attempt < 2:
                try:
                    bp = float(trading_client.get_account().buying_power)
                    cur_price = 0
                    try:
                        snap = data_client.get_stock_snapshot(
                            StockSnapshotRequest(symbol_or_symbols=symbol, feed=DATA_FEED))
                        cur_price = float(snap[symbol].latest_quote.ask_price)
                    except Exception:
                        pass
                    if cur_price > 0:
                        new_shares = max(1, int(bp / cur_price))
                        if new_shares < shares:
                            log(f"  买不了{shares}股, 降至{new_shares}股")
                            shares = new_shares
                    else:
                        shares = max(1, shares // 2)
                except Exception:
                    shares = max(1, shares // 2)
                continue
            if analysis["category"] in ("rate_limit", "network") and attempt < 2:
                time.sleep(5)
                continue
            if not analysis["retry"] or attempt >= 2:
                return None, shares, last_reject
    return None, shares, "retry_exhausted"


# ── OCO exit order ──
def place_oco_exit(symbol, shares, target_price, stop_price):
    """Place OCO: take_profit at target_price + stop_loss at stop_price.
    Returns (order_id, None) on success, or (None, error_msg) on failure."""
    stop_limit_price = round(stop_price * (1 - STOP_LIMIT_BUFFER), 2)

    # Pre-validate: target must be > stop
    if target_price <= stop_price:
        msg = f"OCO invalid: target ${target_price:.2f} <= stop ${stop_price:.2f}"
        log(f"{RED}{msg}{RESET}")
        return None, msg

    if DRY_RUN:
        oid = f"DRY-OCO-{uuid4().hex[:8]}"
        mock = MockOrder(id=oid, symbol=symbol, qty=shares, side="sell",
                         order_type="oco", limit_price=target_price,
                         stop_price=stop_price, status="new")
        dry_run_orders[oid] = mock
        log(f"[DRY] OCO {symbol} {shares} target=${target_price:.2f} stop=${stop_price:.2f}")
        return oid, None

    try:
        order = trading_client.submit_order(LimitOrderRequest(
            symbol=symbol,
            qty=shares,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            limit_price=target_price,
            order_class=OrderClass.OCO,
            take_profit=TakeProfitRequest(limit_price=target_price),
            stop_loss=StopLossRequest(
                stop_price=stop_price,
                limit_price=stop_limit_price,
            ),
        ))
        order_id = str(order.id)
        log(f"OCO PLACED: {symbol} {shares}sh target=${target_price:.2f} stop=${stop_price:.2f} stop_limit=${stop_limit_price:.2f} -> {order_id}")
        return order_id, None
    except Exception as e:
        analysis = analyze_alpaca_rejection(e)
        log(f"{RED}OCO REJECTED {symbol}: {analysis['detail']}{RESET}")
        return None, str(e)


# ── Force sell (for EOD / circuit breaker) ──
def force_sell_position(symbol, shares):
    """Market sell all shares. Returns (actual_shares_sold, fill_price)."""
    if shares <= 0:
        return 0, 0

    if DRY_RUN:
        oid = f"DRY-FS-{uuid4().hex[:8]}"
        price = _dry_run_get_price(symbol) or 0
        fill_price = round(price * 0.99, 2) if price else 0
        mock = MockOrder(id=oid, symbol=symbol, qty=shares, side="sell",
                         order_type="market", status="filled",
                         filled_qty=shares, filled_price=fill_price)
        dry_run_orders[oid] = mock
        log(f"[DRY] FORCE SELL {symbol} {shares} @ ~${fill_price:.2f}")
        return shares, fill_price

    # Cancel any open orders for this symbol first
    try:
        open_orders = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
        for o in open_orders:
            if o.symbol == symbol and o.side == OrderSide.SELL:
                cancel_order(str(o.id))
                log(f"  Cancelled sell order {o.id}")
    except Exception:
        pass

    for attempt in range(3):
        try:
            order = trading_client.submit_order(MarketOrderRequest(
                symbol=symbol, qty=shares, side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            ))
            log(f"FORCE SELL {symbol} {shares} -> order {order.id}")
            filled = _wait_order_filled(str(order.id), timeout=15)
            if filled:
                actual = get_order_filled_qty(str(order.id))
                fill_price = get_order_filled_price(str(order.id))
                log(f"FORCE SELL CONFIRMED: {symbol} {actual}sh @ ${fill_price:.4f}")
                return actual, fill_price
            else:
                log(f"{YELLOW}FORCE SELL TIMEOUT: {symbol} attempt {attempt+1}{RESET}")
        except Exception as e:
            analysis = analyze_alpaca_rejection(e)
            log(f"FORCE SELL REJECTED {symbol}: {analysis['detail']}")
            if analysis["category"] == "no_position":
                return shares, 0  # Alpaca already cleared it
            if analysis["category"] in ("rate_limit", "network") and attempt < 2:
                time.sleep(3)
                continue

    # Last resort: close_position
    try:
        result = trading_client.close_position(symbol)
        log(f"CLOSE POSITION {symbol}: {result}")
        return shares, 0
    except Exception as e:
        log(f"{RED}CLOSE POSITION FAILED {symbol}: {e}{RESET}")
        return 0, 0


# ── Check OCO fill ──
def check_oco_fill(oco_order_id, symbol):
    """Check if an OCO order has filled. Returns (filled, leg_type, fill_price, fill_qty).
    leg_type: "take_profit" or "stop_loss" or None."""
    if DRY_RUN:
        mo = dry_run_orders.get(oco_order_id)
        if not mo or mo.status != "filled":
            # Simulate fill based on current price
            price = _dry_run_get_price(symbol) or 0
            if price > 0 and mo:
                if price >= mo.limit_price:
                    mo.status = "filled"
                    mo.filled_qty = mo.qty
                    mo.filled_price = mo.limit_price
                    return True, "take_profit", mo.limit_price, mo.qty
                elif price <= mo.stop_price:
                    mo.status = "filled"
                    mo.filled_qty = mo.qty
                    mo.filled_price = mo.stop_price
                    return True, "stop_loss", mo.stop_price, mo.qty
            return False, None, 0, 0
        leg = "take_profit" if mo.filled_price >= mo.limit_price * 0.99 else "stop_loss"
        return True, leg, mo.filled_price, mo.filled_qty

    try:
        order = trading_client.get_order_by_id(oco_order_id)
        if order.status == OrderStatus.FILLED:
            fill_price = float(order.filled_avg_price) if order.filled_avg_price else 0
            fill_qty = int(float(order.filled_qty)) if order.filled_qty else 0
            leg = "take_profit"  # limit fill = take profit
            return True, leg, fill_price, fill_qty
        elif order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
            # OCO LIMIT canceled — check if the companion STOP_LIMIT filled (stop loss)
            # When stop loss triggers, Alpaca cancels the LIMIT leg and fills the STOP_LIMIT leg
            try:
                closed_orders = trading_client.get_orders(
                    GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=50))
                for o in closed_orders:
                    if (o.symbol == symbol and o.side == OrderSide.SELL
                            and o.status == OrderStatus.FILLED
                            and getattr(o, 'order_class', None) == OrderClass.OCO
                            and o.order_type in (OrderType.STOP_LIMIT, OrderType.STOP)):
                        fill_price = float(o.filled_avg_price) if o.filled_avg_price else 0
                        fill_qty = int(float(o.filled_qty)) if o.filled_qty else 0
                        if fill_qty > 0:
                            return True, "stop_loss", fill_price, fill_qty
            except Exception:
                pass
            # No filled companion found — check if position still exists
            try:
                pos = trading_client.get_open_position(symbol)
                # Position still exists → genuine external cancellation
                return True, "canceled", 0, 0
            except Exception:
                # Position gone → OCO filled but we couldn't find the fill
                # Look for any recent filled sell for this symbol
                try:
                    for o in closed_orders:
                        if (o.symbol == symbol and o.side == OrderSide.SELL
                                and o.status == OrderStatus.FILLED):
                            fill_price = float(o.filled_avg_price) if o.filled_avg_price else 0
                            fill_qty = int(float(o.filled_qty)) if o.filled_qty else 0
                            if fill_qty > 0:
                                leg = "stop_loss"  # assume stop loss if not take profit
                                return True, leg, fill_price, fill_qty
                except Exception:
                    pass
                return True, "canceled", 0, 0
        return False, None, 0, 0
    except Exception:
        return False, None, 0, 0


# ── Scan gaps ──
def scan_gaps():
    """Scan for gap-down stocks matching criteria."""
    try:
        all_symbols = get_tradable_symbols()
        all_symbols = [s for s in all_symbols if not is_leveraged_etf(s)]
        log(f"Scanning {len(all_symbols)} symbols for gaps...")

        today = pd.Timestamp.now(tz="America/New_York")
        client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
        df = scan_gaps_batch(client, today, all_symbols, batch_size=500)

        if df.empty:
            return []

        # Filter: gap down >= threshold
        gap_min = getattr(config, "GAP_THRESHOLD", 0.10)
        gap_max = getattr(config, "GAP_MAX", 1.0)
        price_min = getattr(config, "PRICE_MIN", 1.0)
        price_max = getattr(config, "PRICE_MAX", 20.0)
        min_dollar_vol = getattr(config, "MIN_DOLLAR_VOLUME", 100000)

        mask = (
            (df["gap_pct"] >= gap_min) &
            (df["gap_pct"] <= gap_max) &
            (df["open_price"] >= price_min) &
            (df["open_price"] <= price_max) &
            (df["prev_volume"] * df["prev_close"] >= min_dollar_vol)
        )
        filtered = df[mask].sort_values("gap_pct", ascending=False)

        results = []
        for _, row in filtered.iterrows():
            results.append({
                "symbol": row["symbol"],
                "open_price": float(row["open_price"]),
                "prev_close": float(row["prev_close"]),
                "gap_pct": float(row["gap_pct"]),
                "prev_volume": int(row.get("prev_volume", 0)),
            })
        return results
    except Exception as e:
        log(f"Scan error: {e}")
        return []


# ── State save/load ──
def save_state(positions, candidates, daily_trades, trades_detail):
    state = {
        "updated": dt.datetime.now().isoformat(),
        "version": "rossway_0.1",
        "daily_trades": daily_trades,
        "candidates": [
            {"symbol": c["symbol"], "open_price": c["open_price"],
             "prev_close": c["prev_close"], "gap_pct": round(c["gap_pct"], 4)}
            for c in candidates
        ],
        "positions": [
            {
                "symbol": p.symbol, "shares": p.shares,
                "entry_price": p.entry_price, "stop_price": p.stop_price,
                "target_price": p.target_price,
                "oco_order_id": p.oco_order_id,
                "entry_time": p.entry_time.isoformat() if p.entry_time else None,
                "gap_pct": p.gap_pct, "trade_type": p.trade_type,
                "highest": p.highest,
            }
            for p in positions
        ],
        "trades_detail": trades_detail[-50:],
    }
    state_path = os.path.join(_ver_dir, "live_state.json")
    try:
        with open(state_path, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


# ── Main trading day ──
def run_trading_day(force_close_time: dt.time, force_close_str: str, today_info: dict) -> dict:
    capital = _get_account_equity()
    log(f"Account equity: ${capital:.2f}")

    positions: list[Position] = []
    daily_trades = 0
    trades_detail = []
    candidates = []
    entry_checked = set()
    tp_exited_syms = set()  # symbols that exited via take_profit (eligible for re-entry with relaxed pullback)
    accumulator = BarAccumulator()
    day_highs = {}

    # Parse entry window
    entry_start = dt.time(*[int(x) for x in config.ENTRY_WINDOW_START.split(":")])
    entry_end = dt.time(*[int(x) for x in config.ENTRY_WINDOW_END.split(":")])

    # ── Pre-market scan ──
    log("Pre-market scanning for gap stocks...")
    preliminary = scan_gaps()
    if preliminary:
        log(f"Preliminary: {[c['symbol'] for c in preliminary[:5]]}... ({len(preliminary)} total)")

    # ── Wait for 9:31 ──
    log("Waiting for 9:31 to re-scan with regular session open prices...")
    while True:
        now = dt.datetime.now(tz=ZoneInfo("America/New_York"))
        if now.time() >= force_close_time:
            log("Market already closed.")
            return {"daily_trades": 0, "trades_detail": [], "candidates": []}
        if now.time() >= dt.time(9, 31, 30):
            break
        time.sleep(5)

    # ── Official scan ──
    candidates = scan_gaps()
    if not candidates:
        log("No gap stocks found.")
        return {"daily_trades": 0, "trades_detail": [], "candidates": []}

    max_monitored = min(getattr(config, "MAX_CANDIDATES", 20), len(candidates))
    candidates = candidates[:max_monitored]
    log(f"Candidates ({len(candidates)}): {[c['symbol'] for c in candidates]}")
    for c in candidates:
        log(f"  {c['symbol']}: gap +{c['gap_pct']:.1%}, open=${c['open_price']:.4f}")
        day_highs[c['symbol']] = c['open_price']

    # ── Backfill 1-min bars ──
    now_est = dt.datetime.now(tz=ZoneInfo("America/New_York"))
    today_open = now_est.replace(hour=9, minute=30, second=0, microsecond=0)
    try:
        req = StockBarsRequest(
            symbol_or_symbols=[c['symbol'] for c in candidates],
            timeframe=TimeFrame(1, TimeFrameUnit.Minute),
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
            log(f"Backfilled: {dict((c['symbol'], len(accumulator.get_1min_bars(c['symbol']))) for c in candidates)}")
    except Exception as e:
        log(f"Backfill error: {e}")

    # ── Start WebSocket ──
    global _stream_state
    _stream_state = StreamState(accumulator)
    _stream_state.start([c['symbol'] for c in candidates])

    # ── Recover existing Alpaca positions ──
    if not DRY_RUN:
        try:
            alpaca_positions = trading_client.get_all_positions()
            alpaca_orders = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
            for ap in alpaca_positions:
                sym = ap.symbol
                qty = int(float(ap.qty))
                avg_entry = float(ap.avg_entry_price)
                if sym in [p.symbol for p in positions]:
                    continue
                log(f"RECOVER: {sym} {qty}sh @ ${avg_entry:.4f}")
                cand = next((c for c in candidates if c["symbol"] == sym), None)
                open_price = cand["open_price"] if cand else avg_entry
                gap_pct = cand["gap_pct"] if cand else 0

                stop_price, target_price = calc_stop_and_target(avg_entry)

                # Find existing OCO order
                oco_id = None
                for ao in alpaca_orders:
                    if ao.symbol == sym and ao.side == OrderSide.SELL:
                        oco_id = str(ao.id)
                        log(f"RECOVER: Found OCO {ao.id}")
                        break

                pos = Position(
                    symbol=sym, shares=qty, entry_price=avg_entry,
                    stop_price=stop_price, target_price=target_price,
                    oco_order_id=oco_id,
                    entry_time=now_est, gap_pct=gap_pct,
                    trade_type="recovered",
                )
                positions.append(pos)
        except Exception as e:
            log(f"Recovery error: {e}")

    # ── Main polling loop ──
    log(f"Starting main loop (poll every {POLL_INTERVAL}s)...")
    last_rescan_idx = 0
    ws_reconnect_time = time.time() + 60  # Check WebSocket health every 60s

    while True:
        now_est = dt.datetime.now(tz=ZoneInfo("America/New_York"))
        now_time = now_est.time()

        # EOD check
        if now_time >= force_close_time:
            log(f"{RED}=== EOD FORCE CLOSE ({force_close_str}) ==={RESET}")
            for pos in positions[:]:
                log(f"EOD: Force closing {pos.symbol} {pos.shares}sh")
                if pos.oco_order_id:
                    cancel_order(pos.oco_order_id)
                sold, fill_price = force_sell_position(pos.symbol, pos.shares)
                if sold > 0:
                    if fill_price <= 0:
                        fill_price = pos.entry_price
                    pnl = (fill_price - pos.entry_price) * sold
                    trades_detail.append({
                        "symbol": pos.symbol, "entry": pos.entry_price,
                        "exit": fill_price, "shares": sold,
                        "pnl": round(pnl, 2), "reason": "force_close",
                        "trade_type": pos.trade_type,
                    })
                    daily_trades += 1
                    log(f"  {pos.symbol}: EOD sell {sold}sh, P&L ${pnl:.2f}")
            positions.clear()
            break

        # ── Check OCO fills ──
        for pos in positions[:]:
            if not pos.oco_order_id:
                continue
            filled, leg, fill_price, fill_qty = check_oco_fill(pos.oco_order_id, pos.symbol)
            if filled and leg != "canceled":
                daily_trades += 1
                if leg == "take_profit":
                    pnl = (fill_price - pos.entry_price) * pos.shares
                    log(f"{GREEN}TAKE PROFIT: {pos.symbol} {pos.shares}sh @ ${fill_price:.4f} P&L ${pnl:.2f}{RESET}")
                    tp_exited_syms.add(pos.symbol)
                    trades_detail.append({
                        "symbol": pos.symbol, "entry": pos.entry_price,
                        "exit": fill_price, "shares": pos.shares,
                        "pnl": round(pnl, 2), "reason": "take_profit",
                        "trade_type": pos.trade_type,
                    })
                else:  # stop_loss
                    pnl = (fill_price - pos.entry_price) * pos.shares
                    log(f"{RED}STOP LOSS: {pos.symbol} {pos.shares}sh @ ${fill_price:.4f} P&L ${pnl:.2f}{RESET}")
                    trades_detail.append({
                        "symbol": pos.symbol, "entry": pos.entry_price,
                        "exit": fill_price, "shares": pos.shares,
                        "pnl": round(pnl, 2), "reason": "stop_loss",
                        "trade_type": pos.trade_type,
                    })
                    entry_checked.add(pos.symbol)  # Stop loss → no re-entry today

                positions.remove(pos)
                pos.oco_order_id = None
            elif filled and leg == "canceled":
                # OCO was canceled externally — force sell
                log(f"{YELLOW}OCO CANCELED: {pos.symbol} — force selling{RESET}")
                sold, sell_price = force_sell_position(pos.symbol, pos.shares)
                if sold > 0:
                    daily_trades += 1
                    pnl = (sell_price - pos.entry_price) * sold
                    trades_detail.append({
                        "symbol": pos.symbol, "entry": pos.entry_price,
                        "exit": sell_price, "shares": sold,
                        "pnl": round(pnl, 2), "reason": "oco_canceled",
                        "trade_type": pos.trade_type,
                    })
                positions.remove(pos)

        # ── Circuit breaker ──
        if MAX_DAILY_LOSS_PCT > 0:
            realized_pnl = sum(t["pnl"] for t in trades_detail)
            if realized_pnl <= -(capital * MAX_DAILY_LOSS_PCT):
                log(f"{RED}CIRCUIT BREAKER: realized PnL ${realized_pnl:.2f} exceeds -{MAX_DAILY_LOSS_PCT*100:.0f}%${RESET}")
                entry_checked.update(c["symbol"] for c in candidates)

        # ── Mid-day rescan ──
        if last_rescan_idx < len(RESCAN_TIMES):
            rescan_str = RESCAN_TIMES[last_rescan_idx]
            rescan_time = dt.time(*[int(x) for x in rescan_str.split(":")])
            if now_time >= rescan_time:
                log(f"RE-SCAN at {rescan_str}...")
                new_candidates = scan_gaps()
                existing_syms = {c["symbol"] for c in candidates}
                added = 0
                for nc in new_candidates:
                    if nc["symbol"] not in existing_syms and nc["symbol"] not in entry_checked:
                        if not is_leveraged_etf(nc["symbol"]):
                            candidates.append(nc)
                            day_highs[nc["symbol"]] = nc["open_price"]
                            existing_syms.add(nc["symbol"])
                            added += 1
                log(f"Re-scan added {added} new candidates (total {len(candidates)})")
                _stream_state.update_symbols([c["symbol"] for c in candidates])
                last_rescan_idx += 1

        # ── Check new entries ──
        in_entry_window = entry_start <= now_time <= entry_end
        if in_entry_window or tp_exited_syms:
            n_open = len(positions)
            available_slots = MAX_POSITIONS - n_open
            if available_slots > 0:
                for c in candidates:
                    sym = c["symbol"]
                    if sym in entry_checked:
                        continue
                    if sym in [p.symbol for p in positions]:
                        continue
                    if available_slots <= 0:
                        break

                    is_reentry = sym in tp_exited_syms
                    # Outside entry window: only check re-entries
                    if not in_entry_window and not is_reentry:
                        continue

                    # Check pullback entry
                    pullback_price, confirmed = check_entry_1min(sym, c["open_price"], accumulator, is_reentry=is_reentry)
                    if not confirmed:
                        continue

                    # Calculate position size
                    buying_power = _get_buying_power()
                    remaining_candidates = sum(
                        1 for c2 in candidates
                        if c2["symbol"] not in entry_checked
                        and c2["symbol"] not in [p.symbol for p in positions]
                    )
                    remaining_candidates = max(remaining_candidates, 1)
                    slot_size = buying_power / remaining_candidates
                    slot_size = max(MIN_POSITION_SIZE, min(MAX_POSITION_SIZE, slot_size))

                    # Calculate shares
                    ask_price = pullback_price * (1 + ENTRY_BUFFER_PCT)
                    if ask_price > pullback_price * (1 + MAX_ENTRY_SLIPPAGE):
                        log(f"  {sym}: ask ${ask_price:.4f} exceeds slippage limit, skipping")
                        entry_checked.add(sym)
                        continue

                    shares = int(slot_size / ask_price)
                    if shares <= 0:
                        log(f"  {sym}: slot ${slot_size:.2f} too small for price ${ask_price:.4f}")
                        entry_checked.add(sym)
                        continue

                    # Buy
                    order, actual_shares, reject = place_buy_market(sym, shares)
                    if order is None:
                        if reject == "pdt":
                            entry_checked.update(c2["symbol"] for c2 in candidates)
                        else:
                            entry_checked.add(sym)
                        continue

                    fill_price = get_order_filled_price(str(order.id)) if not DRY_RUN else (order.filled_price if hasattr(order, 'filled_price') else ask_price)
                    if fill_price <= 0:
                        fill_price = ask_price

                    # Calculate stop/target from actual fill price
                    stop_price, target_price = calc_stop_and_target(fill_price)

                    # Place OCO exit
                    oco_id, oco_err = place_oco_exit(sym, actual_shares, target_price, stop_price)
                    if oco_err:
                        # OCO failed — place trailing stop as fallback, then force sell
                        log(f"{YELLOW}OCO failed for {sym}, force selling{RESET}")
                        sold, fs_price = force_sell_position(sym, actual_shares)
                        if sold > 0:
                            actual_exit = fs_price if fs_price > 0 else fill_price * 0.99
                            pnl = (actual_exit - fill_price) * sold
                            trades_detail.append({
                                "symbol": sym, "entry": fill_price,
                                "exit": actual_exit, "shares": sold,
                                "pnl": round(pnl, 2), "reason": "oco_failed",
                                "trade_type": "first",
                            })
                            daily_trades += 1
                        entry_checked.add(sym)
                        continue

                    # Track position
                    trade_type = "reentry" if is_reentry else "first"
                    pos = Position(
                        symbol=sym, shares=actual_shares, entry_price=fill_price,
                        stop_price=stop_price, target_price=target_price,
                        oco_order_id=oco_id, entry_time=now_est,
                        gap_pct=c["gap_pct"], trade_type=trade_type,
                    )
                    positions.append(pos)
                    daily_trades += 1
                    available_slots -= 1
                    entry_checked.add(sym)
                    tp_exited_syms.discard(sym)
                    reentry_tag = " (RE-ENTRY)" if is_reentry else ""
                    log(f"{GREEN}ENTERED: {sym} {actual_shares}sh @ ${fill_price:.4f} stop=${stop_price:.2f} target=${target_price:.2f} (gap {c['gap_pct']:.1%}){reentry_tag}{RESET}")

        # ── Update day highs ──
        if positions:
            try:
                syms = [p.symbol for p in positions]
                snaps = get_snapshots(syms)
                for pos in positions:
                    snap = snaps.get(pos.symbol)
                    if snap and snap.latest_trade:
                        cur = float(snap.latest_trade.price)
                        if cur > pos.highest:
                            pos.highest = cur
                        if pos.symbol in day_highs:
                            day_highs[pos.symbol] = max(day_highs[pos.symbol], cur)
                        else:
                            day_highs[pos.symbol] = cur
            except Exception:
                pass

        # ── WebSocket health check ──
        if _stream_state and _stream_state._running:
            if time.time() - _stream_state._last_bar_time > 60:
                log("WebSocket: 60s without bars, reconnecting...")
                _stream_state.restart([c["symbol"] for c in candidates])

        # ── Save state ──
        save_state(positions, candidates, daily_trades, trades_detail)
        time.sleep(POLL_INTERVAL)

    # ── End of day ──
    if _stream_state:
        _stream_state.stop()

    # Print summary
    log("=" * 60)
    total_pnl = sum(t["pnl"] for t in trades_detail)
    log(f"Day complete: {daily_trades} trades, P&L ${total_pnl:.2f}")
    for t in trades_detail:
        pnl_str = f"${t['pnl']:.2f}" if t['pnl'] >= 0 else f"-${abs(t['pnl']):.2f}"
        log(f"  {t['symbol']}: {t['trade_type']} entry=${t['entry']:.4f} exit=${t['exit']:.4f} {t['shares']}sh {pnl_str} ({t['reason']})")
    log("=" * 60)

    return {
        "daily_trades": daily_trades,
        "trades_detail": trades_detail,
        "candidates": candidates,
    }


# ── Scheduling ──
def get_next_trading_day():
    """Get next trading day info from Alpaca calendar."""
    try:
        now_est = dt.datetime.now(tz=ZoneInfo("America/New_York"))
        from alpaca.trading.requests import GetCalendarRequest
        calendar = trading_client.get_calendar(
            GetCalendarRequest(
                start=now_est.strftime("%Y-%m-%d"),
                end=(now_est + dt.timedelta(days=7)).strftime("%Y-%m-%d"),
            )
        )
        for day in calendar:
            day_date = day.date
            if hasattr(day_date, 'to_pydatetime'):
                day_date = day_date.to_pydatetime()
            if hasattr(day_date, 'date'):
                day_date_val = day_date.date()
            else:
                day_date_val = day_date
            close_time = day.close
            if hasattr(close_time, 'to_pytime'):
                close_time = close_time.to_pytime()
            open_time = day.open
            if hasattr(open_time, 'to_pytime'):
                open_time = open_time.to_pytime()
            if day_date_val >= now_est.date():
                return {
                    "date": day_date_val,
                    "open": open_time,
                    "close": close_time,
                }
    except Exception as e:
        log(f"Calendar error: {e}")
    return None


def test_connectivity():
    """Test API and data connectivity."""
    log("Testing data connectivity...")
    try:
        req = StockBarsRequest(
            symbol_or_symbols="AAPL",
            timeframe=TimeFrame.Day,
            start=pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(days=5),
            end=pd.Timestamp.now(tz="America/New_York"),
            feed=DATA_FEED,
        )
        bars = data_client.get_stock_bars(req)
        if not bars.df.empty:
            last = bars.df.iloc[-1]
            log(f"  AAPL daily_bar: O={last['open']:.2f} H={last['high']:.2f} L={last['low']:.2f} C={last['close']:.2f}")
        log("Connectivity OK!")
        return True
    except Exception as e:
        log(f"Connectivity FAILED: {e}")
        return False


# ── Main ──
def main():
    mode = "DRY RUN" if DRY_RUN else "LIVE"
    log("=" * 60)
    log(f"rossway_daytrade_0.1 — {mode} Trading")
    log(f"Stop/Target: tiered by price ({len(STOP_TIERS)} tiers)" if STOP_TIERS else "Stop/Target: 3%/3%")
    log(f"Max positions: {MAX_POSITIONS} | EOD: {config.FORCE_CLOSE_TIME}")
    log(f"Entry: {config.ENTRY_WINDOW_START}-{config.ENTRY_WINDOW_END} | 3-bar confirmation")
    log("=" * 60)

    if not test_connectivity():
        log("Aborting: connectivity test failed")
        return

    while True:
        next_day = get_next_trading_day()
        if not next_day:
            log("Cannot get calendar, sleeping 30 min...")
            time.sleep(1800)
            continue

        now_est = dt.datetime.now(tz=ZoneInfo("America/New_York"))
        day_date = next_day["date"]
        if hasattr(day_date, 'date') and callable(day_date.date):
            target_date = day_date.date()
        else:
            target_date = day_date

        if now_est.date() < target_date:
            log(f"Today ({now_est.date()}) is NOT a trading day. Next: {target_date}")
            open_dt = dt.datetime.combine(target_date, dt.time(9, 30), tzinfo=ZoneInfo("America/New_York"))
            smart_sleep_until(open_dt)
            continue

        # It's a trading day
        force_close_str = config.FORCE_CLOSE_TIME
        force_close_time = dt.time(*[int(x) for x in force_close_str.split(":")])

        # Wait for market open if needed
        market_open = dt.time(9, 30)
        if now_est.time() < market_open:
            open_dt = dt.datetime.combine(now_est.date(), market_open, tzinfo=ZoneInfo("America/New_York"))
            smart_sleep_until(open_dt)

        # Run trading day
        result = run_trading_day(force_close_time, force_close_str, next_day)

        # Wait until next trading day
        now_est = dt.datetime.now(tz=ZoneInfo("America/New_York"))
        next_day2 = get_next_trading_day()
        if next_day2:
            nd = next_day2["date"]
            if hasattr(nd, 'date') and callable(nd.date):
                next_date = nd.date()
            else:
                next_date = nd
            if next_date > now_est.date():
                open_dt = dt.datetime.combine(next_date, dt.time(9, 30), tzinfo=ZoneInfo("America/New_York"))
                log(f"Trading day complete. Next: {next_date}")
                smart_sleep_until(open_dt)
            else:
                time.sleep(300)
        else:
            time.sleep(300)


if __name__ == "__main__":
    main()
