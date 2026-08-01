"""stonewang_daytrade_1.2 — OCO ladder sell system.

Changes over stonewang_daytrade_1.1:
- NEW: OCO (One-Cancels-Other) pre-placed orders for T2-T6 ladder sells.
  After T1 fills → T2 OCO (limit=T2 target, stop=T1 fill×0.98) + trailing stop
  for remaining. OCO limit fills → advance tier, place next OCO + new trailing.
  OCO stop fills → don't advance, trailing protects remaining.
  Eliminates 5-second polling delay between tiers. Alpaca OCO locks shares once
  per pair (no double-lock issue).
- NEW: OCO_ENABLED config flag — True = v1.2 OCO mode, False = v1.1 polling fallback.
- NEW: tier_fill_prices field — stores actual fill prices per tier for OCO stop calculation.
- NEW: oco_order_ids field — tracks pre-placed OCO orders per position.
- NEW: place_oco_sell, check_oco_fill, place_oco_for_next_tier, cancel_all_oco_for_position functions.
- NEW: DRY_RUN support for OCO fill simulation (price vs limit_price/stop_price).
- NEW: INV-7 invariant check — OCO + trailing lock = remaining_shares.
- NEW: Recovery of OCO orders on startup from Alpaca open orders.
- MOD: Trailing stop fill handler cancels all pending OCO orders before proceeding.
- MOD: EOD force close cancels all OCO orders before force selling.
- MOD: Polling fallback — if no active OCO and price >= target, market sell + place OCO.

Changes over stonewang_daytrade_1.0 (inherited from 1.1):
- FIX: Ladder sell lock-up bug — protective stop locks all shares → partial market sell
  rejected → Method 3 sells entire position instead of tier fraction.
  Solution: cancel protective stop BEFORE tier sell, then re-place trailing stop for
  remaining shares. ~1-2 second naked window (acceptable risk).
- FIX: force_sell_position Method 3 sells qty (not total_qty) for partial sells —
  safety measure preventing accidental full liquidation even if Method 2 fails.
- FIX: force_sell_position Method 3 cancels only symbol-specific sell orders (not all
  account orders) — prevents disrupting other positions' protective stops.
- FIX: RED/GREEN/YELLOW/RESET ANSI color constants defined — was causing NameError
  on critical failure messages (3 errors in 7/27 live log).

Changes over 0.4.15:
- 8-tier profit targets with list-based fields (replaces 3-tier target_75/1125/150)
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

# ── ANSI color codes — defined FIRST for error logging availability ──
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"

import re
import json
import math
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
    StopLimitOrderRequest, TrailingStopOrderRequest,
    GetOrdersRequest, TakeProfitRequest, StopLossRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType, QueryOrderStatus, OrderStatus, OrderClass
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
_spec = importlib.util.spec_from_file_location("config", os.path.join(_ver_dir, "config.py"))
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)
sys.modules["config"] = config

from scanner import get_tradable_symbols
from strategy import (
    calc_atr, calc_stop_price, calc_price_at_retracement, calc_position_size,
    find_reentry_point,
)

# ── 0.4.10 Parameters ────────────────────────────────────────────────
ENTRY_LIMIT_BUFFER = getattr(config, "ENTRY_LIMIT_BUFFER", 0.01)
MAX_ENTRY_SLIPPAGE = getattr(config, "MAX_ENTRY_SLIPPAGE", 0.04)  # skip buy if ask > entry × (1 + this)
STOP_LIMIT_BUFFER = getattr(config, "STOP_LIMIT_BUFFER", 0.03)
FORCE_CLOSE_LIMIT_TIMEOUT = getattr(config, "FORCE_CLOSE_LIMIT_TIMEOUT", 120)
TARGET_LIMIT_BUFFER = 0.003
REENTRY_CUTOFF = getattr(config, "REENTRY_CUTOFF_TIME", "12:30")

# ── 1.2: OCO ladder sell parameters ──────────────────────────────────
OCO_ENABLED = getattr(config, "OCO_ENABLED", True)  # True = v1.2 OCO, False = v1.1 polling
OCO_STOP_BUFFER_PCT = getattr(config, "OCO_STOP_BUFFER_PCT", 0.02)  # OCO stop = prev_tier_fill * (1 - this)

# ── 0.4.10: Leveraged ETF detection ─────────────────────────────────
_LEV_PATTERN = re.compile(r'(2X|3X|BULL|BEAR)$', re.IGNORECASE)
_LEV_SUFFIXES = getattr(config, "LEVERAGED_ETF_SUFFIXES", ("BULL", "BEAR"))


def is_leveraged_etf(symbol: str) -> bool:
    if _LEV_PATTERN.search(symbol):
        return True
    if _LEV_SUFFIXES and len(symbol) > 3 and symbol[-1] in _LEV_SUFFIXES:
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

# ── Alpaca拒绝检测与自动修复 ──────────────────────────────────
# 常见拒绝原因及处理策略:
#   "insufficient buying power" → 减仓再试
#   "pattern day trader"        → 标记PDT, 停止当天新入场
#   "stock is not tradable"     → 移除候选, 不重试
#   "order quantity is too small" → 增加到最小1股
#   "stop price too close"      → 增大缓冲距离
#   "429 rate limit"            → 等待5秒重试

_REJECTION_LOG = []  # 记录所有拒绝事件

def analyze_alpaca_rejection(error_msg: str) -> dict:
    """分析Alpaca拒绝原因，返回分类+修复建议"""
    msg = str(error_msg).lower()
    result = {"category": "unknown", "action": "skip", "retry": False, "detail": str(error_msg)}

    if "insufficient buying power" in msg or "not enough buying power" in msg:
        result = {"category": "buying_power", "action": "reduce_size", "retry": True,
                  "detail": "资金不足，减小仓位重试"}
    elif "pattern day trader" in msg or "pdt" in msg:
        result = {"category": "pdt", "action": "stop_trading", "retry": False,
                  "detail": "PDT规则限制，停止当天新入场"}
    elif "not tradable" in msg or "not fractionable" in msg or "halted" in msg:
        result = {"category": "not_tradable", "action": "remove_symbol", "retry": False,
                  "detail": "股票不可交易，移除候选"}
    elif "too small" in msg or "minimum quantity" in msg or "quantity must be at least" in msg:
        result = {"category": "qty_small", "action": "increase_qty", "retry": True,
                  "detail": "数量太小，增加到1股重试"}
    elif "stop price too close" in msg or "stop_price must be" in msg:
        result = {"category": "stop_distance", "action": "increase_buffer", "retry": True,
                  "detail": "止损距离太近，增大缓冲"}
    elif "limit_price must be" in msg and "stop_price" in msg:
        result = {"category": "oco_invalid", "action": "adjust_params", "retry": True,
                  "detail": "OCO limit_price<=stop_price，自动调整参数重试"}
    elif "oco orders must be exit orders" in msg:
        result = {"category": "oco_structure", "action": "cancel_existing", "retry": True,
                  "detail": "OCO结构错误(oco orders must be exit orders)，取消已有订单重试"}
    elif "429" in msg or "rate limit" in msg or "too many requests" in msg:
        result = {"category": "rate_limit", "action": "wait_retry", "retry": True,
                  "detail": "API限频，5秒后重试"}
    elif "price" in msg and ("invalid" in msg or "outside" in msg):
        result = {"category": "price_invalid", "action": "adjust_price", "retry": True,
                  "detail": "价格无效，调整后重试"}
    elif "market is closed" in msg:
        result = {"category": "market_closed", "action": "skip", "retry": False,
                  "detail": "市场已关闭"}
    elif "not allowed to short" in msg or ("short" in msg and "not allowed" in msg):
        result = {"category": "no_position", "action": "clear_position", "retry": False,
                  "detail": "无仓位可卖(Alpaca已清仓)，清除本地幽灵仓位"}
    elif "insufficient qty" in msg or "insufficient quantity" in msg:
        result = {"category": "insufficient_qty", "action": "sync_qty", "retry": True,
                  "detail": "Alpaca实际股数不足，同步本地股数"}
    elif "connection" in msg or "timeout" in msg or "network" in msg:
        result = {"category": "network", "action": "wait_retry", "retry": True,
                  "detail": "网络问题，5秒后重试"}

    _REJECTION_LOG.append({
        "timestamp": dt.datetime.now().isoformat(),
        "error": str(error_msg)[:200],
        "category": result["category"],
        "action": result["action"],
    })
    return result


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
    # 1.0: 8-tier list-based fields
    targets: list = field(default_factory=list)
    sell_ratios: list = field(default_factory=list)
    trail_pcts: list = field(default_factory=list)
    reached_list: list = None
    sold_shares_list: list = None
    target_mode: str = "retracement"
    # Ladder sell: index of next tier to place (0=T1, 1=T2, ...; = len(targets) after all sold)
    next_tier_idx: int = 0
    # Naked position tracking: poll cycles without protective order (0 = protected)
    naked_since_poll: int = 0
    # 1.2: OCO ladder system — pre-placed OCO orders for T2+
    oco_order_ids: list = field(default_factory=list)  # [{order_id, tier_idx, qty, target_price, stop_price, leg_filled}]
    # 1.2: Actual fill prices per tier — needed for OCO stop price calculation
    tier_fill_prices: list = field(default_factory=list)

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


# ── DRY_RUN mode ─────────────────────────────────────────────────────
DRY_RUN = getattr(config, "DRY_RUN", False)

@dataclass
class MockOrder:
    """Simulated order for DRY_RUN mode."""
    id: str
    symbol: str
    qty: int
    side: str          # "buy" / "sell"
    order_type: str    # "limit" / "stop_limit" / "trailing_stop" / "market" / "oco"
    limit_price: float | None
    stop_price: float | None
    trail_percent: float | None
    status: str = "new"
    filled_qty: int = 0
    filled_price: float = 0.0
    oco_stop_limit_price: float | None = None  # 1.2: OCO stop leg limit price
    leg_type: str | None = None  # "limit" or "stop" — set when OCO fills in DRY_RUN

dry_run_orders: dict[str, MockOrder] = {}
_dry_run_day_highs: dict[str, float] = {}  # set from day_highs in run_trading_day
_order_cache: dict[str, dict] = {}  # batch order status cache (refreshed each polling cycle)

SLIPPAGE_ENTRY = getattr(config, "SLIPPAGE_ENTRY_PCT", 0.005)
SLIPPAGE_STOP = getattr(config, "SLIPPAGE_STOP_PCT", 0.02)
SLIPPAGE_TARGET = getattr(config, "SLIPPAGE_TARGET_PCT", 0.003)
SLIPPAGE_TRAILING = getattr(config, "SLIPPAGE_TRAILING_PCT", 0.01)
SLIPPAGE_FORCE = getattr(config, "SLIPPAGE_FORCE_CLOSE_PCT", 0.01)


def _dry_run_get_price(symbol):
    """Get latest trade price for fill simulation."""
    from alpaca.data.requests import StockLatestTradeRequest
    try:
        trade = data_client.get_stock_latest_trade(StockLatestTradeRequest(
            symbol_or_symbols=symbol, feed=DATA_FEED))
        if isinstance(trade, dict):
            return float(trade[symbol].price)
        return float(trade.price)
    except Exception:
        return None


# ── 8-tier target calculation ──────────────────────────────────────
def calc_targets(entry_price: float, open_price: float):
    retracements = getattr(config, "PROFIT_RETRACEMENT_TIERS", [0.10, 0.20, 0.35, 0.50, 0.75, 1.00, 1.25, 1.50])
    caps = getattr(config, "TARGET_CAP_TIERS", [0.01, 0.02, 0.035, 0.05, 0.08, 0.10, 0.13, 0.18])
    sell_ratios = getattr(config, "PARTIAL_SELL_RATIOS", [1/8]*8)
    trail_pcts = getattr(config, "TRAILING_STOP_PCTS", [0.01, 0.015, 0.02, 0.025, 0.03, 0.035, 0.04, 0.05])
    targets = []
    any_capped = False
    # When entry >= open OR gap too small for retracement targets, use capped mode.
    # Capped targets = entry * (1 + cap_pct) guarantee minimum profit distance.
    # Without this, when entry ≈ open (gap nearly filled), retracement targets
    # cluster around entry price with zero profit room.
    min_retrace_pct = getattr(config, "MIN_RETRACE_PCT", 0.03)  # 3% minimum gap for retracement
    use_capped = entry_price >= open_price or (open_price - entry_price) / entry_price < min_retrace_pct
    if use_capped:
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
    trail_pcts = getattr(config, "TRAILING_STOP_PCTS", [0.01, 0.015, 0.02, 0.025, 0.03, 0.035, 0.04, 0.05])
    if hasattr(pos, 'reached_list') and pos.reached_list:
        for ti in range(len(pos.reached_list) - 1, -1, -1):
            if pos.reached_list[ti]:
                return trail_pcts[ti] if ti < len(trail_pcts) else trail_pcts[-1]
    return trail_pcts[0]


# ── State export ────────────────────────────────────────────────────
def save_state(positions, candidates, daily_trades, daily_stopped,
               entry_checked, day_highs, accumulator, events_log,
               invariant_violation=False):
    all_syms = set([c["symbol"] for c in candidates] + [p.symbol for p in positions])
    state = {
        "updated": dt.datetime.now().isoformat(),
        "version": "1.1",
        "data_feed": "SIP" if DATA_FEED == DataFeed.SIP else "IEX",
        "ws_connected": _stream_state._running if _stream_state else False,
        "daily_trades": daily_trades,
        "daily_stopped": daily_stopped,
        "invariant_violation": invariant_violation,
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
                "next_tier_idx": p.next_tier_idx,
                "oco_order_ids": p.oco_order_ids if p.oco_order_ids else [],
                "tier_fill_prices": p.tier_fill_prices if p.tier_fill_prices else [],
                "reentry_bar_count": p.reentry_bar_count,
                "atr": p.atr,
                "time_limit_active": p.time_limit_active,
                "bar_count": p.bar_count,
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


def load_saved_positions():
    """Load position data from saved state file. Returns dict keyed by symbol."""
    state_path = os.path.join(_parent_dir, "live_state.json")
    if not os.path.exists(state_path):
        return {}
    try:
        with open(state_path, "r") as f:
            state = json.load(f)
        saved = {}
        for pd in state.get("positions", []):
            saved[pd["symbol"]] = pd
        return saved
    except Exception:
        return {}


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
                # 1.0: Chart targets use dict comprehension from retracement tiers
                retracements = getattr(config, "PROFIT_RETRACEMENT_TIERS", [0.10, 0.20, 0.35, 0.50, 0.75, 1.00, 1.25, 1.50])
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
        self._lock = threading.Lock()
        self._seen_ts = defaultdict(set)
        self._minute_bars = defaultdict(list)
        self._5min_cache = defaultdict(list)

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
            b = buckets[ts]
            is_last = (i == len(sorted_ts) - 1)
            # Only add completed bars (5 bars present) or non-last buckets
            # Last bucket may still be accumulating — only include if fully complete
            if not is_last and b["count"] >= 5:
                completed.append({
                    "timestamp": b["timestamp"], "open": b["open"],
                    "high": b["high"], "low": b["low"],
                    "close": b["close"], "volume": b["volume"],
                })
        self._5min_cache[symbol] = completed

    def get_5min_bars(self, symbol):
        with self._lock:
            return list(self._5min_cache.get(symbol, []))

    def get_1min_bars(self, symbol):
        with self._lock:
            return list(self._minute_bars.get(symbol, []))

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
        self._last_bar_time = 0  # P1-19: timestamp of last received bar for reconnect detection

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

    def restart(self, symbols):
        """Restart WebSocket stream (stop + start) for reconnect."""
        log("WebSocket: restarting stream...")
        self.stop()
        time.sleep(2)
        self._last_bar_time = time.time()
        self.start(symbols)


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
        _stream_state._last_bar_time = time.time()  # P1-19: track last bar for reconnect detection
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

def place_buy_market(symbol, shares):
    """Place a market buy order — used after pullback confirmation for immediate entry.
    Waits for Alpaca fill confirmation before returning.
    Returns (order, pdt_flag, actual_shares, reject_category):
      order: Alpaca order object or None
      pdt_flag: True if PDT rule triggered (caller should set local _pdt_detected)
      actual_shares: the qty actually filled (may differ from requested on partial fill)
      reject_category: last rejection category if order failed (None if succeeded; "rate_limit"/"network" = transient)
    """
    pdt_flag = False
    last_reject = None
    if DRY_RUN:
        oid = f"DRY-BM-{uuid4().hex[:8]}"
        price = _dry_run_get_price(symbol) or 0
        fill_price = round(price * (1 + SLIPPAGE_ENTRY), 2) if price else 0
        mock = MockOrder(id=oid, symbol=symbol, qty=shares, side="buy",
                         order_type="market", limit_price=None, stop_price=None, trail_percent=None,
                         status="filled", filled_qty=shares, filled_price=fill_price)
        dry_run_orders[oid] = mock
        log(f"[DRY] BUY MARKET {symbol} {shares} @ ~${fill_price:.2f} -> {oid}")
        return mock, False, shares, None
    for attempt in range(3):  # 最多3次尝试
        try:
            order = trading_client.submit_order(MarketOrderRequest(
                symbol=symbol, qty=shares, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            ))
            log(f"BUY MARKET {symbol} {shares} -> order {order.id}")
            # 等待Alpaca确认成交
            filled = _wait_order_filled(str(order.id), timeout=15)
            if filled:
                filled_qty = get_order_filled_qty(str(order.id))
                actual_fill_price = get_order_filled_price(str(order.id))
                if filled_qty > 0:
                    actual_shares = filled_qty
                else:
                    actual_shares = shares
                log(f"BUY MARKET CONFIRMED: {symbol} {actual_shares}sh @ ${actual_fill_price:.4f}")
                return order, False, actual_shares, None
            else:
                log(f"{YELLOW}BUY MARKET TIMEOUT: {symbol} order {order.id} not filled in 15s — returning order for pending check{RESET}")
                return order, False, shares, None
        except Exception as e:
            analysis = analyze_alpaca_rejection(e)
            last_reject = analysis["category"]
            log(f"BUY MARKET REJECTED ({attempt+1}/3) {symbol}: {analysis['detail']}")
            if analysis["category"] == "pdt":
                pdt_flag = True
                log(f"{RED}PDT规则触发！停止当天所有新入场{RESET}")
                return None, True, shares, "pdt"
            if analysis["category"] == "buying_power" and attempt < 2:
                # 根据实际可用资金精确计算可买股数，而非盲目减半
                try:
                    bp = float(trading_client.get_account().buying_power)
                    # 获取当前ask价作为参考
                    cur_price = 0
                    try:
                        snap = data_client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbol, feed=DATA_FEED))
                        cur_price = float(snap[symbol].latest_quote.ask_price) if snap and symbol in snap else 0
                    except Exception:
                        pass
                    if cur_price <= 0:
                        cur_price = _dry_run_get_price(symbol) or 0
                    if cur_price > 0:
                        new_shares = int(bp / cur_price)  # 向下取整
                        new_shares = max(1, new_shares)
                        if new_shares < shares:
                            log(f"  资金不足: {symbol} bp=${bp:.2f} ask=${cur_price:.4f} → 最多买{new_shares}股 (原{shares}股)")
                            shares = new_shares
                        else:
                            log(f"  资金重新计算: {symbol} bp=${bp:.2f} 可买{new_shares}股 ≥ 原定{shares}股")
                    else:
                        shares = max(1, shares // 2)
                        log(f"  资金不足且无法获取价格: {symbol} 减半至{shares}股")
                except Exception:
                    shares = max(1, shares // 2)
                    log(f"  资金不足且查询失败: {symbol} 减半至{shares}股")
                continue
            if analysis["category"] in ("rate_limit", "network") and attempt < 2:
                time.sleep(5)
                continue
            if not analysis["retry"] or attempt >= 2:
                return None, False, shares, last_reject
    return None, False, shares, last_reject


def place_buy_limit(symbol, shares, limit_price, timeout=10):
    """限价买入 — limit_price = entry_price * (1 + ENTRY_LIMIT_BUFFER)
    等待timeout秒成交，超时则取消并回退市价单。
    Returns (order, pdt_flag, actual_shares, reject_category) — 与place_buy_market相同签名
    """
    if DRY_RUN:
        oid = f"DRY-BL-{uuid4().hex[:8]}"
        price = _dry_run_get_price(symbol) or 0
        fill_price = round(min(price * (1 + ENTRY_LIMIT_BUFFER), limit_price), 2) if price else 0
        mock = MockOrder(id=oid, symbol=symbol, qty=shares, side="buy",
                         order_type="limit", limit_price=limit_price, stop_price=None, trail_percent=None,
                         status="filled", filled_qty=shares, filled_price=fill_price)
        dry_run_orders[oid] = mock
        log(f"[DRY] BUY LIMIT {symbol} {shares} @ ${limit_price:.2f} -> {oid} (filled @ ${fill_price:.2f})")
        return mock, False, shares, None

    # 提交限价买单
    try:
        order = trading_client.submit_order(LimitOrderRequest(
            symbol=symbol, qty=shares, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY, limit_price=round(limit_price, 2),
        ))
        log(f"BUY LIMIT {symbol} {shares} @ ${limit_price:.2f} -> order {order.id}")
    except Exception as e:
        analysis = analyze_alpaca_rejection(e)
        log(f"BUY LIMIT REJECTED {symbol}: {analysis['detail']} — falling back to market order")
        if analysis["category"] == "pdt":
            return None, True, shares, "pdt"
        # 限价单被拒，直接回退市价单
        return place_buy_market(symbol, shares)

    # 等待成交
    filled = _wait_order_filled(str(order.id), timeout=timeout)
    if filled:
        filled_qty = get_order_filled_qty(str(order.id))
        actual_fill_price = get_order_filled_price(str(order.id))
        actual_shares = filled_qty if filled_qty > 0 else shares
        log(f"BUY LIMIT FILLED: {symbol} {actual_shares}sh @ ${actual_fill_price:.4f}")
        return order, False, actual_shares, None

    # 超时未成交 — 取消限价单，回退市价单
    log(f"{YELLOW}BUY LIMIT TIMEOUT: {symbol} not filled in {timeout}s — cancel and fallback to market{RESET}")
    try:
        cancel_order(str(order.id))
        _wait_cancel_confirmed(str(order.id), timeout=3.0)
    except Exception:
        log(f"  Warning: failed to cancel limit order {order.id}, proceeding with market order anyway")

    return place_buy_market(symbol, shares)


def place_sell_limit(symbol, shares, price):
    """限价卖单 — 失败时微调价格重试"""
    if DRY_RUN:
        oid = f"DRY-S-{uuid4().hex[:8]}"
        mock = MockOrder(id=oid, symbol=symbol, qty=shares, side="sell",
                         order_type="limit", limit_price=price, stop_price=None, trail_percent=None)
        dry_run_orders[oid] = mock
        log(f"[DRY] SELL LIMIT {symbol} {shares} @ ${price:.2f} -> {oid}")
        return mock
    for attempt in range(2):
        try:
            order = trading_client.submit_order(LimitOrderRequest(
                symbol=symbol, qty=shares, side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY, limit_price=round(price, 2),
            ))
            log(f"SELL LIMIT {symbol} {shares} @ ${price:.2f} -> order {order.id}")
            return order
        except Exception as e:
            analysis = analyze_alpaca_rejection(e)
            log(f"SELL LIMIT REJECTED ({attempt+1}/2) {symbol}: {analysis['detail']}")
            if analysis["category"] == "price_invalid" and attempt < 1:
                price = round(price * 0.99, 2)  # 降1%重试
                log(f"  降价重试: limit=${price:.2f}")
                continue
            if analysis["category"] in ("rate_limit", "network") and attempt < 1:
                time.sleep(5)
                continue
            if not analysis["retry"] or attempt >= 1:
                return None
    return None


def place_sell_market(symbol, shares):
    """Market sell — 用于阶梯卖出和强制平仓。失败时自动重试3次。"""
    if DRY_RUN:
        oid = f"DRY-SM-{uuid4().hex[:8]}"
        price = _dry_run_get_price(symbol) or 0
        fill_price = round(price * (1 - SLIPPAGE_FORCE), 2) if price else 0
        mock = MockOrder(id=oid, symbol=symbol, qty=shares, side="sell",
                         order_type="market", limit_price=None, stop_price=None, trail_percent=None,
                         status="filled", filled_qty=shares, filled_price=fill_price)
        dry_run_orders[oid] = mock
        log(f"[DRY] SELL MARKET {symbol} {shares} @ ~${fill_price:.2f} -> {oid}")
        return mock
    for attempt in range(3):
        try:
            order = trading_client.submit_order(MarketOrderRequest(
                symbol=symbol, qty=shares, side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            ))
            log(f"SELL MARKET {symbol} {shares} -> order {order.id}")
            return order
        except Exception as e:
            analysis = analyze_alpaca_rejection(e)
            log(f"SELL MARKET REJECTED ({attempt+1}/3) {symbol}: {analysis['detail']}")
            if analysis["category"] in ("rate_limit", "network") and attempt < 2:
                time.sleep(5)
                continue
            if analysis["category"] == "qty_small" and attempt < 2:
                shares = max(1, shares)
                continue
            if not analysis["retry"] or attempt >= 2:
                log(f"{RED}SELL MARKET 最终失败: {symbol} — 仓位需人工干预!{RESET}")
                return None
    return None


def place_stop_limit_sell(symbol, shares, stop_price, limit_price):
    """止损限价单 — 失败时增大缓冲距离重试"""
    if DRY_RUN:
        oid = f"DRY-SL-{uuid4().hex[:8]}"
        mock = MockOrder(id=oid, symbol=symbol, qty=shares, side="sell",
                         order_type="stop_limit", limit_price=limit_price, stop_price=stop_price, trail_percent=None)
        dry_run_orders[oid] = mock
        log(f"[DRY] STOP-LIMIT {symbol} {shares} stop=${stop_price:.2f} limit=${limit_price:.2f} -> {oid}")
        return mock
    for attempt in range(2):
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
            analysis = analyze_alpaca_rejection(e)
            log(f"STOP-LIMIT REJECTED ({attempt+1}/2) {symbol}: {analysis['detail']}")
            if analysis["category"] == "stop_distance" and attempt < 1:
                # 不降低stop_price（不超过10%亏损上限），只加宽stop→limit缓冲
                buffer = STOP_LIMIT_BUFFER + 0.02
                limit_price = round(stop_price * (1 - buffer), 2)
                log(f"  加宽缓冲重试: stop=${stop_price:.2f} limit=${limit_price:.2f} buffer={buffer:.0%}")
                continue
            if analysis["category"] in ("rate_limit", "network") and attempt < 1:
                time.sleep(5)
                continue
            if not analysis["retry"] or attempt >= 1:
                return None
    return None


def place_trailing_stop_sell(symbol, shares, trail_percent):
    """Trailing stop — 失败时回退到stop-limit兜底保护"""
    if DRY_RUN:
        oid = f"DRY-TS-{uuid4().hex[:8]}"
        mock = MockOrder(id=oid, symbol=symbol, qty=shares, side="sell",
                         order_type="trailing_stop", limit_price=None, stop_price=None, trail_percent=trail_percent)
        dry_run_orders[oid] = mock
        log(f"[DRY] TRAILING STOP {symbol} {shares} trail={trail_percent:.1f}% -> {oid}")
        return mock
    for attempt in range(2):
        try:
            order = trading_client.submit_order(TrailingStopOrderRequest(
                symbol=symbol, qty=shares, side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                trail_percent=round(trail_percent, 1),
            ))
            log(f"TRAILING STOP {symbol} {shares} trail={trail_percent:.1f}% -> order {order.id}")
            return order
        except Exception as e:
            analysis = analyze_alpaca_rejection(e)
            log(f"TRAILING STOP REJECTED ({attempt+1}/2) {symbol}: {analysis['detail']}")
            if analysis["category"] in ("rate_limit", "network") and attempt < 1:
                time.sleep(5)
                continue
            # trailing stop失败 → 回退到stop-limit兜底
            if attempt >= 1 or not analysis["retry"]:
                # 用当前价格的trail_percent作为stop，3%缓冲作为limit
                cur_price = 0
                try:
                    snap = data_client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbol, feed=DATA_FEED))
                    cur_price = float(snap[symbol].latest_trade.price)
                except Exception:
                    pass
                if cur_price > 0:
                    fallback_stop = round(cur_price * (1 - trail_percent), 2)
                    fallback_limit = round(fallback_stop * 0.97, 2)
                    log(f"  回退兜底: stop-limit stop=${fallback_stop:.2f} limit=${fallback_limit:.2f}")
                    return place_stop_limit_sell(symbol, shares, fallback_stop, fallback_limit)
                else:
                    log(f"{RED}TRAILING STOP失败且无法兜底: {symbol} — 仓位无保护!{RESET}")
                    return None
    return None


def cancel_order(order_id):
    if DRY_RUN:
        if order_id in dry_run_orders:
            dry_run_orders[order_id].status = "canceled"
            del dry_run_orders[order_id]
            log(f"[DRY] CANCELLED order {order_id}")
        return
    try:
        trading_client.cancel_order_by_id(order_id)
        log(f"CANCELLED order {order_id}")
    except Exception as e:
        # 验证取消是否成功（可能订单已成交/已取消）
        try:
            order_obj = trading_client.get_order_by_id(order_id)
            if order_obj and order_obj.status not in ("canceled", "filled", "partially_filled", "rejected", "expired"):
                log(f"{YELLOW}CANCEL FAILED: order {order_id} still status={order_obj.status} — {e}{RESET}")
            else:
                log(f"CANCEL order {order_id}: already {order_obj.status}")
        except Exception:
            log(f"{YELLOW}CANCEL FAILED for {order_id} and cannot verify status — {e}{RESET}")


def _wait_cancel_confirmed(order_id, timeout=2.0, poll_interval=0.3):
    """等待Alpaca确认订单取消/成交/过期。收到确认立即返回，不阻塞超过timeout秒。
    Returns True if order is no longer active (cancelled/filled/rejected/expired).
    Returns False if timeout reached and order may still be active.
    Uses batch get_orders instead of individual get_order_by_id to save API quota."""
    if DRY_RUN:
        return order_id not in dry_run_orders
    start = time.time()
    while time.time() - start < timeout:
        # Check _order_cache first (free — no API call)
        cached = _order_cache.get(order_id)
        if cached and cached["status"] in ("canceled", "filled", "partially_filled", "rejected", "expired"):
            return True
        # Cache may not reflect latest cancel — refresh with 1 batch call
        try:
            fresh_orders = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500))
            for o in fresh_orders:
                oid = str(o.id)
                st = o.status.value if hasattr(o.status, 'value') else str(o.status)
                _order_cache[oid] = {
                    "status": st,
                    "filled_qty": int(float(o.filled_qty)) if o.filled_qty else 0,
                    "filled_avg_price": float(o.filled_avg_price) if o.filled_avg_price else 0.0,
                    "side": o.side.value if hasattr(o.side, 'value') else str(o.side),
                    "symbol": o.symbol,
                    "qty": float(o.qty) if o.qty else 0,
                    "order_type": o.order_type.value if hasattr(o.order_type, 'value') else str(o.order_type),
                    "order_class": o.order_class.value if hasattr(o.order_class, 'value') else str(o.order_class) if o.order_class else None,
                    "legs": [
                        {
                            "status": leg.status.value if hasattr(leg.status, 'value') else str(leg.status),
                            "filled_avg_price": float(leg.filled_avg_price) if leg.filled_avg_price else 0.0,
                            "order_type": leg.order_type.value if hasattr(leg.order_type, 'value') else str(leg.order_type),
                            "qty": float(leg.qty) if leg.qty else 0,
                            "filled_qty": int(float(leg.filled_qty)) if leg.filled_qty else 0,
                        }
                        for leg in (o.legs or [])
                    ] if o.legs else [],
                }
            # Check again with refreshed cache
            cached = _order_cache.get(order_id)
            if cached and cached["status"] in ("canceled", "filled", "partially_filled", "rejected", "expired"):
                return True
            if not cached:
                return True  # Order not found in any status = gone
        except Exception:
            # API error — assume cancel succeeded to avoid infinite block
            log(f"_wait_cancel_confirmed API error, assuming cancel OK: {order_id}")
            return True
        time.sleep(poll_interval)
    log(f"{YELLOW}CANCEL WAIT TIMEOUT: {order_id} still active after {timeout}s — proceeding anyway{RESET}")
    return False


def _wait_oco_confirmed(order_id, timeout=10.0, poll_interval=0.5):
    """等待Alpaca确认OCO订单已接受（status=new/held）。
    Returns True if order is confirmed accepted.
    Returns False if timeout or order is rejected/canceled."""
    if DRY_RUN:
        return order_id in dry_run_orders
    start = time.time()
    while time.time() - start < timeout:
        try:
            order_obj = trading_client.get_order_by_id(order_id)
            status = order_obj.status.value if hasattr(order_obj.status, 'value') else str(order_obj.status)
            if status in ("new", "held", "partially_filled", "filled"):
                return True
            if status in ("rejected", "canceled", "expired"):
                log(f"OCO order {order_id} rejected/canceled: {status}")
                return False
            # pending_new, accepted, etc. — still processing, wait
        except Exception as e:
            log(f"_wait_oco_confirmed error for {order_id}: {e}")
        time.sleep(poll_interval)
    log(f"{YELLOW}OCO CONFIRM TIMEOUT: {order_id} not confirmed after {timeout}s{RESET}")
    return False


def cancel_all_orders():
    """Cancel all open orders on the account."""
    if DRY_RUN:
        dry_run_orders.clear()
        return
    try:
        trading_client.cancel_orders()
        log("CANCEL ALL ORDERS: all open orders cancelled")
    except Exception as e:
        log(f"CANCEL ALL ORDERS error: {e}")


def close_all_positions():
    if DRY_RUN:
        log("[DRY] CLOSE ALL POSITIONS (no-op)")
        return
    try:
        positions = trading_client.get_all_positions()
        for pos in positions:
            log(f"EOD CLOSE: selling {pos.qty} {pos.symbol}")
            trading_client.close_position(pos.symbol)
    except Exception as e:
        log(f"Close positions error: {e}")


# ── 1.2: OCO (One-Cancels-Other) ladder sell functions ──────────────

def place_oco_sell(symbol, shares, target_price, stop_price, stop_limit_price):
    """Place an OCO sell order (limit leg = target_price, stop leg = stop_price/stop_limit_price).
    Uses LimitOrderRequest with order_class=OrderClass.OCO.
    Waits for Alpaca to confirm the order is accepted (status=new/held) before returning.
    Returns order object or None on failure. 2 retries with buffer increase on stop_distance rejection."""
    if DRY_RUN:
        oid = f"DRY-OCO-{uuid4().hex[:8]}"
        mock = MockOrder(id=oid, symbol=symbol, qty=shares, side="sell",
                         order_type="oco", limit_price=target_price,
                         stop_price=stop_price, trail_percent=None,
                         oco_stop_limit_price=stop_limit_price)
        dry_run_orders[oid] = mock
        log(f"[DRY] OCO SELL {symbol} {shares} limit=${target_price:.2f} stop=${stop_price:.2f} stop_limit=${stop_limit_price:.2f} -> {oid}")
        return mock

    for attempt in range(3):
        try:
            order = trading_client.submit_order(LimitOrderRequest(
                symbol=symbol, qty=shares, side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                limit_price=round(target_price, 2),
                order_class=OrderClass.OCO,
                take_profit=TakeProfitRequest(limit_price=round(target_price, 2)),
                stop_loss=StopLossRequest(stop_price=round(stop_price, 2),
                                          limit_price=round(stop_limit_price, 2)),
            ))
            # Wait for Alpaca to confirm the order is accepted
            confirmed = _wait_oco_confirmed(str(order.id), timeout=10.0)
            if confirmed:
                log(f"OCO SELL {symbol} {shares} limit=${target_price:.2f} stop=${stop_price:.2f} stop_limit=${stop_limit_price:.2f} -> order {order.id} CONFIRMED")
                return order
            else:
                log(f"{YELLOW}OCO SELL {symbol} order {order.id} not confirmed after 10s — treating as failed{RESET}")
                # Try to cancel the unconfirmed order to free shares
                try:
                    trading_client.cancel_order_by_id(order.id)
                except Exception:
                    pass
                # Fall through to retry or return None
                if attempt < 2:
                    continue
                return None
        except Exception as e:
            analysis = analyze_alpaca_rejection(e)
            log(f"OCO SELL REJECTED ({attempt+1}/3) {symbol}: {analysis['detail']}")
            if analysis["category"] == "stop_distance" and attempt < 2:
                # Increase stop buffer — widen stop_price away from current price
                buffer_increase = 0.01 * (attempt + 1)
                stop_price = round(stop_price * (1 - buffer_increase), 2)
                stop_limit_price = round(stop_price * 0.99, 2)
                log(f"  OCO stop buffer increase: stop=${stop_price:.2f} stop_limit=${stop_limit_price:.2f}")
                continue
            if analysis["category"] == "oco_invalid" and attempt < 2:
                # limit_price <= stop_price, adjust stop down
                stop_price = round(target_price - 0.01, 2)
                stop_limit_price = round(stop_price * 0.99, 2)
                log(f"  OCO price fix: stop→${stop_price:.2f} stop_limit→${stop_limit_price:.2f}")
                continue
            if analysis["category"] == "oco_structure" and attempt < 2:
                # OCO structure error — cancel existing orders and retry
                log(f"  OCO structure error: cancel existing sell orders for {symbol} and retry")
                try:
                    open_orders = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]))
                    for o in open_orders:
                        if o.side == OrderSide.SELL:
                            cancel_order(str(o.id))
                            _wait_cancel_confirmed(str(o.id), timeout=2.0)
                except Exception:
                    pass
                time.sleep(1)
                continue
            if analysis["category"] in ("rate_limit", "network") and attempt < 2:
                time.sleep(5)
                continue
            if not analysis["retry"] or attempt >= 2:
                log(f"{RED}OCO SELL FAILED: {symbol} — falling back to polling mode for this tier{RESET}")
                return None
    return None


def check_oco_fill(oco_entry):
    """Check if an OCO order has filled. Returns (filled, leg_type, fill_price).
    leg_type: "limit" (profit fill), "stop" (loss protection fill), "canceled" (order expired/canceled).
    Live: uses _order_cache + order legs to determine which leg filled (not price heuristic).
    DRY_RUN: simulate fill based on current price vs limit_price/stop_price."""
    order_id = oco_entry["order_id"]
    target_price = oco_entry["target_price"]
    stop_price = oco_entry["stop_price"]

    if DRY_RUN:
        mock = dry_run_orders.get(order_id)
        if not mock:
            return True, "canceled", 0.0
        if mock.status == "filled":
            fill_price = mock.filled_price
            # Use stored leg_type (set during simulation) instead of price heuristic
            if mock.leg_type:
                return True, mock.leg_type, fill_price
            # Fallback if leg_type not set (shouldn't happen)
            if fill_price >= target_price:
                return True, "limit", fill_price
            else:
                return True, "stop", fill_price
        # Simulate fill based on current price
        price = _dry_run_get_price(mock.symbol)
        if not price:
            return False, None, 0.0
        # Check limit leg: price >= target_price
        if price >= target_price:
            fill_price = round(price * (1 - SLIPPAGE_TARGET), 2)
            mock.status = "filled"
            mock.filled_qty = mock.qty
            mock.filled_price = fill_price
            mock.leg_type = "limit"
            log(f"[DRY] OCO LIMIT FILLED: {mock.symbol} {mock.qty} @ ${fill_price:.2f}")
            return True, "limit", fill_price
        # Check stop leg: price <= stop_price
        if price <= stop_price:
            fill_price = round(min(mock.oco_stop_limit_price or stop_price, price * (1 - SLIPPAGE_STOP)), 2)
            mock.status = "filled"
            mock.filled_qty = mock.qty
            mock.filled_price = fill_price
            mock.leg_type = "stop"
            log(f"[DRY] OCO STOP FILLED: {mock.symbol} {mock.qty} @ ${fill_price:.2f}")
            return True, "stop", fill_price
        return False, None, 0.0

    # Live mode — use _order_cache (no individual API call)
    cached = _order_cache.get(order_id)
    if not cached:
        return False, None, 0.0

    status = cached["status"]

    if status == "filled":
        # Use legs to determine which leg filled (accurate, not price heuristic)
        legs = cached.get("legs", [])
        if legs:
            for leg in legs:
                if leg["status"] == "filled" and leg["filled_avg_price"] > 0:
                    leg_type = leg["order_type"]
                    fill_price = leg["filled_avg_price"]
                    if leg_type in ("limit",):
                        return True, "limit", fill_price
                    elif leg_type in ("stop", "stop_limit"):
                        return True, "stop", fill_price
            # No filled leg found — parent is filled but no leg detail, use price fallback
            fill_price = cached["filled_avg_price"]
            if fill_price >= target_price:
                return True, "limit", fill_price
            else:
                return True, "stop", fill_price
        else:
            # No legs info — use price comparison (tighter than old 0.95 threshold)
            fill_price = cached["filled_avg_price"]
            if fill_price >= target_price:
                return True, "limit", fill_price
            else:
                return True, "stop", fill_price

    if status in ("canceled", "expired", "rejected"):
        return True, "canceled", 0.0

    return False, None, 0.0


def place_oco_for_next_tier(pos, tier_idx, prev_fill_price=None):
    """Place an OCO sell for the next tier (T2+).
    OCO limit_price = next tier target (止盈 — price rises to target → sell at profit).
    OCO stop_price = previous tier's actual fill price (止损触发 — price drops back to confirmed level → trigger stop).
    OCO stop_limit_price = stop_price × 0.99 (止损限价 — minimum acceptable price after stop triggers).
    Also cancels the current trailing stop, places a new one for remaining minus OCO qty,
    and records the OCO entry in pos.oco_order_ids.
    Returns oco_entry dict or None."""
    if not OCO_ENABLED:
        return None

    if tier_idx >= len(pos.targets) or pos.remaining_shares <= 0:
        return None

    target_price = pos.targets[tier_idx]
    tier_shares = math.ceil(pos.shares / 8) if pos.shares >= 8 else 1
    tier_shares = min(tier_shares, pos.remaining_shares)
    if tier_shares <= 0:
        return None

    # OCO stop_price = previous tier's actual fill price (confirmed level, acts as floor)
    # OCO stop_limit_price = stop_price × 0.99 (1% below the confirmed level)
    # Rationale: the fill price is the actual confirmed level — if price drops back,
    # the reversal is real and we should exit the tier shares quickly.
    # Prefer prev_fill_price (explicit arg), then tier_fill_prices (stored), then targets (fallback).
    if prev_fill_price and prev_fill_price > 0:
        stop_price = prev_fill_price
    elif tier_idx > 0 and len(pos.tier_fill_prices) >= tier_idx and pos.tier_fill_prices[tier_idx - 1] > 0:
        stop_price = pos.tier_fill_prices[tier_idx - 1]
    else:
        stop_price = pos.targets[tier_idx - 1] if tier_idx > 0 else pos.entry_price
    stop_limit_price = round(stop_price * 0.99, 2)

    # Ensure stop_price > 0 (safety)
    stop_price = max(stop_price, 0.01)
    stop_limit_price = max(stop_limit_price, 0.01)

    # Ensure target_price > stop_price, otherwise Alpaca rejects OCO
    if target_price <= stop_price:
        log(f"  OCO T{tier_idx+1}: target ${target_price:.2f} <= stop ${stop_price:.2f}, adjusting stop down")
        stop_price = round(target_price - 0.01, 2)
        stop_limit_price = round(stop_price * 0.99, 2)
        stop_price = max(stop_price, 0.01)
        stop_limit_price = max(stop_limit_price, 0.01)

    # Cancel current trailing/protective stop first (to unlock shares for OCO + new trailing)
    if pos.protective_order_id:
        cancel_order(pos.protective_order_id)
        log(f"OCO T{tier_idx+1}: cancelled protective stop {pos.protective_order_id} for {pos.symbol}")
        _wait_cancel_confirmed(pos.protective_order_id, timeout=2.0)
        pos.protective_order_id = None

    # Place OCO sell for tier fraction
    oco_order = place_oco_sell(pos.symbol, tier_shares, target_price, stop_price, stop_limit_price)
    if not oco_order:
        # OCO failed — fallback to v1.1 mode (trailing stop only)
        log(f"{YELLOW}OCO T{tier_idx+1} FAILED for {pos.symbol} — falling back to v1.1 trailing-only mode{RESET}")
        # Sync Alpaca shares before placing trailing stop
        if not DRY_RUN:
            try:
                ap = trading_client.get_open_position(pos.symbol)
                actual_qty = int(float(ap.qty))
                if actual_qty != pos.remaining_shares:
                    log(f"  OCO fallback sync: {pos.symbol} local={pos.remaining_shares} Alpaca={actual_qty}")
                    pos.remaining_shares = actual_qty
            except Exception:
                pos.remaining_shares = 0
        # Re-place trailing stop for all remaining shares
        if pos.remaining_shares > 0:
            current_trail_pct = pos.trail_pcts[min(tier_idx - 1, len(pos.trail_pcts) - 1)] if tier_idx > 0 else pos.trail_pcts[0]
            replace_with_trailing_stop(pos, current_trail_pct)
        return None

    oco_entry = {
        "order_id": str(oco_order.id),
        "tier_idx": tier_idx,
        "qty": tier_shares,
        "target_price": target_price,
        "stop_price": stop_price,
        "leg_filled": None,  # will be set when fill detected
    }
    pos.oco_order_ids.append(oco_entry)

    # Place new trailing stop for remaining shares (minus OCO qty)
    trail_remaining = pos.remaining_shares - tier_shares
    if trail_remaining > 0:
        current_trail_pct = pos.trail_pcts[min(tier_idx - 1, len(pos.trail_pcts) - 1)] if tier_idx > 0 else pos.trail_pcts[0]
        trail_order = place_trailing_stop_sell(pos.symbol, trail_remaining, current_trail_pct * 100)
        if trail_order:
            pos.protective_order_id = str(trail_order.id)
            log(f"OCO T{tier_idx+1}: trailing stop placed for {trail_remaining}sh at {current_trail_pct:.1%} -> {trail_order.id}")
        else:
            # Trailing stop failed — fallback to stop-limit to protect remaining shares
            log(f"{YELLOW}OCO T{tier_idx+1}: trailing stop FAILED for {pos.symbol} {trail_remaining}sh — trying stop-limit fallback{RESET}")
            stop_result = place_protective_stop(pos)
            if stop_result:
                log(f"OCO T{tier_idx+1}: stop-limit fallback placed for {pos.symbol}")
            else:
                log(f"{RED}OCO T{tier_idx+1}: BOTH trailing and stop-limit FAILED for {pos.symbol} — {trail_remaining}sh UNPROTECTED{RESET}")
    else:
        log(f"OCO T{tier_idx+1}: no remaining shares for trailing stop (position fully allocated)")

    log(f"OCO T{tier_idx+1} PLACED: {pos.symbol} {tier_shares}sh limit=${target_price:.2f} stop=${stop_price:.2f} + trailing {trail_remaining}sh")
    return oco_entry


def cancel_all_oco_for_position(pos):
    """Cancel all pending OCO orders for a position (those where leg_filled is None).
    Remove them from pos.oco_order_ids. Waits for Alpaca confirmation on each cancel."""
    for entry in pos.oco_order_ids[:]:
        if entry.get("leg_filled") is None:
            cancel_order(entry["order_id"])
            _wait_cancel_confirmed(entry["order_id"], timeout=2.0)
            log(f"CANCEL OCO: {entry['order_id']} for {pos.symbol} T{entry['tier_idx']+1}")
            pos.oco_order_ids.remove(entry)


def force_sell_position(symbol: str, qty: int, cancel_existing_orders=True, intent="full_exit") -> int:
    """Force sell shares. Returns number of shares actually sold (0 = failed).
    close_position sells ALL Alpaca shares — returns total_qty even if qty was less.
    Caller must check: if result >= pos.remaining_shares, position is fully closed.
    cancel_existing_orders: when True (default), cancel pending sell orders first.
    intent: "full_exit" = stop loss/trailing/EOD (close_position allowed for entire position)
            "partial"  = ladder tier sell (NEVER use close_position, always market sell exact qty)
    NOTE: intent="partial" prevents the close_position bug where Alpaca sells ALL remaining
    shares instead of just the tier fraction, which caused 5 stocks to be fully liquidated
    on 7/27 (t1_full_exit/t5_full_exit instead of partial tier sells)."""
    if DRY_RUN:
        # Cancel any DRY sell orders for this symbol
        if cancel_existing_orders:
            to_cancel = [oid for oid, m in dry_run_orders.items() if m.symbol == symbol and m.side == "sell"]
            for oid in to_cancel:
                del dry_run_orders[oid]
        order = place_sell_market(symbol, qty)
        if order:
            log(f"[DRY] FORCE SELL {symbol} {qty} -> {order.id}")
            return qty
        return 0
    # Cancel any pending sell orders for this symbol first
    # (they lock shares and will be replaced by this sell)
    if cancel_existing_orders:
        try:
            open_orders = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]))
            for o in open_orders:
                if o.side == OrderSide.SELL:
                    cancel_order(str(o.id))
                    _wait_cancel_confirmed(str(o.id), timeout=2.0)
                    log(f"FORCE SELL: cancelled pending sell order {o.id} for {symbol}")
        except Exception:
            pass

    # Get actual Alpaca position quantity (after cancellations)
    total_qty = 0
    try:
        alpaca_pos = trading_client.get_open_position(symbol)
        total_qty = int(float(alpaca_pos.qty))
    except Exception:
        pass

    # If Alpaca shows no position, stock was already sold (e.g. stop filled)
    # Return 0 — caller knows position is already closed via protective stop
    if total_qty <= 0:
        log(f"FORCE SELL: {symbol} no Alpaca position (already closed via protective stop)")
        return 0

    # close_position sells ALL Alpaca shares — ONLY use for full_exit intent
    # (stop loss, trailing stop, force close). NEVER for partial ladder tier sells.
    # FIX: On 7/27, close_position was triggered for ladder tier sells because
    # qty >= total_qty after protective stop cancellation race condition, selling
    # entire position instead of just 1/8 shares.
    use_close_position = (intent == "full_exit" and qty >= total_qty)
    if use_close_position:
        try:
            result = trading_client.close_position(symbol)
            if result:
                # Verify position actually closed
                time.sleep(0.5)
                try:
                    alpaca_pos = trading_client.get_open_position(symbol)
                    remaining = int(float(alpaca_pos.qty))
                    if remaining > 0:
                        log(f"FORCE SELL: close_position partial fill for {symbol}, {remaining} shares remain")
                        sell_qty = remaining
                    else:
                        log(f"FORCE SELL (close_position): {symbol} {total_qty} shares")
                        return total_qty
                except Exception:
                    # Position not found = fully closed
                    log(f"FORCE SELL (close_position): {symbol} {total_qty} shares")
                    return total_qty
        except Exception as e:
            log(f"close_position failed for {symbol}: {e}")
            sell_qty = total_qty
    else:
        # Partial sell: qty < total_qty. Use market sell for exact qty.
        sell_qty = qty

    # Method 2: Market sell
    try:
        order = place_sell_market(symbol, sell_qty)
        if order:
            filled = _wait_order_filled(order.id, timeout=30)
            if filled:
                log(f"FORCE SELL (market): {symbol} {sell_qty} shares")
                return sell_qty
    except Exception as e:
        rejection = analyze_alpaca_rejection(str(e))
        if rejection["category"] in ("no_position", "market_closed"):
            log(f"FORCE SELL: {symbol} cannot sell — {rejection['detail']}")
            return 0
        log(f"market sell failed for {symbol}: {e}")

    # Method 3: Cancel all sell orders for this symbol, wait, retry market sell
    # FIX 1.1: For partial sells (qty < total_qty), sell only the requested qty,
    # not total_qty. This prevents ladder tier sells from accidentally liquidating
    # the entire position when Method 2 fails due to share locking.
    try:
        # Cancel only sell orders for THIS symbol (not all account orders)
        try:
            open_orders = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]))
            for o in open_orders:
                if o.side == OrderSide.SELL:
                    cancel_order(str(o.id))
                    _wait_cancel_confirmed(str(o.id), timeout=2.0)
                    log(f"FORCE SELL (retry): cancelled sell order {o.id} for {symbol}")
        except Exception:
            cancel_all_orders()
        time.sleep(1)
        # Re-check actual position
        current_qty = 0
        try:
            alpaca_pos = trading_client.get_open_position(symbol)
            current_qty = int(float(alpaca_pos.qty))
        except Exception:
            pass
        if current_qty <= 0:
            log(f"FORCE SELL (retry): {symbol} position already closed")
            return 0
        # FIX: For partial sells, sell only the requested qty; for full sells, sell all
        retry_qty = min(qty, current_qty)
        order = place_sell_market(symbol, retry_qty)
        if order:
            filled = _wait_order_filled(order.id, timeout=30)
            if filled:
                log(f"FORCE SELL (retry): {symbol} {retry_qty} shares")
                return retry_qty
    except Exception as e:
        log(f"All sell methods failed for {symbol}: {e}")

    # Method 4: Limit sell with deep discount (95% of current price) — last resort
    try:
        snap = get_snapshots([symbol]).get(symbol)
        if snap and snap.latest_trade:
            last_price = float(snap.latest_trade.price)
            if last_price > 0 and sell_qty > 0:
                deep_discount = round(last_price * 0.95, 2)
                log(f"FORCE SELL (limit deep discount): {symbol} {sell_qty} @ ${deep_discount:.2f}")
                order = place_sell_limit(symbol, sell_qty, deep_discount)
                if order:
                    filled = _wait_order_filled(order.id, timeout=30)
                    if filled:
                        log(f"FORCE SELL (deep discount limit): {symbol} {sell_qty} shares")
                        return sell_qty
    except Exception as e:
        rejection = analyze_alpaca_rejection(str(e))
        if rejection["category"] in ("no_position", "market_closed"):
            log(f"FORCE SELL: {symbol} cannot sell — {rejection['detail']}")
            return 0
        log(f"Deep discount limit sell also failed for {symbol}: {e}")

    if sell_qty > 0:
        # Final check: verify Alpaca still has position before reporting critical
        if not DRY_RUN:
            try:
                ap = trading_client.get_open_position(symbol)
                if int(float(ap.qty)) <= 0:
                    log(f"FORCE SELL: {symbol} Alpaca position already closed (all methods failed but position gone)")
                    return 0
            except Exception:
                log(f"FORCE SELL: {symbol} no Alpaca position found (all methods failed but position gone)")
                return 0
        log(f"{RED}CRITICAL: ALL 4 force_sell methods failed for {symbol} {sell_qty}sh — manual intervention required!{RESET}")

    return 0


def check_order_filled(order_id) -> bool:
    if DRY_RUN:
        mock = dry_run_orders.get(order_id)
        if not mock:
            return False
        if mock.status == "filled":
            return True
        # Simulate fill based on current price
        price = _dry_run_get_price(mock.symbol)
        if not price:
            return False
        filled = False
        fill_price = 0.0
        if mock.side == "buy" and mock.order_type == "limit":
            if price <= mock.limit_price:
                fill_price = round(price * (1 + SLIPPAGE_ENTRY), 2)
                filled = True
        elif mock.side == "sell" and mock.order_type == "limit":
            if price >= mock.limit_price:
                fill_price = round(price * (1 - SLIPPAGE_TARGET), 2)
                filled = True
        elif mock.side == "sell" and mock.order_type == "stop_limit":
            if price <= mock.stop_price:
                fill_price = round(min(mock.limit_price, price * (1 - SLIPPAGE_STOP)), 2)
                filled = True
        elif mock.side == "sell" and mock.order_type == "trailing_stop":
            # Use day_highs from run_trading_day scope
            dh = _dry_run_day_highs.get(mock.symbol, price)
            if dh > 0 and price <= dh * (1 - mock.trail_percent / 100):
                fill_price = round(price * (1 - SLIPPAGE_TRAILING), 2)
                filled = True
        elif mock.side == "sell" and mock.order_type == "oco":
            # 1.2: OCO order — check limit leg and stop leg
            if price >= mock.limit_price:
                # Limit leg fills (profit)
                fill_price = round(price * (1 - SLIPPAGE_TARGET), 2)
                filled = True
            elif price <= mock.stop_price:
                # Stop leg fills (loss protection)
                fill_price = round(min(mock.oco_stop_limit_price or mock.stop_price, price * (1 - SLIPPAGE_STOP)), 2)
                filled = True
        elif mock.order_type == "market":
            fill_price = round(price * (1 - SLIPPAGE_FORCE), 2)
            filled = True
        if filled:
            mock.status = "filled"
            mock.filled_qty = mock.qty
            mock.filled_price = fill_price
            log(f"[DRY] FILLED {mock.side} {mock.symbol} {mock.qty} @ ${fill_price:.2f}")
        return filled
    # Use batch order cache if available (saves 1 API call per check)
    cached = _order_cache.get(order_id)
    if cached:
        return cached["status"] in ("filled", "partially_filled")
    try:
        order = trading_client.get_order_by_id(order_id)
        return order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)
    except Exception as e:
        log(f"check_order_filled error for {order_id}: {e}")
        return False


def get_order_filled_qty(order_id) -> int:
    """Return the number of shares actually filled for an order."""
    if DRY_RUN:
        mock = dry_run_orders.get(order_id)
        return mock.filled_qty if mock else 0
    cached = _order_cache.get(order_id)
    if cached:
        return cached["filled_qty"]
    try:
        order = trading_client.get_order_by_id(order_id)
        if order.filled_qty:
            return int(float(order.filled_qty))
        return 0
    except Exception:
        return 0


def get_order_filled_price(order_id) -> float:
    """Return the average fill price for an order. Returns 0.0 if not filled or unknown."""
    if DRY_RUN:
        mock = dry_run_orders.get(order_id)
        return mock.filled_price if mock else 0.0
    cached = _order_cache.get(order_id)
    if cached:
        return cached["filled_avg_price"]
    try:
        order = trading_client.get_order_by_id(order_id)
        if order.filled_avg_price:
            return float(order.filled_avg_price)
        return 0.0
    except Exception:
        return 0.0


def check_order_canceled(order_id) -> bool:
    if DRY_RUN:
        mock = dry_run_orders.get(order_id)
        return mock.status == "canceled" if mock else True  # not found = canceled
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
                if sold > 0:
                    log(f"FORCE CLOSE SUCCESS: {pos.symbol} {sold} shares")
                else:
                    log(f"FORCE CLOSE FAILED: {pos.symbol}")
                pos.remaining_shares = 0
    except Exception as e:
        log(f"Force close check error: {e}")


# ── Protective order management ────────────────────────────────────

def place_protective_stop(pos: LivePosition) -> str | None:
    """Place protective stop-limit sell. Returns order_id if successful.
    If stop placement fails, escalates to market sell and marks position for removal.
    Caller must check pos.remaining_shares == 0 after call to handle emergency exit cleanup."""
    limit_price = round(pos.stop_price * (1 - STOP_LIMIT_BUFFER), 2)
    order = place_stop_limit_sell(pos.symbol, pos.remaining_shares, pos.stop_price, limit_price)
    if order:
        pos.protective_order_id = str(order.id)
        return str(order.id)
    # 止损挂不上（stop_distance等原因）→ 市价卖出紧急退出，不允许裸仓
    log(f"{RED}PROTECTIVE STOP FAILED for {pos.symbol} — escalating to market sell (no naked position){RESET}")
    sold = force_sell_position(pos.symbol, pos.remaining_shares)
    if sold >= pos.remaining_shares:
        pos.remaining_shares = 0
        # record_trade and positions.remove handled by caller (emergency_exit path)
    elif sold > 0:
        pos.remaining_shares -= sold
        log(f"Emergency market sell partial: {pos.symbol} {sold} sold, {pos.remaining_shares} remain")
    return None


def _verify_order_active(order_id: str) -> bool:
    """检查订单是否仍然active（未取消、未成交）"""
    if DRY_RUN:
        return order_id in dry_run_orders
    if not order_id:
        return False
    cached = _order_cache.get(order_id)
    if cached:
        return cached["status"] in ("new", "accepted", "pending_new", "pending_replace", "pending_cancel")
    try:
        order = trading_client.get_order_by_id(order_id)
        return order and order.status in ("new", "accepted", "pending_new", "pending_replace", "pending_cancel")
    except Exception:
        return False


def replace_with_trailing_stop(pos: LivePosition, trail_pct: float) -> str | None:
    """替换为trailing stop — 先取消旧单再挂新单（Alpaca锁仓机制要求必须先释放股份）
    Returns order_id if successful, None if failed.
    If pos.remaining_shares == 0 after call, emergency exit occurred — caller must handle cleanup."""
    old_order_id = pos.protective_order_id

    # Sync Alpaca actual shares before placing new order
    if not DRY_RUN:
        try:
            ap = trading_client.get_open_position(pos.symbol)
            actual_qty = int(float(ap.qty))
            if actual_qty != pos.remaining_shares:
                log(f"  trailing sync: {pos.symbol} local={pos.remaining_shares} Alpaca={actual_qty}")
                pos.remaining_shares = actual_qty
        except Exception:
            log(f"  trailing sync: {pos.symbol} Alpaca无仓位，跳过trailing")
            pos.remaining_shares = 0
            return None
    if pos.remaining_shares <= 0:
        return None

    # Alpaca锁仓: 旧卖单锁定股份，新卖单无法覆盖同一批股份
    # 必须先取消旧单释放股份，才能挂新单（有0.3-2秒裸仓窗口，不可避免）
    if old_order_id:
        cancel_order(old_order_id)
        _wait_cancel_confirmed(old_order_id, timeout=2.0)
        pos.protective_order_id = None

    # 挂新trailing stop
    order = place_trailing_stop_sell(pos.symbol, pos.remaining_shares, trail_pct * 100)
    if order:
        pos.protective_order_id = str(order.id)
        return str(order.id)
    # trailing stop失败 → fallback到stop-limit
    log(f"Trailing stop failed for {pos.symbol}, falling back to stop-limit")
    result = place_protective_stop(pos)
    if result:
        return result
    # 两者都失败 → 裸仓！INV-2将在下轮检查中捕获
    log(f"{RED}Both trailing and stop-limit failed for {pos.symbol} — NAKED POSITION!{RESET}")
    pos.protective_order_id = None
    return None


def replace_stop_for_remaining(pos: LivePosition) -> str | None:
    """替换止损 — 先挂新单再取消旧单，避免裸仓空窗期
    Returns order_id if successful, None if failed.
    If pos.remaining_shares == 0 after call, emergency exit occurred — caller must handle cleanup."""
    old_order_id = pos.protective_order_id

    if pos.remaining_shares <= 0:
        return None

    # Ladder: use next_tier_idx to determine trailing pct
    if pos.trade_type in ("first", "recovered") and pos.targets:
        if pos.next_tier_idx > 0:
            filled_tier = pos.next_tier_idx - 1
            trail_pct = pos.trail_pcts[min(filled_tier, len(pos.trail_pcts) - 1)]
            result = replace_with_trailing_stop(pos, trail_pct)
            # replace_with_trailing_stop已内部处理先新再旧
            return result
        else:
            # 先取消旧单再挂新stop-limit（Alpaca锁仓机制要求）
            if old_order_id:
                cancel_order(old_order_id)
                _wait_cancel_confirmed(old_order_id, timeout=2.0)
                pos.protective_order_id = None
            result = place_protective_stop(pos)
            if result:
                return result
            # 新单失败 → 裸仓！INV-2将在下轮检查中捕获
            log(f"{RED}Protective stop replacement failed for {pos.symbol} — NAKED POSITION!{RESET}")
            pos.protective_order_id = None
            return None

    # 默认: 先取消旧单再挂新stop-limit（Alpaca锁仓机制要求）
    if old_order_id:
        cancel_order(old_order_id)
        _wait_cancel_confirmed(old_order_id, timeout=2.0)
        pos.protective_order_id = None
    result = place_protective_stop(pos)
    if result:
        return result
    # 新单失败 → 裸仓！INV-2将在下轮检查中捕获
    log(f"{RED}Protective stop replacement failed for {pos.symbol} — NAKED POSITION!{RESET}")
    pos.protective_order_id = None
    return None


# ── Entry detection ────────────────────────────────────────────────
def _is_hammer_bar(bar, open_price):
    """判断一根bar是否是锤子线（探底+反弹合一）。"""
    b_open = bar.get("open", bar.get("open_price", 0.0))
    b_high = bar["high"]
    b_low = bar["low"]
    b_close = bar["close"]
    bar_range = b_high - b_low
    if bar_range < 0.001:
        bar_range = 0.001
    return (b_low < open_price and                       # 探底
            b_close > b_open and                         # 阳线
            (b_close - b_low) / bar_range >= 0.6 and     # 收盘在上半部
            (b_close - b_open) / b_open >= 0.005 and      # 实体至少0.5%
            (b_close - b_low) / b_low >= 0.01)            # 从低到收盘至少1%反弹


def check_entry_1min(symbol, open_price, accumulator):
    """1分钟K线折返点检测 — 锤子线(1min)/1-2根确认bar(2-3min)，返回底部low。"""
    bars = accumulator.get_1min_bars(symbol)
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

    # 第一层：底部bar本身就是锤子线 → 1分钟确认
    if _is_hammer_bar(bars[pullback_idx], open_price):
        return pullback_price, True

    # 第二层/第三层：等1-2根确认bar
    confirm_count = 0
    bullish_count = 0
    last_confirm_close = 0.0
    for i in range(pullback_idx + 1, len(bars)):
        bar = bars[i]
        bar_low = bar["low"]
        bar_close = bar["close"]
        bar_open = bar.get("open", bar.get("open_price", 0.0))

        # 更深底部 → 重置，检查新底部是否是锤子线
        if bar_low < open_price and bar_low < pullback_price:
            pullback_idx = i
            pullback_price = bar_low
            confirm_count = 0
            bullish_count = 0
            last_confirm_close = 0.0
            if _is_hammer_bar(bar, open_price):
                return pullback_price, True
            continue

        # 还在底部区域 → 跳过
        if bar_low <= pullback_price or bar_close <= pullback_price:
            continue

        # 有效确认bar
        confirm_count += 1
        last_confirm_close = bar_close
        if bar_close > bar_open:
            bullish_count += 1

        # 第二层：1根确认bar（需阳线+实体>=0.3%）
        if confirm_count == 1 and bullish_count == 1:
            if (bar_close - bar_open) / bar_open >= 0.003:
                return pullback_price, True

        # 第三层：2根确认bar（至少1根阳线）
        if confirm_count >= 2 and bullish_count >= 1:
            return pullback_price, True

    return 0, False


def check_reentry_1min(symbol, open_price, accumulator, min_pullback=0.04):
    """1分钟K线re-entry检测 — 锤子线/1-2根确认bar，1-2分钟内确认。
    Uses last 60 bars (1 hour window) for peak detection."""
    bars = accumulator.get_1min_bars(symbol)
    if len(bars) < 5:
        return 0, 0, False
    recent = bars[-60:] if len(bars) >= 60 else bars
    peak = max(b["high"] for b in recent)
    if peak < open_price * 1.03:
        return 0, 0, False
    peak_idx = max(range(len(recent)), key=lambda i: recent[i]["high"])
    pb_threshold = peak * (1 - min_pullback)

    # 找底部bar
    pullback_idx = -1
    pullback_price = 0.0
    for i in range(peak_idx + 1, len(recent)):
        if recent[i]["low"] < pb_threshold:
            pullback_idx = i
            pullback_price = recent[i]["low"]
            break
    if pullback_idx < 0:
        return 0, 0, False

    # 第一层：底部bar是锤子线 → 1分钟确认
    bottom = recent[pullback_idx]
    b_open = bottom.get("open", bottom.get("open_price", 0.0))
    b_high = bottom["high"]
    b_low = bottom["low"]
    b_close = bottom["close"]
    bar_range = b_high - b_low
    if bar_range < 0.001:
        bar_range = 0.001
    if (b_low < pb_threshold and
        b_close > b_open and
        (b_close - b_low) / bar_range >= 0.6 and
        (b_close - b_open) / b_open >= 0.005 and
        (b_close - b_low) / b_low >= 0.01):
        return pullback_price, peak, True

    # 第二层/第三层：等1-2根确认bar
    confirm_count = 0
    bullish_count = 0
    last_confirm_close = 0.0
    for i in range(pullback_idx + 1, len(recent)):
        bar = recent[i]
        bar_low = bar["low"]
        bar_close = bar["close"]
        bar_open = bar.get("open", bar.get("open_price", 0.0))

        # 更深底部 → 重置
        if bar_low < pb_threshold and bar_low < pullback_price:
            pullback_idx = i
            pullback_price = bar_low
            confirm_count = 0
            bullish_count = 0
            last_confirm_close = 0.0
            # 重新检查新底部是否是锤子线
            b_high = bar["high"]
            b_open = bar_open
            b_low = bar_low
            b_close = bar_close
            bar_range = b_high - b_low
            if bar_range < 0.001:
                bar_range = 0.001
            if (b_low < pb_threshold and
                b_close > b_open and
                (b_close - b_low) / bar_range >= 0.6 and
                (b_close - b_open) / b_open >= 0.005 and
                (b_close - b_low) / b_low >= 0.01):
                return pullback_price, peak, True
            continue

        if bar_low <= pullback_price or bar_close <= pullback_price:
            continue

        confirm_count += 1
        last_confirm_close = bar_close
        if bar_close > bar_open:
            bullish_count += 1

        # 第二层：1根确认bar
        if confirm_count == 1 and bullish_count == 1:
            if (bar_close - bar_open) / bar_open >= 0.003:
                return pullback_price, peak, True

        # 第三层：2根确认bar
        if confirm_count >= 2 and bullish_count >= 1:
            return pullback_price, peak, True

    return 0, 0, False


def check_entry(symbol, open_price, accumulator):
    """DEPRECATED: 旧版5分钟K线入场检测，保留兼容。"""
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
    if DRY_RUN:
        eq = getattr(config, "INITIAL_CAPITAL", 500)
        log(f"[DRY] Account equity: ${eq:.2f} (simulated)")
        return eq
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
    log("Stone 1.1 Live Paper Trading -- Auto Scheduler")
    equity = _get_account_equity()
    log(f"Capital: ${equity:,.2f} | Max daily trades: {config.MAX_DAILY_TRADES if config.MAX_DAILY_TRADES > 0 else 'unlimited'}")
    log(f"Entry buffer: +{ENTRY_LIMIT_BUFFER:.1%} | Stop-limit buffer: -{STOP_LIMIT_BUFFER:.1%}")
    log(f"Target buffer: -{TARGET_LIMIT_BUFFER:.1%} | Force-close timeout: {FORCE_CLOSE_LIMIT_TIMEOUT}s")
    log(f"Re-entry cutoff: {REENTRY_CUTOFF} EST | Leveraged ETF filter: ON")
    log(f"Scan: 9:20 preliminary, 9:31 official (aligned with backtest)")
    log(f"8-tier targets with list-based fields | calc_targets() | get_trailing_pct()")
    log(f"WebSocket: {'ON' if getattr(config, 'USE_WEBSOCKET', False) else 'OFF'} | "
        f"Data feed: {'SIP' if DATA_FEED == DataFeed.SIP else 'IEX'}")
    log(f"1.1: All 1.0 features + P0-P2 fixes (circuit breaker, skip-gap, protective stop gap, thread safety, WebSocket reconnect)")
    if DRY_RUN:
        log("*** DRY_RUN MODE - No real orders will be placed ***")
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
            next_day = get_next_trading_day(trading_client, today)
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

        # Get start equity (with fallback to INITIAL_CAPITAL)
        try:
            equity_start = float(trading_client.get_account().equity)
        except Exception:
            equity_start = config.INITIAL_CAPITAL

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
            version="1.1",
            equity_start=equity_start,
            equity_end=equity_end,
            daily_trades=result["daily_trades"],
            trades_detail=result["trades_detail"],
            candidates=result["candidates"],
            events_log=result["events_log"],
        )

        # Wait for next trading day (wake at 9:20 EST for pre-market scan)
        next_day = get_next_trading_day(trading_client, today)
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
    _pdt_detected = False  # Pattern Day Trader 规则触发标志
    candidates = []
    entry_checked = set()
    entered_symbols = set()  # Symbols that were actually entered (for re-entry eligibility)
    stop_loss_symbols = set()  # Symbols that exited via stop loss (excluded from re-entry)
    reentry_checked = set()
    _last_rescan_time = ""  # v1.3: track last mid-day rescan timestamp
    accumulator = BarAccumulator()
    day_highs = {}
    if DRY_RUN:
        global _dry_run_day_highs
        _dry_run_day_highs = day_highs
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

    def _check_circuit_breaker(snaps) -> bool:
        """Check daily loss circuit breaker. Returns True if triggered."""
        max_daily_loss_pct = getattr(config, "MAX_DAILY_LOSS_PCT", 0)
        if max_daily_loss_pct <= 0:
            return False
        realized_pnl = sum(t["pnl"] for t in trades_detail)
        unrealized_pnl = 0
        for pos2 in positions:
            if pos2.remaining_shares > 0:
                snap2 = snaps.get(pos2.symbol)
                if snap2 and snap2.latest_trade:
                    cur2 = float(snap2.latest_trade.price)
                    unrealized_pnl += (cur2 - pos2.entry_price) * pos2.remaining_shares
        total_pnl = realized_pnl + unrealized_pnl
        if total_pnl <= -(capital * max_daily_loss_pct):
            log(f"{RED}CIRCUIT BREAKER: total PnL ${total_pnl:.2f} (realized ${realized_pnl:.2f} + unrealized ${unrealized_pnl:.2f}) exceeds -{max_daily_loss_pct*100:.0f}% of ${capital:.2f}{RESET}")
            events_log.append(f"{now_est.strftime('%H:%M:%S')} CIRCUIT BREAKER total PnL ${total_pnl:.2f}")
            entry_checked.update(set(c["symbol"] for c in candidates))
            return True
        return False

    # ── Pre-market scan (9:20 preview, NOT used for trading) ──
    log("Pre-market scanning for gap stocks (preliminary)...")
    preliminary = scan_gaps()
    if preliminary:
        log(f"Pre-market preliminary: {[c['symbol'] for c in preliminary[:5]]}... ({len(preliminary)} total)")
    else:
        log("Pre-market: no gap stocks found yet (pre-market prices may be incomplete)")

    # ── Wait for 9:31 to re-scan with official open prices (aligns with backtest) ──
    log("Waiting for 9:31 to re-scan with regular session open prices...")
    while True:
        now = dt.datetime.now(tz=ZoneInfo("America/New_York"))
        if now.time() >= force_close_time:
            log("Market already closed, skipping trading day.")
            return {"daily_trades": 0, "trades_detail": [], "candidates": [], "events_log": events_log}
        if now.time() >= dt.time(9, 31, 30):
            break
        time.sleep(5)

    # ── Official scan with regular session open prices ──
    log("Re-scanning with regular session open prices (aligned with backtest)...")
    candidates = scan_gaps()
    if not candidates:
        log("No gap stocks found with regular session prices.")
        return {"daily_trades": 0, "trades_detail": [], "candidates": [], "events_log": events_log}
    log(f"Official scan found {len(candidates)} gap stocks")

    n_candidates = len(candidates)
    max_monitored = min(getattr(config, "MAX_CANDIDATES", 10), n_candidates)
    candidates = candidates[:max_monitored]

    log(f"Candidates: {[c['symbol'] for c in candidates]}")
    for c in candidates:
        log(f"  {c['symbol']}: gap +{c['gap_pct']:.1%}, open=${c['open_price']:.4f}")
        day_highs[c['symbol']] = c['open_price']

    # ── Backfill historical 1-min bars for candidates ──
    # 1-min bars auto-aggregate into 5-min cache via BarAccumulator
    now_est = dt.datetime.now(tz=ZoneInfo("America/New_York"))
    if candidates:
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
                log(f"Backfilled 1min bars: {dict((c['symbol'], len(accumulator.get_1min_bars(c['symbol']))) for c in candidates)}")
                log(f"Backfilled 5min cache: {dict((c['symbol'], accumulator.bar_count(c['symbol'])) for c in candidates)}")
        except Exception as e:
            log(f"Backfill error: {e}")

    # ── Recover existing Alpaca positions ──
    # Load saved state first — use original stop_price from saved positions
    saved_positions = load_saved_positions() if not DRY_RUN else {}
    if DRY_RUN:
        log("[DRY] Skip position recovery (no real positions in DRY_RUN)")
    else:
      try:
        alpaca_positions = trading_client.get_all_positions()
        alpaca_orders = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
        log(f"RECOVERY: Alpaca has {len(alpaca_positions)} positions, {len(alpaca_orders)} open orders, locally tracking {len(positions)}")
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

            # Find existing protective (SELL) order — only accept OPEN orders
            prot_order_id = None
            # P1 fix: Prefer saved stop_price over recalculated default
            saved_pd = saved_positions.get(sym)
            stop_price = saved_pd["stop_price"] if saved_pd and saved_pd.get("stop_price") else avg_entry * 0.95
            oco_order_ids_recover = []  # 1.2: Recovered OCO orders
            for ao in alpaca_orders:
                if ao.symbol == sym and ao.side == OrderSide.SELL and ao.status == OrderStatus.OPEN:
                    if getattr(ao, 'order_class', None) and ao.order_class.value == 'oco':
                        # 1.2: Found an OCO order — match to tier by target_price
                        tp_limit = None
                        sl_stop = None
                        if ao.limit_price:
                            tp_limit = float(ao.limit_price)
                        if ao.stop_price:
                            sl_stop = float(ao.stop_price)
                        # Match limit_price to a target tier
                        matched_ti = None
                        for ti in range(len(targets)):
                            if abs(tp_limit - targets[ti]) < 0.05:
                                matched_ti = ti
                                break
                        oco_qty = int(float(ao.qty)) if ao.qty else 0
                        if matched_ti is not None:
                            oco_entry = {
                                "order_id": str(ao.id),
                                "tier_idx": matched_ti,
                                "qty": oco_qty,
                                "target_price": tp_limit or 0,
                                "stop_price": sl_stop or 0,
                                "leg_filled": None,
                            }
                            oco_order_ids_recover.append(oco_entry)
                            log(f"RECOVER: Found OCO order {ao.id} T{matched_ti+1} limit=${tp_limit:.2f} stop=${sl_stop:.2f} qty={oco_qty}")
                        else:
                            log(f"RECOVER: OCO order {ao.id} limit=${tp_limit:.2f} doesn't match any target — skipping")
                    else:
                        prot_order_id = str(ao.id)
                        if ao.stop_price:
                            stop_price = float(ao.stop_price)
                        log(f"RECOVER: Found protective order {ao.id} stop=${stop_price:.4f}")

            # 1.0: Calculate 8-tier targets for recovered position
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

            # Compute next_tier_idx from reached_list
            next_tier_idx = 0
            for ti in range(len(reached)):
                if not reached[ti]:
                    next_tier_idx = ti
                    break
            else:
                next_tier_idx = len(targets)

            highest_seen = max(cur_price, avg_entry)
            day_highs[sym] = max(day_highs.get(sym, 0), highest_seen)

            # P1/P2 fix: Restore saved fields when available (trade_type, reentry_target, prev_high, atr)
            trade_type = "recovered"
            reentry_target = None
            prev_high_restore = avg_entry
            atr_restore = 0.0
            if saved_pd:
                trade_type = saved_pd.get("trade_type", "recovered")
                reentry_target = saved_pd.get("reentry_target")
                prev_high_restore = saved_pd.get("prev_high", avg_entry)
                atr_restore = saved_pd.get("atr", 0.0)
                if trade_type == "reentry":
                    log(f"RECOVER: {sym} was re-entry position (target=${reentry_target}, prev_high=${prev_high_restore})")

            pos = LivePosition(
                symbol=sym, entry_price=avg_entry, shares=qty,
                stop_price=stop_price,
                open_price=open_price,
                trade_type=trade_type,
                highest=highest_seen, prev_high=prev_high_restore,
                entry_time=now_est, protective_order_id=prot_order_id,
                atr=atr_restore,
                targets=targets, sell_ratios=sell_ratios,
                trail_pcts=trail_pcts,
                reached_list=reached,
                sold_shares_list=sold_shares_list,
                target_mode=target_mode,
                next_tier_idx=next_tier_idx,
                oco_order_ids=oco_order_ids_recover,
                tier_fill_prices=[avg_entry] * next_tier_idx if next_tier_idx > 0 else [],
                reentry_target=reentry_target,
            )
            positions.append(pos)
            # No pending ladder sells to recover — market sells fill immediately
            # Place protective stop if none exists (or if ladder system needs one)
            if not prot_order_id:
                replace_stop_for_remaining(pos)
                log(f"RECOVER: Placed protective stop for {sym}")
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

    def record_trade(pos, exit_price, exit_reason, sold_shares=None):
        nonlocal daily_trades
        daily_trades += 1
        if sold_shares is None:
            sold_shares = pos.remaining_shares if pos.remaining_shares > 0 else pos.shares
        pnl = (exit_price - pos.entry_price) * sold_shares
        trades_detail.append({
            "symbol": pos.symbol,
            "type": pos.trade_type,
            "entry": round(pos.entry_price, 4),
            "exit": round(exit_price, 4),
            "shares": sold_shares,
            "exit_reason": exit_reason,
            "pnl": round(pnl, 2),
        })
        add_chart_event(pos.symbol, "sell", exit_price,
                        f"{exit_reason.replace('_', ' ').upper()} {sold_shares}sh")

    def _check_emergency_exit(pos):
        """If pos.remaining_shares == 0 (emergency market sell from protective stop failure),
        record the trade and remove from positions list."""
        if pos.remaining_shares <= 0 and pos in positions:
            record_trade(pos, pos.stop_price, "emergency_exit")
            positions.remove(pos)
            return True
        return False

    # ── 不变量检查器 — 实盘状态一致性验证 ──────────────────────
    def check_invariants():
        """运行6条不变量检查 — 只检测+报警+记录修复指令，不直接修改状态。
        修复指令存入 _invariant_fixes，下一轮循环开头才执行（在所有交易逻辑之前），
        确保检测和修复不影响当前轮的交易操作。
        """
        errors = []
        fixes = []  # 修复指令列表，下一轮执行
        critical = False

        # INV-1: 本地remaining_shares = Alpaca实际持仓股数
        for pos in positions:
            if pos.remaining_shares <= 0:
                continue
            try:
                alpaca_pos = trading_client.get_open_position(pos.symbol)
                alpaca_qty = int(float(alpaca_pos.qty))
                # Tolerate <=1 share difference (partial fill rounding)
                if abs(pos.remaining_shares - alpaca_qty) <= 1:
                    if pos.remaining_shares != alpaca_qty:
                        pos.remaining_shares = alpaca_qty  # sync silently
                    continue
                if pos.remaining_shares != alpaca_qty:
                    errors.append(f"INV-1 仓位不一致: {pos.symbol} 本地={pos.remaining_shares} Alpaca={alpaca_qty}")
                    fixes.append(("sync_remaining", pos.symbol, alpaca_qty))
                    critical = True
            except Exception:
                errors.append(f"INV-1 幽灵仓位: {pos.symbol} 本地={pos.remaining_shares} Alpaca无仓位")
                fixes.append(("clear_ghost", pos.symbol))
                critical = True

        # INV-2: 每个有仓位的股票必须有≥1个卖单保护
        for pos in positions:
            if pos.remaining_shares <= 0:
                continue
            has_protection = pos.protective_order_id is not None
            if not has_protection and not DRY_RUN:
                try:
                    open_orders = [o for o in trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN)) if o.symbol == pos.symbol and o.side.value == "sell" and o.status.value in ("open", "new", "accepted", "pending_new")]
                    if not open_orders:
                        errors.append(f"INV-2 裸仓: {pos.symbol} 有{pos.remaining_shares}股但无卖单保护!")
                        fixes.append(("add_stop", pos.symbol))
                        critical = True
                except Exception as e:
                    errors.append(f"INV-2 裸仓(查询失败): {pos.symbol}, err={e}")
                    critical = True

        # INV-3: 已卖股数≤总股数
        for pos in positions:
            sold = pos.shares - pos.remaining_shares
            if sold > pos.shares or pos.remaining_shares < 0:
                errors.append(f"INV-3 超卖: {pos.symbol} 已卖{sold}股/总{pos.shares}股, remaining={pos.remaining_shares}")
                fixes.append(("reset_remaining", pos.symbol))

        # INV-4: PnL一致性
        realized = sum(t["pnl"] for t in trades_detail)
        unrealized = 0
        for pos in positions:
            if pos.remaining_shares > 0:
                try:
                    alpaca_pos = trading_client.get_open_position(pos.symbol)
                    unrealized += float(alpaca_pos.unrealized_pl)
                except Exception:
                    pass
        total_pnl = realized + unrealized
        try:
            current_equity = float(trading_client.get_account().equity)
            equity_change = current_equity - equity_start
            if abs(total_pnl - equity_change) > 5:
                errors.append(f"INV-4 PnL偏差: 系统={total_pnl:.2f}, 账户变化={equity_change:.2f}, 差=${abs(total_pnl-equity_change):.2f}")
        except Exception:
            pass

        # INV-5: tier进度一致性
        for pos in positions:
            if pos.sold_shares_list is None:
                continue
            expected_sold = 0
            for i in range(pos.next_tier_idx):
                if i < len(pos.sold_shares_list):
                    expected_sold += pos.sold_shares_list[i]
            actual_sold = pos.shares - pos.remaining_shares
            if expected_sold != actual_sold and pos.remaining_shares > 0:
                errors.append(f"INV-5 tier不一致: {pos.symbol} 预期已卖={expected_sold}, 实际={actual_sold}, next_tier={pos.next_tier_idx}")
                fixes.append(("sync_tier_progress", pos.symbol))

        # INV-6: pending_buys和positions不重叠
        pos_symbols = {p.symbol for p in positions if p.remaining_shares > 0}
        buy_symbols = set(pending_buys.keys())
        overlap = pos_symbols & buy_symbols
        if overlap:
            errors.append(f"INV-6 重复入场: {overlap} 同时在pending_buys和positions中")

        # INV-7: OCO锁股 + trailing锁股 = remaining_shares (1.2新增)
        if OCO_ENABLED:
            for pos in positions:
                if pos.remaining_shares <= 0 or pos.trade_type in ("reentry", "recovered"):
                    continue
                oco_locked = sum(e["qty"] for e in pos.oco_order_ids if e.get("leg_filled") is None)
                # trailing/protective stop covers (remaining - oco_locked) shares
                # but we can't easily verify the exact qty from Alpaca order — just check
                # that oco_locked <= remaining_shares
                if oco_locked > pos.remaining_shares:
                    errors.append(f"INV-7 OCO超锁: {pos.symbol} OCO锁{oco_locked}股 > remaining={pos.remaining_shares}股")
                    fixes.append(("cancel_oco_overlock", pos.symbol))
                    critical = True

        return errors, fixes, critical

    def apply_invariant_fixes(fixes):
        """执行上一轮检测到的修复指令 — 在所有交易逻辑之前执行，确保不干扰交易"""
        for fix in fixes:
            action = fix[0]
            symbol = fix[1]
            pos = next((p for p in positions if p.symbol == symbol), None)
            if pos is None:
                continue  # 仓位可能已在上一轮退出

            if action == "sync_remaining":
                new_qty = fix[2]
                log(f"  修复INV-1: {symbol} remaining {pos.remaining_shares}→{new_qty}")
                old_remaining = pos.remaining_shares
                pos.remaining_shares = new_qty
                # 同步sold_shares_list和next_tier_idx以保持tier一致性
                if pos.sold_shares_list and pos.trade_type != "reentry":
                    actual_sold = pos.shares - new_qty
                    # 从头累计sold_shares_list直到达到actual_sold
                    cumulative = 0
                    new_tier_idx = 0
                    for i, s in enumerate(pos.sold_shares_list):
                        cumulative += s
                        if cumulative <= actual_sold:
                            new_tier_idx = i + 1
                        else:
                            # 部分成交的tier: 按实际sold调整
                            pos.sold_shares_list[i] = actual_sold - (cumulative - s)
                            new_tier_idx = i + 1
                            break
                    pos.next_tier_idx = new_tier_idx
                    # 清零之后未成交的tier
                    for i in range(new_tier_idx, len(pos.sold_shares_list)):
                        pos.sold_shares_list[i] = 0
                    log(f"  同步tier: next_tier_idx→{new_tier_idx}, sold_shares_list={pos.sold_shares_list}")
                # Rebuild protective stop to match new qty
                if pos.remaining_shares > 0:
                    if pos.protective_order_id:
                        cancel_order(pos.protective_order_id)
                        _wait_cancel_confirmed(pos.protective_order_id, timeout=2.0)
                        pos.protective_order_id = None
                    replace_stop_for_remaining(pos)
                    log(f"  重建protective stop for {symbol}")

            elif action == "clear_ghost":
                log(f"  修复INV-1: 清理幽灵仓位 {symbol}, remaining→0")
                # Cancel all associated orders before clearing
                cancel_all_oco_for_position(pos)
                if pos.protective_order_id:
                    cancel_order(pos.protective_order_id)
                    _wait_cancel_confirmed(pos.protective_order_id, timeout=2.0)
                    pos.protective_order_id = None
                pos.remaining_shares = 0

            elif action == "add_stop":
                log(f"  修复INV-2: 补挂止损保护 {symbol}")
                # 重试3次（级联失败防护）
                result = None
                for _inv_retry in range(3):
                    result = place_protective_stop(pos)
                    if result:
                        log(f"  补挂止损成功: {symbol} (attempt {_inv_retry+1})")
                        break
                    log(f"  补挂止损失败 ({_inv_retry+1}/3): {symbol}")
                    time.sleep(3)
                if not result:
                    # 补挂止损失败 → 升级到强制卖出（最后一道防线）
                    log(f"{RED}  INV-2级联失败: {symbol} 补挂止损3次全败 — 升级到force_sell_position{RESET}")
                    sold = force_sell_position(pos.symbol, pos.remaining_shares)
                    if sold >= pos.remaining_shares:
                        log(f"  INV-2 force_sell成功: {symbol} 完全清仓")
                        pos.remaining_shares = 0
                        if pos in positions:
                            positions.remove(pos)
                    elif sold > 0:
                        pos.remaining_shares -= sold
                        log(f"  INV-2 force_sell部分成功: {symbol} {sold}sh卖出, {pos.remaining_shares}sh剩余 — 需人工干预!")
                    else:
                        log(f"{RED}  INV-2终极失败: {symbol} force_sell也失败 — 需人工干预！{RESET}")

            elif action == "reset_remaining":
                correct_remaining = max(0, pos.shares - min(pos.shares - pos.remaining_shares, pos.shares))
                log(f"  修复INV-3: {symbol} remaining {pos.remaining_shares}→{correct_remaining}")
                pos.remaining_shares = correct_remaining

            elif action == "cancel_oco_overlock":
                log(f"  修复INV-7: {symbol} OCO超锁 — 取消所有OCO订单")
                cancel_all_oco_for_position(pos)
                # After canceling OCOs, remaining shares are unprotected — add trailing
                if pos.remaining_shares > 0:
                    replace_stop_for_remaining(pos)

            elif action == "sync_tier_progress":
                # INV-5: sold_shares_list与remaining_shares不一致
                # 重新计算sold_shares_list和next_tier_idx以匹配实际remaining
                actual_sold = pos.shares - pos.remaining_shares
                log(f"  修复INV-5: {symbol} 同步tier进度, actual_sold={actual_sold}")
                if pos.sold_shares_list and pos.trade_type != "reentry":
                    cumulative = 0
                    new_tier_idx = 0
                    for i, s in enumerate(pos.sold_shares_list):
                        cumulative += s
                        if cumulative <= actual_sold:
                            new_tier_idx = i + 1
                        else:
                            pos.sold_shares_list[i] = actual_sold - (cumulative - s)
                            new_tier_idx = i + 1
                            break
                    pos.next_tier_idx = new_tier_idx
                    for i in range(new_tier_idx, len(pos.sold_shares_list)):
                        pos.sold_shares_list[i] = 0
                    log(f"  同步tier: next_tier_idx→{new_tier_idx}, sold_shares_list={pos.sold_shares_list}")

    _invariant_fixes = []  # 修复指令队列（跨轮传递）

    _entry_window_closed = False
    _invariant_violation = False  # 暂停新入场标志

    while True:
        now_est = dt.datetime.now(tz=ZoneInfo("America/New_York"))
        now_time = now_est.time()
        poll_count += 1

        # ── Batch order status refresh (1 API call replaces N get_order_by_id calls) ──
        # Fetch all open+recently-filled orders once per cycle, cache for lookup.
        global _order_cache
        _order_cache = {}
        if not DRY_RUN:
            try:
                all_orders = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500))
                for o in all_orders:
                    oid = str(o.id)
                    _order_cache[oid] = {
                        "status": o.status.value if hasattr(o.status, 'value') else str(o.status),
                        "filled_qty": int(float(o.filled_qty)) if o.filled_qty else 0,
                        "filled_avg_price": float(o.filled_avg_price) if o.filled_avg_price else 0.0,
                        "side": o.side.value if hasattr(o.side, 'value') else str(o.side),
                        "symbol": o.symbol,
                        "qty": float(o.qty) if o.qty else 0,
                        "order_type": o.order_type.value if hasattr(o.order_type, 'value') else str(o.order_type),
                        "order_class": o.order_class.value if hasattr(o.order_class, 'value') else str(o.order_class) if o.order_class else None,
                        "legs": [
                            {
                                "status": leg.status.value if hasattr(leg.status, 'value') else str(leg.status),
                                "filled_avg_price": float(leg.filled_avg_price) if leg.filled_avg_price else 0.0,
                                "order_type": leg.order_type.value if hasattr(leg.order_type, 'value') else str(leg.order_type),
                                "qty": float(leg.qty) if leg.qty else 0,
                                "filled_qty": int(float(leg.filled_qty)) if leg.filled_qty else 0,
                            }
                            for leg in (o.legs or [])
                        ] if o.legs else [],
                    }
            except Exception as e:
                log(f"Order cache refresh error: {e}")

        # ── 执行上一轮记录的修复指令（在所有交易逻辑之前）──
        if _invariant_fixes:
            apply_invariant_fixes(_invariant_fixes)
            _invariant_fixes = []  # 清空修复队列

        # ── Check pending buy fills ──
        for symbol in list(pending_buys.keys()):
            order_id, pos_data = pending_buys[symbol]
            if check_order_filled(order_id):
                filled_qty = get_order_filled_qty(order_id)
                # For market orders: update entry_price to actual fill price
                try:
                    order_obj = trading_client.get_order_by_id(order_id)
                    if order_obj and float(order_obj.filled_avg_price) > 0:
                        actual_fill_price = float(order_obj.filled_avg_price)
                        log(f"BUY MARKET actual fill price: {symbol} @ ${actual_fill_price:.4f}")
                        pos_data["entry_price"] = actual_fill_price
                        # Recalc stop price based on actual fill
                        atr_val = pos_data.get("atr", 0)
                        pos_data["stop_price"] = calc_stop_price(actual_fill_price, atr_val)
                        # Recalc targets based on actual fill and open_price
                        targets_new, ratios_new, trails_new, mode_new = calc_targets(actual_fill_price, pos_data["open_price"])
                        pos_data["targets"] = targets_new
                        pos_data["sell_ratios"] = ratios_new
                        pos_data["trail_pcts"] = trails_new
                        pos_data["target_mode"] = mode_new
                        log(f"  Recalc targets for {symbol}: entry=${actual_fill_price:.4f} open=${pos_data['open_price']:.4f} "
                            f"mode={mode_new} targets={[round(t,2) for t in targets_new]}")
                        pos_data["reached_list"] = [False] * len(targets_new)
                        pos_data["sold_shares_list"] = [0] * len(targets_new)
                except Exception as e:
                    log(f"Could not get actual fill price for {symbol}: {e}")
                # If partial fill, adjust shares to actual filled amount
                if filled_qty > 0 and filled_qty != pos_data.get("shares", filled_qty):
                    log(f"BUY PARTIAL FILL: {symbol} {filled_qty}/{pos_data.get('shares', '?')} shares")
                    pos_data["shares"] = filled_qty
                log(f"BUY FILLED: {symbol} order {order_id} ({filled_qty}sh) @ ${pos_data['entry_price']:.4f}")
                pos = LivePosition(**pos_data)
                positions.append(pos)
                entered_symbols.add(symbol)  # v1.3: add after fill confirmed (not before)
                # ── Approach 3: Protective stop covers ALL shares, T1 activated on price reach ──
                # Stop covers 100% of position from the start. T1 limit sell is only placed
                # when main loop detects price >= T1 target, ensuring no orphan fraction.
                # ── Approach 3: Protective stop covers ALL shares, T1 activated on price reach ──
                # Stop covers 100% of position from the start. T1 limit sell is only placed
                # when main loop detects price >= T1 target, ensuring no orphan fraction.
                # 重试3次: stop_distance拒绝时增大缓冲，确保不裸仓
                result = None
                for _ps_attempt in range(3):
                    result = place_protective_stop(pos)
                    if result:
                        log(f"PROTECTIVE STOP placed for all {pos.shares} shares: {pos.symbol} stop=${pos.stop_price:.4f}")
                        break
                    log(f"PROTECTIVE STOP retry ({_ps_attempt+1}/3) for {pos.symbol}")
                    time.sleep(2)
                if not result:
                    # Emergency market sell may have already closed the position
                    if pos.remaining_shares <= 0:
                        record_trade(pos, pos.stop_price, "emergency_exit")
                        log(f"EMERGENCY EXIT: {pos.symbol} — protective stop failed, market sold all shares")
                    else:
                        log(f"{RED}WARNING: Protective stop FAILED 3/3 for {pos.symbol} — INV-2 will catch next loop{RESET}")
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

        # ── Check pending sell fills (re-entry tier-1 only) ──
        # Ladder sells (T1-T6) use market orders — fill immediately, no pending state.
        # Only re-entry tier-1 uses limit sell orders tracked in pending_sells.
        # P0-6/P0-7: For reentry_tier1, remaining_shares is NOT decremented until confirmed fill.
        for order_id in list(pending_sells.keys()):
            info = pending_sells[order_id]
            symbol = info["symbol"]
            sell_shares = info["shares"]
            tier_idx = info.get("tier_idx")
            sell_type = info.get("type")
            if check_order_filled(order_id):
                actual_filled = get_order_filled_qty(order_id)
                # P0-6/P0-7: For reentry_tier1, decrement remaining_shares on confirmed fill
                if sell_type == "reentry_tier1":
                    pos = next((p for p in positions if p.symbol == symbol), None)
                    if pos:
                        pos.sold_partial1_shares = sell_shares
                        pos.remaining_shares -= sell_shares
                        if actual_filled > 0 and actual_filled < sell_shares:
                            shortfall = sell_shares - actual_filled
                            log(f"RE-ENTRY TIER-1 PARTIAL FILL: {symbol} {actual_filled}/{sell_shares}sh, adjusting remaining_shares")
                            # Adjust: we decremented sell_shares but only actual_filled sold
                            pos.remaining_shares += shortfall
                        pos.breakeven_active = True
                else:
                    if actual_filled > 0 and actual_filled < sell_shares:
                        shortfall = sell_shares - actual_filled
                        log(f"SELL LIMIT PARTIAL FILL: {symbol} {actual_filled}/{sell_shares}sh, adding {shortfall} back to remaining_shares")
                        pos = next((p for p in positions if p.symbol == symbol), None)
                        if pos:
                            pos.remaining_shares += shortfall
                log(f"SELL LIMIT FILLED: {symbol} {actual_filled}sh order {order_id}")
                del pending_sells[order_id]
                pos = next((p for p in positions if p.symbol == symbol), None)
                if pos and pos.remaining_shares > 0:
                    replace_stop_for_remaining(pos)
            elif check_order_canceled(order_id):
                log(f"SELL LIMIT CANCELED: {symbol} order {order_id}")
                pos = next((p for p in positions if p.symbol == symbol), None)
                if pos:
                    # P0-6/P0-7: For reentry_tier1, remaining_shares was never decremented
                    if sell_type == "reentry_tier1":
                        pos.reached_target1 = False
                        pos.sold_partial1_shares = 0
                        pos.breakeven_active = False
                        # 恢复保护性止损 — 重试3次（防止裸仓）
                        if pos.remaining_shares > 0 and pos.protective_order_id is None:
                            result = None
                            for _rt_retry in range(3):
                                result = place_protective_stop(pos)
                                if result:
                                    log(f"  RE-ENTRY TIER-1 canceled: protective stop re-placed for {symbol} (attempt {_rt_retry+1})")
                                    break
                                log(f"  RE-ENTRY TIER-1 protective stop retry ({_rt_retry+1}/3) failed for {symbol}")
                                time.sleep(2)
                            if not result:
                                if pos.remaining_shares <= 0:
                                    # Emergency market sell closed the position
                                    record_trade(pos, pos.stop_price, "emergency_exit")
                                else:
                                    log(f"{RED}  RE-ENTRY TIER-1 protective stop FAILED 3/3 for {symbol} — INV-2 will catch next loop{RESET}")
                        # remaining_shares was never decremented, so no restore needed
                    else:
                        pos.remaining_shares += sell_shares
                        if tier_idx is None and pos.trade_type == "reentry":
                            pos.reached_target1 = False
                            pos.breakeven_active = False
                            pos.sold_partial1_shares = 0
                del pending_sells[order_id]

        # ── Check protective order fills + stop triggered but not filled ──
        for pos in positions[:]:
            if pos.remaining_shares <= 0:
                continue
            if pos.protective_order_id and check_order_filled(pos.protective_order_id):
                log(f"PROTECTIVE ORDER FILLED: {pos.symbol} order {pos.protective_order_id}")
                events_log.append(f"{now_est.strftime('%H:%M:%S')} PROTECTIVE FILLED {pos.symbol}")
                # Cancel any pending re-entry tier-1 limit sells for this symbol
                for oid in list(pending_sells.keys()):
                    if pending_sells[oid]["symbol"] == pos.symbol:
                        cancel_order(oid)
                        log(f"CANCEL pending re-entry sell {oid} for {pos.symbol} (protective stop filled)")
                        del pending_sells[oid]
                # 1.2: Cancel any active OCO orders for this position
                cancel_all_oco_for_position(pos)
                # Use actual fill price, not pos.stop_price — protective_order_id may be a
                # trailing stop whose fill price is much higher than the initial stop_price
                actual_fill_price = get_order_filled_price(pos.protective_order_id) if pos.protective_order_id else 0.0
                exit_price = actual_fill_price if actual_fill_price > 0 else pos.stop_price
                # Get actual filled qty — trailing stop may only cover (remaining - OCO_locked)
                # leaving OCO-locked shares orphaned on Alpaca
                actual_filled = get_order_filled_qty(pos.protective_order_id) if pos.protective_order_id else 0
                if actual_filled <= 0:
                    actual_filled = pos.remaining_shares  # fallback
                sold_amount = actual_filled
                pos.remaining_shares -= actual_filled
                pos.protective_order_id = None
                record_trade(pos, exit_price, "protective_stop", sold_shares=sold_amount)
                # If OCO-locked shares remain (released by cancel_all_oco above), sell them
                if pos.remaining_shares > 0:
                    log(f"OCO-ORPHAN: {pos.symbol} {pos.remaining_shares} shares remaining after trailing stop fill ({actual_filled} of {actual_filled + pos.remaining_shares}) — selling")
                    orphan_sold = force_sell_position(pos.symbol, pos.remaining_shares)
                    if orphan_sold > 0:
                        record_trade(pos, exit_price, "oco_orphan_sell", sold_shares=orphan_sold)
                        pos.remaining_shares -= orphan_sold
                    if pos.remaining_shares > 0:
                        # Verify Alpaca still has position before flagging error
                        if not DRY_RUN:
                            try:
                                ap = trading_client.get_open_position(pos.symbol)
                                alpaca_qty = int(float(ap.qty))
                                if alpaca_qty <= 0:
                                    log(f"OCO-ORPHAN: {pos.symbol} Alpaca已无仓位，清除本地残留")
                                else:
                                    pos.remaining_shares = min(pos.remaining_shares, alpaca_qty)
                                    log(f"{RED}OCO-ORPHAN: {pos.symbol} Alpaca仍有{alpaca_qty}股，同步本地→{pos.remaining_shares}{RESET}")
                            except Exception:
                                log(f"OCO-ORPHAN: {pos.symbol} Alpaca无仓位(查询异常)，清除本地残留")
                        else:
                            log(f"{RED}OCO-ORPHAN: {pos.symbol} still has {pos.remaining_shares} unsold shares!{RESET}")
                pos.remaining_shares = 0
                positions.remove(pos)
                continue
            # ── Stop triggered but limit not filled: price below stop → immediate market sell ──
            # When cur_price <= stop_price, the stop should have triggered.
            # If the stop-limit order's limit price is above cur_price, it won't fill.
            # Don't wait for 5 seconds — market sell immediately.
            snap = get_snapshots([pos.symbol]).get(pos.symbol)
            if snap and snap.latest_trade:
                cur_price = float(snap.latest_trade.price)
                if cur_price <= pos.stop_price:
                    log(f"{RED}STOP TRIGGERED BUT NOT FILLED: {pos.symbol} stop=${pos.stop_price:.4f} cur=${cur_price:.4f} — immediate market sell{RESET}")
                    events_log.append(f"{now_est.strftime('%H:%M:%S')} STOP_TRIGGERED_NOT_FILLED {pos.symbol} @ ${pos.stop_price:.4f}")
                    stop_loss_symbols.add(pos.symbol)
                    # 1.2: Cancel OCO orders before emergency market sell
                    cancel_all_oco_for_position(pos)
                    if pos.protective_order_id:
                        cancel_order(pos.protective_order_id)
                        _wait_cancel_confirmed(pos.protective_order_id, timeout=2.0)
                        pos.protective_order_id = None
                    sold = force_sell_position(pos.symbol, pos.remaining_shares)
                    if sold >= pos.remaining_shares:
                        pos.remaining_shares = 0
                        record_trade(pos, pos.stop_price, "stop_triggered_market_sell")
                        positions.remove(pos)
                    elif sold > 0:
                        pos.remaining_shares -= sold
                        log(f"STOP MARKET SELL PARTIAL: {pos.symbol} {sold} sold, {pos.remaining_shares} remain — re-placing stop")
                        replace_stop_for_remaining(pos)
                    else:
                        # force_sell returned 0 — check if Alpaca already closed position
                        alpaca_gone = False
                        if not DRY_RUN:
                            try:
                                ap = trading_client.get_open_position(pos.symbol)
                                if int(float(ap.qty)) <= 0:
                                    alpaca_gone = True
                            except Exception:
                                alpaca_gone = True
                        if alpaca_gone:
                            log(f"STOP SELL FAILED but Alpaca position gone: {pos.symbol} — clearing local state")
                            cancel_all_oco_for_position(pos)
                            pos.remaining_shares = 0
                            record_trade(pos, pos.stop_price, "stop_triggered_market_sell")
                            positions.remove(pos)
                        else:
                            log(f"{RED}STOP MARKET SELL FAILED: {pos.symbol} — re-placing protective stop{RESET}")
                            replace_stop_for_remaining(pos)
                    continue

        # Filter out closed positions to prevent double-sell race condition
        positions = [p for p in positions if p.remaining_shares > 0]

        # ── Force close ──
        if now_time >= force_close_time:
            log("Force close time reached!")
            cancel_all_orders()
            # 1.2: Cancel all OCO orders for each position before force selling
            for pos in positions:
                if pos.oco_order_ids:
                    cancel_all_oco_for_position(pos)
            for symbol in list(force_close_started.keys()):
                del force_close_started[symbol]
            for pos in positions:
                if pos.remaining_shares > 0:
                    snap = get_snapshots([pos.symbol]).get(pos.symbol)
                    bid_price = float(snap.latest_trade.price) if snap and snap.latest_trade else 0
                    if bid_price > 0:
                        limit_price = round(bid_price * 0.99, 2)
                        order = place_sell_limit(pos.symbol, pos.remaining_shares, limit_price)
                        if order:
                            force_close_started[pos.symbol] = now_est
                            events_log.append(f"{now_est.strftime('%H:%M:%S')} FORCE CLOSE LIMIT {pos.symbol} {pos.remaining_shares} @ ${limit_price:.2f}")
                            record_trade(pos, limit_price, "force_close")
                        else:
                            # Limit sell failed — use force_sell_position as fallback
                            sold = force_sell_position(pos.symbol, pos.remaining_shares)
                            if sold >= pos.remaining_shares:
                                record_trade(pos, bid_price, "force_close")
                                pos.remaining_shares = 0
                            elif sold > 0:
                                pos.remaining_shares -= sold
                                log(f"FORCE CLOSE PARTIAL: {pos.symbol} {sold} sold, {pos.remaining_shares} remain")
                            else:
                                log(f"FORCE CLOSE FAILED for {pos.symbol} — will try close_position in final sweep")
                    else:
                        sold = force_sell_position(pos.symbol, pos.remaining_shares)
                        if sold >= pos.remaining_shares:
                            record_trade(pos, pos.entry_price, "force_close")
                            pos.remaining_shares = 0
                        elif sold > 0:
                            pos.remaining_shares -= sold
                            log(f"FORCE CLOSE PARTIAL: {pos.symbol} {sold} sold, {pos.remaining_shares} remain")
                        else:
                            log(f"FORCE CLOSE FAILED for {pos.symbol} — will try close_position in final sweep")
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
            # ── Final naked position sweep ──
            # 不允许带着裸仓退出 — 逐个检查本地positions
            for pos in positions[:]:
                if pos.remaining_shares > 0:
                    log(f"{RED}EOD NAKED POSITION: {pos.symbol} {pos.remaining_shares}sh remaining — attempting final force_sell{RESET}")
                    # 尝试force_sell最多3次
                    for _fc_retry in range(3):
                        sold = force_sell_position(pos.symbol, pos.remaining_shares)
                        if sold >= pos.remaining_shares:
                            pos.remaining_shares = 0
                            record_trade(pos, pos.entry_price, "eod_force_close")
                            break
                        elif sold > 0:
                            pos.remaining_shares -= sold
                        else:
                            log(f"EOD force_sell retry ({_fc_retry+1}/3) failed for {pos.symbol}")
                            time.sleep(5)
                    if pos.remaining_shares > 0:
                        # 所有尝试失败 — 验证Alpaca端是否仍有持仓
                        try:
                            ap = trading_client.get_open_position(pos.symbol)
                            if int(float(ap.qty)) > 0:
                                log(f"{RED}CRITICAL: {pos.symbol} STILL HAS {ap.qty} SHARES ON ALPACA AFTER ALL EOD ATTEMPTS — manual intervention required!{RESET}")
                            else:
                                pos.remaining_shares = 0
                                log(f"EOD: {pos.symbol} Alpaca position already closed (local stale)")
                        except Exception:
                            pos.remaining_shares = 0
                            log(f"EOD: {pos.symbol} no Alpaca position found (local stale)")
            # 清理本地positions列表
            positions = [p for p in positions if p.remaining_shares <= 0]
            # Stop WebSocket stream
            if _stream_state:
                _stream_state.stop()
            break

        # ── Collect snapshot data ──
        # 1.0: Include position symbols in stream
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

        # ── Naked position timeout ──
        # 如果仓位连续3轮轮询都没有保护性止损，强制卖出（防止长时间裸仓）
        NAKED_TIMEOUT_POLLS = 3
        for pos in positions[:]:
            if pos.remaining_shares <= 0:
                continue
            if pos.protective_order_id:
                pos.naked_since_poll = 0  # 有保护性止损 → 重置计数器
            else:
                pos.naked_since_poll += 1
                if pos.naked_since_poll >= NAKED_TIMEOUT_POLLS:
                    log(f"{RED}NAKED TIMEOUT: {pos.symbol} unprotected for {pos.naked_since_poll} polls — force selling!{RESET}")
                    sold = force_sell_position(pos.symbol, pos.remaining_shares)
                    if sold >= pos.remaining_shares:
                        pos.remaining_shares = 0
                        record_trade(pos, pos.entry_price, "naked_timeout")
                        positions.remove(pos)
                    elif sold > 0:
                        pos.remaining_shares -= sold
                        log(f"NAKED TIMEOUT partial sell: {pos.symbol} {sold}sh, {pos.remaining_shares}sh remain")
                    else:
                        log(f"{RED}NAKED TIMEOUT force_sell FAILED: {pos.symbol} — will retry next poll{RESET}")

        # ── Pullback stop (15% from day high) -- per-stock, only HELD positions ──
        # Only sells the stock that triggered the stop, other positions continue
        # P1-15: Use rolling 20-bar window instead of daily bar
        for pos in positions[:]:
            if pos.remaining_shares <= 0:
                continue
            if pos.trade_type in ("recovered",):
                continue  # Skip pullback stop for recovered positions
            symbol = pos.symbol
            snap = snaps.get(symbol)
            if not snap:
                continue
            # Use pos.highest for dh (already tracked during holding period, no timezone issue)
            # For dl, use recent 1-min bars (entry-time filtered to exclude pre-entry gap bars)
            dh = pos.highest if pos.highest > 0 else day_highs.get(symbol, 0)
            recent_1min = accumulator.get_1min_bars(pos.symbol)
            dl = 0.0
            if len(recent_1min) >= 5:
                # Filter bars after entry time to exclude pre-entry gap bars
                if pos.entry_time:
                    # Convert entry_time to comparable format
                    entry_ts = pos.entry_time
                    post_entry = [b for b in recent_1min if b.get("timestamp", b.get("t", 0)) >= entry_ts]
                    window = post_entry[-20:] if len(post_entry) >= 20 else post_entry
                else:
                    window = recent_1min[-20:] if len(recent_1min) >= 20 else recent_1min
                if len(window) >= 2:
                    dl = min(b["low"] for b in window)
            else:
                # fallback to daily bar if insufficient 1-min data
                if snap.daily_bar:
                    dl = float(snap.daily_bar.low)
            if dh > 0 and dl > 0 and dh > dl and (dh - dl) / dh > config.PULLBACK_STOP_THRESHOLD:
                log(f"PULLBACK STOP: {symbol} dropped {(dh - dl) / dh:.1%} from high ${dh:.4f}")
                events_log.append(f"{now_est.strftime('%H:%M:%S')} PULLBACK STOP {symbol} -{(dh - dl) / dh:.1%}")
                # 1.2: Cancel OCO orders before pullback stop exit
                cancel_all_oco_for_position(pos)
                # Cancel protective stop first to avoid double-sell
                old_order_id = pos.protective_order_id
                if old_order_id:
                    cancel_order(old_order_id)
                    _wait_cancel_confirmed(old_order_id, timeout=2.0)
                    pos.protective_order_id = None
                sold_shares = pos.remaining_shares
                sold = force_sell_position(symbol, pos.remaining_shares)
                if sold >= pos.remaining_shares:
                    actual_sold = max(sold, sold_shares)
                    log(f"PULLBACK STOP FILLED: {symbol} {actual_sold} shares")
                    pos.remaining_shares = 0
                    pos.protective_order_id = None
                    entry_checked.add(symbol)
                    stop_loss_symbols.add(symbol)  # Prevent re-entry after 15% crash
                    record_trade(pos, dl, "pullback_stop", sold_shares=actual_sold)
                elif sold > 0:
                    pos.remaining_shares -= sold
                    log(f"PULLBACK STOP PARTIAL: {symbol} {sold} sold, {pos.remaining_shares} remain — re-placing stop")
                    replace_stop_for_remaining(pos)
                else:
                    log(f"PULLBACK STOP FORCE SELL FAILED: {symbol}, re-placing protective stop")
                    replace_stop_for_remaining(pos)

        # Clean up zero-share positions — record emergency exits that weren't logged
        for pos in positions[:]:
            if pos.remaining_shares <= 0 and pos.trade_type not in ("", None):
                # Check if this position was already recorded (avoid duplicate)
                already_recorded = any(t["symbol"] == pos.symbol and t["exit_reason"] == "emergency_exit" for t in trades_detail[-5:])
                if not already_recorded:
                    record_trade(pos, pos.stop_price, "emergency_exit")
        positions = [p for p in positions if p.remaining_shares > 0]

        # ── Daily loss circuit breaker (separate from pullback stop) ──
        if daily_stopped:
            _force_close_remaining(positions)
            positions = [p for p in positions if p.remaining_shares > 0]
            save_state(positions, candidates, daily_trades, daily_stopped,
                       entry_checked, day_highs, accumulator, events_log,
                       invariant_violation=_invariant_violation)
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
                stop_loss_symbols.add(pos.symbol)
                # 1.2: Cancel OCO orders before stop loss exit
                cancel_all_oco_for_position(pos)
                if pos.protective_order_id:
                    cancel_order(pos.protective_order_id)
                    _wait_cancel_confirmed(pos.protective_order_id, timeout=2.0)
                    pos.protective_order_id = None
                sold = force_sell_position(pos.symbol, pos.remaining_shares)
                if sold >= pos.remaining_shares:
                    pos.remaining_shares = 0
                    record_trade(pos, pos.stop_price, "stop_loss")
                    positions.remove(pos)
                elif sold > 0:
                    pos.remaining_shares -= sold
                    log(f"STOP LOSS PARTIAL: {pos.symbol} {sold} sold, {pos.remaining_shares} remain")
                    replace_stop_for_remaining(pos)
                else:
                    log(f"STOP LOSS FORCE SELL FAILED: {pos.symbol}, re-placing protective stop")
                    replace_stop_for_remaining(pos)
                continue

            # ── First trade / recovered: ladder sell system ──
            if pos.trade_type in ("first", "recovered"):
                need_replace_protective = False

                # ── T1 Activation: price reaches T1 target → market sell fraction ──
                # FIX 1.1: Cancel protective stop BEFORE selling, then re-place trailing stop
                # for remaining shares. This avoids the lock-up bug where protective stop
                # holds all shares and prevents partial market sell (Method 2 rejection →
                # Method 3 sells entire position instead of just the tier fraction).
                # Naked window: ~1-2 seconds between cancel and sell fill — acceptable risk.
                if pos.next_tier_idx == 0 and pos.targets:
                    if cur_price >= pos.targets[0]:
                        t1_shares = math.ceil(pos.shares / 8) if pos.shares >= 8 else 1
                        t1_shares = min(t1_shares, pos.remaining_shares)
                        if t1_shares > 0:
                            # Cancel protective stop to unlock shares for partial sell
                            if pos.protective_order_id:
                                cancel_order(pos.protective_order_id)
                                log(f"T1 SELL: cancelled protective stop {pos.protective_order_id} to unlock {t1_shares}sh for {pos.symbol}")
                                _wait_cancel_confirmed(pos.protective_order_id, timeout=2.0)
                                pos.protective_order_id = None
                            sold = force_sell_position(pos.symbol, t1_shares, intent="partial")
                            if sold >= pos.remaining_shares:
                                # close_position sold ALL shares (more than t1_shares)
                                sold_shares = pos.remaining_shares
                                pos.remaining_shares = 0
                                pos.reached_list[0] = True
                                log(f"T1 MARKET SELL (full exit): {pos.symbol} {sold}sh — close_position sold entire position")
                                record_trade(pos, cur_price, "t1_full_exit", sold_shares=sold_shares)
                                positions.remove(pos)
                                continue
                            elif sold > 0:
                                pos.sold_shares_list[0] = sold
                                pos.remaining_shares -= sold
                                pos.reached_list[0] = True
                                pos.next_tier_idx = 1
                                # Store T1 fill price for OCO stop calculation
                                t1_fill_price = cur_price  # approximate (market sell fills near cur_price)
                                # Try to get exact fill price from Alpaca
                                if not DRY_RUN:
                                    try:
                                        # The market sell order was placed by force_sell_position
                                        # We need the actual fill price — use cur_price as approximation
                                        # since market sells fill near current price
                                        pass
                                    except Exception:
                                        pass
                                if len(pos.tier_fill_prices) <= 0:
                                    pos.tier_fill_prices.append(t1_fill_price)
                                else:
                                    pos.tier_fill_prices[0] = t1_fill_price
                                log(f"T1 MARKET SELL: {pos.symbol} {sold}sh @ ~${cur_price:.4f}")
                                events_log.append(f"{now_est.strftime('%H:%M:%S')} T1 MARKET SELL {pos.symbol} {sold}sh @ ~${cur_price:.4f}")
                                add_chart_event(pos.symbol, "sell", cur_price, f"T1 {sold}sh")
                                record_trade(pos, cur_price, "t1_sell", sold_shares=sold)
                                # ── Place T2 OCO + trailing stop for remaining ──
                                if OCO_ENABLED and pos.remaining_shares > 0 and len(pos.targets) > 1:
                                    oco_result = place_oco_for_next_tier(pos, 1, prev_fill_price=t1_fill_price)
                                    if oco_result:
                                        need_replace_protective = False  # trailing already placed by place_oco_for_next_tier
                                        log(f"T1→T2 OCO setup complete for {pos.symbol}")
                                    else:
                                        # OCO failed — fall back to v1.1 trailing-only mode
                                        need_replace_protective = True
                                else:
                                    need_replace_protective = True
                            else:
                                log(f"T1 MARKET SELL FAILED: {pos.symbol}, re-placing protective stop NOW")
                                if pos.remaining_shares > 0:
                                    replace_stop_for_remaining(pos)

                # ── Time limit: if no tier filled in 40 min, sell at breakeven+ ──
                # bar_count tracks 5-min bars, not poll iterations
                if pos.trade_type == "first":
                    pos.bar_count = accumulator.bar_count(pos.symbol)
                    time_limit = getattr(config, "FIRST_TRADE_TIME_LIMIT_BARS", 0)
                    has_any_filled = any(pos.reached_list[:pos.next_tier_idx]) if pos.reached_list else False
                    if time_limit > 0 and not has_any_filled and pos.bar_count >= time_limit:
                        pos.time_limit_active = True
                if pos.trade_type == "first" and pos.time_limit_active and cur_price >= pos.entry_price and pos.remaining_shares > 0:
                    tl_bars = getattr(config, "FIRST_TRADE_TIME_LIMIT_BARS", 0)
                    log(f"TIME LIMIT EXIT: {pos.symbol} @ ${cur_price:.4f} (no target in {tl_bars * 5}min)")
                    events_log.append(f"{now_est.strftime('%H:%M:%S')} TIME LIMIT EXIT {pos.symbol} @ ${cur_price:.4f}")
                    add_chart_event(pos.symbol, "sell", cur_price, f"TIME LIMIT {pos.remaining_shares}sh")
                    # 1.2: Cancel OCO orders before time limit exit
                    cancel_all_oco_for_position(pos)
                    if pos.protective_order_id:
                        cancel_order(pos.protective_order_id)
                        _wait_cancel_confirmed(pos.protective_order_id, timeout=2.0)
                        pos.protective_order_id = None
                    sold = force_sell_position(pos.symbol, pos.remaining_shares)
                    if sold >= pos.remaining_shares:
                        pos.remaining_shares = 0
                        record_trade(pos, cur_price, "time_limit_exit")
                        positions.remove(pos)
                    elif sold > 0:
                        pos.remaining_shares -= sold
                        replace_stop_for_remaining(pos)
                    else:
                        log(f"TIME LIMIT FORCE SELL FAILED: {pos.symbol}, re-placing protective stop")
                        replace_stop_for_remaining(pos)
                    continue

                # ── OCO fill detection: check pre-placed OCO orders for T2+ ──
                if OCO_ENABLED and pos.oco_order_ids and pos.remaining_shares > 0:
                    for oco_entry in pos.oco_order_ids[:]:
                        if oco_entry.get("leg_filled") is not None:
                            continue  # already processed
                        filled, leg_type, fill_price = check_oco_fill(oco_entry)
                        if not filled:
                            continue

                        ti = oco_entry["tier_idx"]
                        tier_qty = oco_entry["qty"]

                        if leg_type == "canceled":
                            pos.oco_order_ids.remove(oco_entry)
                            log(f"OCO CANCELED: {pos.symbol} T{ti+1} order {oco_entry['order_id']}")
                            continue

                        oco_entry["leg_filled"] = leg_type

                        if leg_type == "limit":
                            # ── OCO limit leg filled: price reached T{ti+1} target ──
                            pos.sold_shares_list[ti] = tier_qty
                            pos.remaining_shares -= tier_qty
                            pos.reached_list[ti] = True
                            pos.next_tier_idx = ti + 1
                            # Store actual fill price
                            while len(pos.tier_fill_prices) <= ti:
                                pos.tier_fill_prices.append(0)
                            pos.tier_fill_prices[ti] = fill_price

                            log(f"OCO LIMIT FILLED: {pos.symbol} T{ti+1} {tier_qty}sh @ ${fill_price:.4f}")
                            events_log.append(f"{now_est.strftime('%H:%M:%S')} OCO LIMIT T{ti+1} {pos.symbol} {tier_qty}sh @ ${fill_price:.4f}")
                            add_chart_event(pos.symbol, "sell", fill_price, f"OCO T{ti+1} {tier_qty}sh")
                            record_trade(pos, fill_price, f"oco_t{ti+1}_sell", sold_shares=tier_qty)

                            # ── Skip-gap: check if price already above next targets ──
                            if pos.remaining_shares > 0:
                                skip_gap_tiers = []
                                next_ti = pos.next_tier_idx
                                while next_ti < len(pos.targets) and cur_price >= pos.targets[next_ti]:
                                    skip_gap_tiers.append(next_ti)
                                    next_ti += 1

                                if skip_gap_tiers:
                                    # Cancel current trailing stop to unlock shares
                                    if pos.protective_order_id:
                                        cancel_order(pos.protective_order_id)
                                        _wait_cancel_confirmed(pos.protective_order_id, timeout=2.0)
                                        pos.protective_order_id = None
                                    for sgt in skip_gap_tiers:
                                        sg_shares = math.ceil(pos.shares / 8) if pos.shares >= 8 else 1
                                        sg_shares = min(sg_shares, pos.remaining_shares)
                                        if sg_shares <= 0:
                                            break
                                        sold = force_sell_position(pos.symbol, sg_shares, intent="partial")
                                        if sold >= pos.remaining_shares:
                                            sold_shares = pos.remaining_shares
                                            pos.remaining_shares = 0
                                            pos.reached_list[sgt] = True
                                            pos.sold_shares_list[sgt] = sold_shares
                                            pos.next_tier_idx = sgt + 1
                                            while len(pos.tier_fill_prices) <= sgt:
                                                pos.tier_fill_prices.append(0)
                                            pos.tier_fill_prices[sgt] = cur_price
                                            record_trade(pos, cur_price, f"t{sgt+1}_skipgap_full_exit", sold_shares=sold_shares)
                                            positions.remove(pos)
                                            break
                                        elif sold > 0:
                                            pos.sold_shares_list[sgt] = sold
                                            pos.remaining_shares -= sold
                                            pos.reached_list[sgt] = True
                                            pos.next_tier_idx = sgt + 1
                                            while len(pos.tier_fill_prices) <= sgt:
                                                pos.tier_fill_prices.append(0)
                                            pos.tier_fill_prices[sgt] = cur_price
                                            log(f"SKIP-GAP T{sgt+1}: {pos.symbol} {sold}sh @ ${cur_price:.4f}")
                                            events_log.append(f"{now_est.strftime('%H:%M:%S')} SKIP-GAP T{sgt+1} {pos.symbol} {sold}sh")
                                            add_chart_event(pos.symbol, "sell", cur_price, f"SKIP-GAP T{sgt+1} {sold}sh")
                                            record_trade(pos, cur_price, f"t{sgt+1}_skipgap_sell", sold_shares=sold)
                                        else:
                                            log(f"SKIP-GAP T{sgt+1} FAILED: {pos.symbol}")
                                            if pos.remaining_shares > 0:
                                                replace_stop_for_remaining(pos)
                                            break

                            # ── After skip-gap (or no skip), place next OCO + trailing ──
                            if pos.remaining_shares > 0 and pos.next_tier_idx < len(pos.targets):
                                prev_fill = pos.tier_fill_prices[pos.next_tier_idx - 1] if pos.next_tier_idx > 0 and len(pos.tier_fill_prices) > pos.next_tier_idx - 1 else None
                                oco_result = place_oco_for_next_tier(pos, pos.next_tier_idx, prev_fill_price=prev_fill)
                                if oco_result:
                                    need_replace_protective = False  # trailing placed by place_oco_for_next_tier
                                else:
                                    need_replace_protective = True

                            pos.oco_order_ids.remove(oco_entry)
                            if pos.remaining_shares <= 0:
                                record_trade(pos, fill_price, f"oco_limit_t{ti+1}_exit")
                                positions.remove(pos)
                                continue

                        elif leg_type == "stop":
                            # ── OCO stop leg filled: tier sold at stop price ──
                            pos.sold_shares_list[ti] = tier_qty
                            pos.remaining_shares -= tier_qty
                            pos.reached_list[ti] = True
                            pos.next_tier_idx = ti + 1
                            while len(pos.tier_fill_prices) <= ti:
                                pos.tier_fill_prices.append(0)
                            pos.tier_fill_prices[ti] = fill_price

                            log(f"OCO STOP FILLED: {pos.symbol} T{ti+1} {tier_qty}sh @ ${fill_price:.4f} (stop leg — price reversed)")
                            events_log.append(f"{now_est.strftime('%H:%M:%S')} OCO STOP T{ti+1} {pos.symbol} {tier_qty}sh @ ${fill_price:.4f}")
                            add_chart_event(pos.symbol, "sell", fill_price, f"OCO STOP T{ti+1} {tier_qty}sh")
                            record_trade(pos, fill_price, f"oco_stop_t{ti+1}_sell", sold_shares=tier_qty)

                            pos.oco_order_ids.remove(oco_entry)
                            if pos.remaining_shares <= 0:
                                record_trade(pos, fill_price, f"oco_stop_t{ti+1}_exit")
                                positions.remove(pos)
                                continue

                            # Place next OCO for next tier (same as after limit fill)
                            if pos.next_tier_idx < len(pos.targets):
                                prev_fill = pos.tier_fill_prices[pos.next_tier_idx - 1] if pos.next_tier_idx > 0 and len(pos.tier_fill_prices) > pos.next_tier_idx - 1 else None
                                oco_result = place_oco_for_next_tier(pos, pos.next_tier_idx, prev_fill_price=prev_fill)
                                if oco_result:
                                    need_replace_protective = False
                                else:
                                    need_replace_protective = True

                # ── Polling fallback: place OCO if no active one for next tier, and price reached target ──
                # Handles: OCO placement failed, or OCO stop fill + OCO placement failed, then price recovers
                if OCO_ENABLED and pos.remaining_shares > 0 and pos.targets and pos.next_tier_idx > 0:
                    next_ti = pos.next_tier_idx
                    has_active_oco = any(e["tier_idx"] == next_ti and e.get("leg_filled") is None
                                         for e in pos.oco_order_ids)
                    if not has_active_oco and next_ti < len(pos.targets) and cur_price >= pos.targets[next_ti]:
                        # Price reached next target without an active OCO — market sell then place next OCO
                        if pos.protective_order_id:
                            cancel_order(pos.protective_order_id)
                            _wait_cancel_confirmed(pos.protective_order_id, timeout=2.0)
                            pos.protective_order_id = None
                        tier_shares = math.ceil(pos.shares / 8) if pos.shares >= 8 else 1
                        tier_shares = min(tier_shares, pos.remaining_shares)
                        if tier_shares > 0:
                            sold = force_sell_position(pos.symbol, tier_shares, intent="partial")
                            if sold >= pos.remaining_shares:
                                sold_shares = pos.remaining_shares
                                pos.remaining_shares = 0
                                pos.reached_list[next_ti] = True
                                pos.sold_shares_list[next_ti] = sold_shares
                                pos.next_tier_idx = next_ti + 1
                                while len(pos.tier_fill_prices) <= next_ti:
                                    pos.tier_fill_prices.append(0)
                                pos.tier_fill_prices[next_ti] = cur_price
                                record_trade(pos, cur_price, f"t{next_ti+1}_polling_full_exit", sold_shares=sold_shares)
                                positions.remove(pos)
                                continue
                            elif sold > 0:
                                pos.sold_shares_list[next_ti] = sold
                                pos.remaining_shares -= sold
                                pos.reached_list[next_ti] = True
                                pos.next_tier_idx = next_ti + 1
                                while len(pos.tier_fill_prices) <= next_ti:
                                    pos.tier_fill_prices.append(0)
                                pos.tier_fill_prices[next_ti] = cur_price
                                log(f"POLLING T{next_ti+1}: {pos.symbol} {sold}sh @ ${cur_price:.4f}")
                                events_log.append(f"{now_est.strftime('%H:%M:%S')} POLLING T{next_ti+1} {pos.symbol} {sold}sh")
                                add_chart_event(pos.symbol, "sell", cur_price, f"POLLING T{next_ti+1} {sold}sh")
                                record_trade(pos, cur_price, f"t{next_ti+1}_polling_sell", sold_shares=sold)
                                # Place next OCO + trailing
                                if pos.remaining_shares > 0 and pos.next_tier_idx < len(pos.targets):
                                    prev_fill = pos.tier_fill_prices[pos.next_tier_idx - 1] if len(pos.tier_fill_prices) > pos.next_tier_idx - 1 else None
                                    oco_result = place_oco_for_next_tier(pos, pos.next_tier_idx, prev_fill_price=prev_fill)
                                    if oco_result:
                                        need_replace_protective = False
                                    else:
                                        need_replace_protective = True
                            else:
                                log(f"POLLING T{next_ti+1} FAILED: {pos.symbol}")
                                if pos.remaining_shares > 0:
                                    replace_stop_for_remaining(pos)

                # ── v1.1 polling fallback: if OCO_ENABLED is False, use old while loop ──
                if not OCO_ENABLED and pos.next_tier_idx > 0:
                    while pos.next_tier_idx < len(pos.targets) and pos.targets:
                        ti = pos.next_tier_idx
                        if cur_price < pos.targets[ti]:
                            break
                        tier_shares = math.ceil(pos.shares / 8) if pos.shares >= 8 else 1
                        tier_shares = min(tier_shares, pos.remaining_shares)
                        if tier_shares <= 0:
                            break
                        if pos.protective_order_id:
                            cancel_order(pos.protective_order_id)
                            _wait_cancel_confirmed(pos.protective_order_id, timeout=2.0)
                            pos.protective_order_id = None
                        sold = force_sell_position(pos.symbol, tier_shares, intent="partial")
                        if sold >= pos.remaining_shares:
                            sold_shares = pos.remaining_shares
                            pos.remaining_shares = 0
                            pos.reached_list[ti] = True
                            log(f"T{ti+1} MARKET SELL (full exit): {pos.symbol} {sold}sh — sold entire remaining position")
                            record_trade(pos, cur_price, f"t{ti+1}_full_exit", sold_shares=sold_shares)
                            positions.remove(pos)
                            break
                        elif sold > 0:
                            pos.sold_shares_list[ti] = sold
                            pos.remaining_shares -= sold
                            pos.reached_list[ti] = True
                            pos.next_tier_idx = ti + 1
                            need_replace_protective = True
                            log(f"T{ti+1} MARKET SELL: {pos.symbol} {sold}sh @ ~${cur_price:.4f}")
                            events_log.append(f"{now_est.strftime('%H:%M:%S')} T{ti+1} MARKET SELL {pos.symbol} {sold}sh @ ~${cur_price:.4f}")
                            add_chart_event(pos.symbol, "sell", cur_price, f"T{ti+1} {sold}sh")
                            record_trade(pos, cur_price, f"t{ti+1}_sell", sold_shares=sold)
                        else:
                            log(f"T{ti+1} MARKET SELL FAILED: {pos.symbol}, re-placing protective stop NOW")
                            if pos.remaining_shares > 0:
                                replace_stop_for_remaining(pos)
                            break

                # ── Replace protective stop ──
                if need_replace_protective and pos.remaining_shares > 0:
                    replace_stop_for_remaining(pos)

                # ── Trailing stop (polled fallback) ──
                if pos.reached_list and any(pos.reached_list) and pos.remaining_shares > 0:
                    pct = get_trailing_pct(pos)
                    tsp = round(pos.highest * (1 - pct), 2)
                    tsp = max(tsp, pos.entry_price)
                    if cur_price <= tsp:
                        # No pending ladder sells to cancel — market sells fill immediately
                        tier_label = "trailing"
                        if pos.reached_list:
                            for tidx in range(len(pos.reached_list) - 1, -1, -1):
                                if pos.reached_list[tidx]:
                                    retracements = getattr(config, "PROFIT_RETRACEMENT_TIERS", [0.10, 0.20, 0.35, 0.50, 0.75, 1.00, 1.25, 1.50])
                                    tier_label = f"{int(retracements[tidx]*100)}%" if tidx < len(retracements) else f"T{tidx+1}"
                                    break
                        log(f"TRAILING STOP({tier_label}) (polled): {pos.symbol} @ ${tsp:.4f} (high=${pos.highest:.4f})")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} TRAILING STOP({tier_label}) {pos.symbol} @ ${tsp:.4f}")
                        # 1.2: Cancel all OCO orders — trailing stop exits the position
                        cancel_all_oco_for_position(pos)
                        if pos.protective_order_id:
                            cancel_order(pos.protective_order_id)
                            _wait_cancel_confirmed(pos.protective_order_id, timeout=2.0)
                            pos.protective_order_id = None
                        sold = force_sell_position(pos.symbol, pos.remaining_shares)
                        if sold >= pos.remaining_shares:
                            pos.remaining_shares = 0
                            record_trade(pos, cur_price, "trailing_stop")
                            positions.remove(pos)
                        elif sold > 0:
                            pos.remaining_shares -= sold
                            log(f"TRAILING STOP PARTIAL: {pos.symbol} {sold} sold, {pos.remaining_shares} remain")
                            replace_stop_for_remaining(pos)
                        else:
                            log(f"TRAILING STOP FORCE SELL FAILED: {pos.symbol}, re-placing protective stop")
                            replace_stop_for_remaining(pos)
                        continue

        # ── Check entries for candidates ──
        if _invariant_violation:
            # 不变量违规时只处理已有仓位，不开新仓
            pass
        elif now_time >= cutoff_time and not _entry_window_closed:
            _entry_window_closed = True
            log(f"Entry window closed ({cutoff_time}). No entries will be placed.")
        if now_time < cutoff_time and (config.MAX_DAILY_TRADES == 0 or daily_trades < config.MAX_DAILY_TRADES) and not daily_stopped and not _pdt_detected:
            # Daily loss circuit breaker — check before any new entry
            if _check_circuit_breaker(snaps):
                daily_stopped = True
        if now_time < cutoff_time and (config.MAX_DAILY_TRADES == 0 or daily_trades < config.MAX_DAILY_TRADES) and not daily_stopped and not _pdt_detected:
            force_qty = getattr(config, "FORCE_QTY", 0)
            # Track total allocated this cycle — deduct from pool after each buy
            allocated_this_cycle = 0
            for cand in candidates:
                # Simultaneous positions cap — stop entering new positions
                if config.MAX_POSITIONS_PER_DAY > 0 and len(positions) + len(pending_buys) >= config.MAX_POSITIONS_PER_DAY:
                    log(f"Position cap: {len(positions) + len(pending_buys)}/{config.MAX_POSITIONS_PER_DAY} — no more entries")
                    break
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

                entry_price, confirmed = check_entry_1min(symbol, cand["open_price"], accumulator)
                if not confirmed or entry_price <= 0:
                    log(f"  {symbol}: no entry confirmation yet (1min bars={len(accumulator.get_1min_bars(symbol))})")
                    continue

                # 0.4.11: Skip if entry price >= open price (no chasing above open)
                # In FORCE_QTY test mode, allow momentum entries to verify cap-only targets
                if getattr(config, "ENTRY_BELOW_OPEN", True) and entry_price >= cand["open_price"] and force_qty == 0:
                    log(f"  {symbol}: entry ${entry_price:.4f} >= open ${cand['open_price']:.4f}, skipping")
                    entry_checked.add(symbol)
                    continue

                bars_5m = accumulator.get_5min_bars(symbol)
                # P1-11: Use historical (prev-day) ATR first, fallback to intra-day
                atr = get_prev_day_atr(symbol)
                if atr <= 0:
                    atr = calc_atr(bars_5m, 14)
                    log(f"  {symbol}: historical ATR unavailable, using intra-day ATR={atr:.4f}")
                else:
                    log(f"  {symbol}: using historical ATR={atr:.4f}")

                stop = calc_stop_price(entry_price, atr)

                # 1.0: 8-tier targets via calc_targets
                targets, sell_ratios, trail_pcts, target_mode = calc_targets(entry_price, cand["open_price"])

                # Slot-based allocation: divide buying power by MAX_POSITIONS_PER_DAY slots
                # This gives consistent per-stock allocation regardless of candidate count
                if config.MAX_DAILY_TRADES > 0:
                    remaining_slots = config.MAX_DAILY_TRADES - daily_trades - len(pending_buys)
                else:
                    remaining_slots = config.MAX_POSITIONS_PER_DAY - len(positions) - len(pending_buys)
                if remaining_slots <= 0:
                    break
                try:
                    bp = float(trading_client.get_account().buying_power)
                except Exception:
                    bp = capital * 0.95
                bp_available = bp - allocated_this_cycle
                if bp_available <= 0:
                    log(f"  No buying power left (${bp_available:.2f}), stopping entries")
                    break
                pos_size = bp_available / remaining_slots
                pos_size = min(pos_size, config.MAX_POSITION_SIZE)
                if bp < pos_size:
                    log(f"  {symbol}: buying power ${bp:.2f} < alloc ${pos_size:.2f}, will retry when capital freed")
                    continue  # v1.3: don't permanently skip — bp may increase when positions exit
                shares = int(pos_size / entry_price)
                if force_qty > 0:
                    shares = force_qty
                if shares <= 0:
                    continue  # v1.3: don't permanently skip — allocation may change with freed slots
                # MIN_POSITION_SIZE check — skip if position too small
                min_pos = getattr(config, "MIN_POSITION_SIZE", 0)
                if min_pos > 0 and shares * entry_price < min_pos:
                    log(f"  {symbol}: position ${shares * entry_price:.2f} < MIN_POSITION_SIZE ${min_pos}, will retry when more slots free")
                    continue  # v1.3: don't permanently skip — allocation may change with freed slots

                # Check real-time ask price vs entry price — reject excessive slippage
                if not DRY_RUN and MAX_ENTRY_SLIPPAGE > 0:
                    try:
                        snap = get_snapshots([symbol]).get(symbol)
                        ask_price = float(snap.latest_quote.ask_price) if snap and snap.latest_quote else 0
                        max_allowed = entry_price * (1 + MAX_ENTRY_SLIPPAGE)
                        if ask_price > max_allowed and ask_price > 0:
                            slippage_pct = (ask_price - entry_price) / entry_price * 100
                            log(f"  {symbol}: ask ${ask_price:.4f} > entry ${entry_price:.4f} × {1+MAX_ENTRY_SLIPPAGE:.2f} = ${max_allowed:.4f} — slippage {slippage_pct:.1f}% exceeds {MAX_ENTRY_SLIPPAGE:.0%} cap, skipping")
                            entry_checked.add(symbol)
                            continue
                        elif ask_price > 0:
                            slippage_pct = (ask_price - entry_price) / entry_price * 100
                            log(f"  {symbol}: ask ${ask_price:.4f} vs entry ${entry_price:.4f} — slippage {slippage_pct:.1f}% OK (cap {MAX_ENTRY_SLIPPAGE:.0%})")
                    except Exception as e:
                        log(f"  {symbol}: ask price check failed ({e}), proceeding anyway")

                order, pdt_hit, actual_shares, reject_cat = place_buy_market(symbol, shares)
                if pdt_hit:
                    _pdt_detected = True
                if order:
                    # place_buy_market已等待确认成交
                    actual_fill_price = get_order_filled_price(str(order.id)) if not DRY_RUN else entry_price
                    if actual_fill_price > 0:
                        # 已确认成交，使用实际成交价直接建仓
                        entry_price = actual_fill_price
                        stop = calc_stop_price(entry_price, atr)
                        targets, sell_ratios, trail_pcts, target_mode = calc_targets(entry_price, cand["open_price"])
                        log(f"  Recalc after fill: {symbol} entry=${entry_price:.4f} stop=${stop:.4f} mode={target_mode}")
                        pos_data = {
                            "symbol": symbol, "entry_price": entry_price, "shares": actual_shares,
                            "stop_price": stop, "open_price": cand["open_price"],
                            "entry_time": now_est, "atr": atr,
                            "targets": targets, "sell_ratios": sell_ratios,
                            "trail_pcts": trail_pcts, "target_mode": target_mode,
                            "reached_list": [False] * len(targets),
                            "sold_shares_list": [0] * len(targets),
                        }
                        # 直接建仓+挂protective stop
                        pos = LivePosition(**pos_data)
                        positions.append(pos)
                        entered_symbols.add(symbol)
                        entry_checked.add(symbol)
                        allocated_this_cycle += actual_shares * entry_price
                        ps_result = None
                        for _ps_attempt in range(3):
                            ps_result = place_protective_stop(pos)
                            if ps_result:
                                log(f"PROTECTIVE STOP placed for all {pos.shares} shares: {pos.symbol} stop=${pos.stop_price:.4f}")
                                break
                            log(f"PROTECTIVE STOP retry ({_ps_attempt+1}/3) for {pos.symbol}")
                            time.sleep(2)
                        if not ps_result:
                            if pos.remaining_shares <= 0:
                                record_trade(pos, pos.stop_price, "emergency_exit")
                                log(f"EMERGENCY EXIT: {pos.symbol} — protective stop failed, market sold all shares")
                            else:
                                log(f"{RED}WARNING: Protective stop FAILED 3/3 for {pos.symbol} — INV-2 will catch next loop{RESET}")
                        log(f"BUY FILLED: {symbol} {actual_shares}sh @ ${entry_price:.4f} stop=${stop:.4f} targets={[round(t, 2) for t in targets]} mode={target_mode}")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} BUY FILLED {symbol} @ ${entry_price:.4f}")
                        add_chart_event(symbol, "buy", entry_price, f"BUY {actual_shares}sh")
                        if _stream_state:
                            _stream_state.update_symbols([symbol])
                    else:
                        # 超时未确认，走pending_buys兜底路径
                        pos_data = {
                            "symbol": symbol, "entry_price": entry_price, "shares": actual_shares,
                            "stop_price": stop, "open_price": cand["open_price"],
                            "entry_time": now_est, "atr": atr,
                            "targets": targets, "sell_ratios": sell_ratios,
                            "trail_pcts": trail_pcts, "target_mode": target_mode,
                            "reached_list": [False] * len(targets),
                            "sold_shares_list": [0] * len(targets),
                        }
                        pending_buys[symbol] = (str(order.id), pos_data)
                        entry_checked.add(symbol)
                        allocated_this_cycle += actual_shares * entry_price
                        log(f"{YELLOW}BUY MARKET PENDING (timeout): {symbol} entry=${entry_price:.4f}, "
                            f"stop=${stop:.4f}, targets={[round(t, 2) for t in targets]}, mode={target_mode}, shares={actual_shares}{RESET}")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} BUY MARKET PENDING {symbol} @ ${entry_price:.4f}")
                else:
                    # 瞬态错误(rate_limit/network)不永久排除，下一轮可重试
                    if reject_cat in ("rate_limit", "network"):
                        log(f"BUY ORDER FAILED (transient): {symbol} {reject_cat} — will retry next poll")
                    else:
                        log(f"BUY ORDER FAILED: {symbol} {reject_cat} — skipping, no retry")
                        entry_checked.add(symbol)

        # ── Check re-entry ──
        # Simplified: 1-min bar entry + single trailing stop 1% after target
        if _invariant_violation:
            pass
        elif now_time < reentry_cutoff_time and (config.MAX_DAILY_TRADES == 0 or daily_trades < config.MAX_DAILY_TRADES) and not daily_stopped and not _pdt_detected:
            exited_symbols = entered_symbols - {p.symbol for p in positions} - set(pending_buys.keys()) - stop_loss_symbols
            for symbol in exited_symbols:
                # Simultaneous positions cap — stop entering new positions
                if config.MAX_POSITIONS_PER_DAY > 0 and len(positions) + len(pending_buys) >= config.MAX_POSITIONS_PER_DAY:
                    log(f"Position cap: {len(positions) + len(pending_buys)}/{config.MAX_POSITIONS_PER_DAY} — no more re-entries")
                    break
                if symbol in reentry_checked:
                    continue
                cand = next((c for c in candidates if c['symbol'] == symbol), None)
                if not cand:
                    log(f"  RE-ENTRY SKIP {symbol}: not in candidates list")
                    continue

                # 1-min bar entry detection (same logic as first trade)
                reentry_min_pb = getattr(config, "REENTRY_MIN_PULLBACK", 0.04)
                entry_price, prev_high, confirmed = check_reentry_1min(symbol, cand["open_price"], accumulator, min_pullback=reentry_min_pb)
                if not confirmed or entry_price <= 0:
                    log(f"  {symbol}: no re-entry confirmation yet (1min bars={len(accumulator.get_1min_bars(symbol))})")
                    continue

                # Daily loss circuit breaker — check before re-entry
                if _check_circuit_breaker(snaps):
                    daily_stopped = True
                    break

                # ATR-based stop for re-entry
                atr = get_prev_day_atr(symbol)
                if atr <= 0:
                    # Fallback: use 1-min bars for intra-day ATR
                    bars_1m = accumulator.get_1min_bars(symbol)
                    atr = calc_atr(bars_1m, period=14) if len(bars_1m) >= 14 else 0
                    log(f"  {symbol}: re-entry ATR from 1min bars={atr:.4f}")
                else:
                    log(f"  {symbol}: re-entry using historical ATR={atr:.4f}")
                if atr > 0:
                    stop = round(entry_price - 1.5 * atr, 2)
                    stop = max(stop, round(entry_price * (1 - config.REENTRY_STOP_PCT_FALLBACK), 2))
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

                # 0.4.10: Dynamic re-entry allocation — deduct already allocated
                reentry_pos_ratio = getattr(config, "REENTRY_POSITION_RATIO", 0.5)
                try:
                    bp = float(trading_client.get_account().buying_power)
                except Exception:
                    bp = capital * 0.95
                bp_available = bp - allocated_this_cycle
                # Re-entry gets half of what a first trade would get
                # Slot-based allocation: same as first trade
                if config.MAX_DAILY_TRADES > 0:
                    remaining_slots = config.MAX_DAILY_TRADES - daily_trades - len(pending_buys)
                else:
                    remaining_slots = config.MAX_POSITIONS_PER_DAY - len(positions) - len(pending_buys)
                first_trade_alloc = bp_available / max(remaining_slots, 1)
                reentry_size = first_trade_alloc * reentry_pos_ratio
                reentry_size = min(reentry_size, config.MAX_POSITION_SIZE)
                if bp_available < reentry_size:
                    log(f"  {symbol}: re-entry skipped, buying power ${bp:.2f} < alloc ${reentry_size:.2f}, will retry when capital freed")
                    continue  # v1.3: don't permanently skip — bp may increase when positions exit
                shares = int(reentry_size / entry_price)
                if force_qty > 0:
                    shares = max(1, force_qty // 2)
                if shares <= 0:
                    continue  # v1.3: don't permanently skip
                # MIN_POSITION_SIZE check for re-entry (lower threshold since re-entry uses half position)
                min_pos = getattr(config, "REENTRY_MIN_POSITION_SIZE", 0) or getattr(config, "MIN_POSITION_SIZE", 0)
                if min_pos > 0 and shares * entry_price < min_pos:
                    log(f"  {symbol}: re-entry position ${shares * entry_price:.2f} < MIN_POSITION_SIZE ${min_pos}, will retry when more slots free")
                    continue  # v1.3: don't permanently skip — allocation may change with freed slots

                # Check real-time ask price vs entry price — reject excessive slippage
                if not DRY_RUN and MAX_ENTRY_SLIPPAGE > 0:
                    try:
                        snap = get_snapshots([symbol]).get(symbol)
                        ask_price = float(snap.latest_quote.ask_price) if snap and snap.latest_quote else 0
                        max_allowed = entry_price * (1 + MAX_ENTRY_SLIPPAGE)
                        if ask_price > max_allowed and ask_price > 0:
                            slippage_pct = (ask_price - entry_price) / entry_price * 100
                            log(f"  {symbol}: re-entry ask ${ask_price:.4f} > entry ${entry_price:.4f} × {1+MAX_ENTRY_SLIPPAGE:.2f} = ${max_allowed:.4f} — slippage {slippage_pct:.1f}% exceeds {MAX_ENTRY_SLIPPAGE:.0%} cap, skipping")
                            reentry_checked.add(symbol)
                            continue
                        elif ask_price > 0:
                            slippage_pct = (ask_price - entry_price) / entry_price * 100
                            log(f"  {symbol}: re-entry ask ${ask_price:.4f} vs entry ${entry_price:.4f} — slippage {slippage_pct:.1f}% OK")
                    except Exception as e:
                        log(f"  {symbol}: re-entry ask price check failed ({e}), proceeding anyway")

                order, pdt_hit, actual_shares, reject_cat = place_buy_market(symbol, shares)
                if pdt_hit:
                    _pdt_detected = True
                if order:
                    # 使用实际成交价重新计算（place_buy_market已等待确认）
                    actual_fill_price = get_order_filled_price(str(order.id)) if not DRY_RUN else entry_price
                    if actual_fill_price > 0 and actual_fill_price != entry_price:
                        log(f"RE-ENTRY actual fill: {symbol} @ ${actual_fill_price:.4f} (expected ${entry_price:.4f})")
                        entry_price = actual_fill_price
                        stop = calc_stop_price(entry_price, atr)
                    # v1.3: Re-entry uses same 8-tier ladder as first trade
                    # open_price = prev_high (gap = prev_high - entry_price)
                    targets, sell_ratios, trail_pcts, target_mode = calc_targets(entry_price, prev_high)
                    pos = LivePosition(
                        symbol=symbol, entry_price=entry_price, shares=actual_shares,
                        stop_price=stop, open_price=cand["open_price"],
                        trade_type="reentry", prev_high=prev_high,
                        reentry_target=target, entry_time=now_est,
                        atr=atr,
                        targets=targets, sell_ratios=sell_ratios, trail_pcts=trail_pcts,
                        target_mode=target_mode,
                    )
                    positions.append(pos)
                    # Cancel any lingering sell orders for this symbol before placing protective stop
                    # (old OCO/trailing from first trade may still lock shares)
                    if not DRY_RUN:
                        try:
                            old_orders = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]))
                            for o in old_orders:
                                if o.side == OrderSide.SELL:
                                    cancel_order(str(o.id))
                                    _wait_cancel_confirmed(str(o.id), timeout=2.0)
                                    log(f"  RE-ENTRY: cancelled old sell order {o.id} for {symbol} (unlocking shares)")
                        except Exception:
                            pass
                    prot_result = place_protective_stop(pos)
                    if pos.remaining_shares <= 0:
                        # Emergency market sell closed the position
                        record_trade(pos, pos.stop_price, "emergency_exit")
                        positions.remove(pos)
                    entered_symbols.add(symbol)  # Track for re-entry eligibility
                    reentry_checked.add(symbol)
                    allocated_this_cycle += actual_shares * entry_price
                    log(f"RE-ENTERED {symbol}: entry=${entry_price:.4f}, "
                        f"stop=${stop:.4f}, target=${target:.4f}, prev_high=${prev_high:.4f}, shares={shares}, atr=${atr:.4f}")
                    events_log.append(f"{now_est.strftime('%H:%M:%S')} RE-ENTERED {symbol} @ ${entry_price:.4f} (v2)")
                    # Update WebSocket subscriptions
                    if _stream_state:
                        _stream_state.update_symbols([symbol])
        elif now_time >= reentry_cutoff_time and poll_count == 1:
            log(f"Re-entry cutoff reached ({REENTRY_CUTOFF} EST). No more re-entries.")

        # ── Cleanup fully exited positions ──
        # Cancel any orphan protective orders before removing positions
        for p in positions:
            if p.remaining_shares <= 0 and p.protective_order_id:
                cancel_order(p.protective_order_id)
                p.protective_order_id = None
        positions = [p for p in positions if p.remaining_shares > 0]

        # ── 不变量检查（每4轮轮询检查一次，避免API限频）──
        # 只检测+记录修复指令，不直接修改状态（保证不影响交易）
        if poll_count % 4 == 0 and not DRY_RUN:
            inv_errors, inv_new_fixes, inv_critical = check_invariants()
            if inv_errors:
                log(f"{RED}⚠️ 不变量违规({len(inv_errors)}条)!{RESET}")
                for err in inv_errors:
                    log(f"  {RED}{err}{RESET}")
                if inv_new_fixes:
                    log(f"  修复指令已记录，下一轮执行({len(inv_new_fixes)}条)")
                    _invariant_fixes.extend(inv_new_fixes)
                if inv_critical:
                    _invariant_violation = True
                    log(f"{RED}严重违规 → 暂停新入场，仅处理已有仓位{RESET}")
            elif _invariant_violation and not _invariant_fixes:
                # 违规已解除且无待修复项
                _invariant_violation = False
                log(f"{GREEN}✓ 不变量全部通过，恢复入场{RESET}")

        # ── Save state ──
        save_state(positions, candidates, daily_trades, daily_stopped,
                   entry_checked, day_highs, accumulator, events_log,
                   invariant_violation=_invariant_violation)
        save_chart_data(accumulator, positions, chart_events, str(now_est.date()))

        # ── Alpaca position sync: detect orphan positions not tracked locally ──
        if poll_count % 20 == 0 and not DRY_RUN:
          try:
            alpaca_pos = trading_client.get_all_positions()
            tracked_syms = {p.symbol for p in positions}
            for ap in alpaca_pos:
                sym = ap.symbol
                qty = int(float(ap.qty))
                if sym not in tracked_syms:
                    log(f"{RED}ORPHAN-DETECT: Alpaca has {sym} {qty}sh not tracked locally — selling immediately{RESET}")
                    sold = force_sell_position(sym, qty)
                    if sold > 0:
                        log(f"ORPHAN-DETECT: Sold {sold} shares of {sym}")
                    else:
                        log(f"{RED}ORPHAN-DETECT: Failed to sell {sym}!{RESET}")
          except Exception as e:
              log(f"ORPHAN-DETECT error: {e}")

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
                    # Show target mode, reached tiers, and ladder progress
                    reached_tiers = [i+1 for i, r in enumerate(pos.reached_list) if r] if pos.reached_list else []
                    ladder_str = f"T{pos.next_tier_idx+1}" if pos.next_tier_idx < len(pos.targets) else "COMPLETE"
                    mode_info = f", mode={pos.target_mode}, tiers={reached_tiers}, ladder={ladder_str}" if pos.targets else ""
                    log(f"  {pos.symbol}({pos.trade_type}): {pos.remaining_shares} shares, "
                        f"entry=${pos.entry_price:.4f} cur=${cur:.4f} pnl=${pnl:.2f}{mode_info}{protective}")

        # ── v1.3: Mid-day re-scan at 10:30 and 11:30 ──
        rescan_times = getattr(config, "RESCAN_TIMES", ["10:30", "11:30"])
        now_est = dt.datetime.now(tz=ZoneInfo("America/New_York"))
        now_hm = now_est.strftime("%H:%M")
        if now_hm in rescan_times and now_hm != _last_rescan_time:
            _last_rescan_time = now_hm
            log(f"[v1.3] Mid-day re-scan at {now_hm}...")
            new_candidates = scan_gaps()
            if new_candidates:
                existing_symbols = {c["symbol"] for c in candidates}
                new_symbols = [c for c in new_candidates if c["symbol"] not in existing_symbols
                               and c["symbol"] not in entry_checked
                               and c["symbol"] not in [p.symbol for p in positions]]
                if new_symbols:
                    candidates.extend(new_symbols)
                    log(f"[v1.3] Re-scan found {len(new_symbols)} new candidates: {[c['symbol'] for c in new_symbols]}")
                    for c in new_symbols:
                        day_highs[c['symbol']] = c['open_price']
                        # Backfill 1-min bars for new candidates
                        try:
                            req = StockBarsRequest(
                                symbol_or_symbols=[c['symbol']],
                                timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                                start=now_est.replace(hour=9, minute=30, second=0, microsecond=0),
                                end=now_est, feed=DATA_FEED,
                            )
                            new_bars = data_client.get_stock_bars(req)
                            if not new_bars.df.empty:
                                for _, row in new_bars.df.iterrows():
                                    accumulator.add_bar(c['symbol'], row, is_1min=True)
                        except Exception as e:
                            log(f"  {c['symbol']}: backfill failed ({e})")
                else:
                    log(f"[v1.3] Re-scan found no new candidates (all already known)")
            else:
                log(f"[v1.3] Re-scan found no gap stocks")

        # P1-19: WebSocket reconnect check
        if _stream_state and _stream_state._running:
            if time.time() - _stream_state._last_bar_time > 60:
                log("WebSocket: no bars for 60s, restarting stream...")
                symbols = [c["symbol"] for c in candidates] + [p.symbol for p in positions]
                _stream_state.restart(symbols)

        time.sleep(getattr(config, "POLL_INTERVAL", 3))  # polling interval (3s with batch cache, 5s without)

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
               entry_checked, day_highs, accumulator, events_log,
               invariant_violation=_invariant_violation)
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
                open_orders = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]))
                sell_orders = [o for o in open_orders if o.side == OrderSide.SELL]
                if not sell_orders:
                    # Verify Alpaca position is actually gone
                    try:
                        alpaca_pos = trading_client.get_open_position(symbol)
                        if alpaca_pos and int(float(alpaca_pos.qty)) > 0:
                            log(f"FORCE CLOSE: {symbol} still has Alpaca position, retrying...")
                            # Retry force sell
                            for pos in positions:
                                if pos.symbol == symbol and pos.remaining_shares > 0:
                                    force_sell_position(symbol, pos.remaining_shares)
                                    pos.remaining_shares = 0
                        else:
                            del force_close_started[symbol]
                    except Exception:
                        # Position not found = success
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
    if "--dry-run" in sys.argv:
        config.DRY_RUN = True
        DRY_RUN = True
    run_live()
