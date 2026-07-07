"""Stone 0.4.8 Live Paper Trading — pure trailing stop (no partial sells).

Changes over 0.4.5:
- Remove three-tier partial profit targets (1/4@75%, 1/3@112.5%, 1/3@150%)
- Replace with pure trailing stop: activate at 75% retracement, trail at 1%
- All shares exit together via trailing stop, stop loss, or force close
- If trailing never activates by force close, exit at breakeven (entry_price)
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

import config
from scanner import get_tradable_symbols
from strategy import (
    calc_atr, calc_stop_price, calc_position_size,
    find_reentry_point,
)

# ── 0.4.8 Parameters ────────────────────────────────────────────────
ENTRY_LIMIT_BUFFER = getattr(config, "ENTRY_LIMIT_BUFFER", 0.005)
STOP_LIMIT_BUFFER = getattr(config, "STOP_LIMIT_BUFFER", 0.03)
FORCE_CLOSE_LIMIT_TIMEOUT = getattr(config, "FORCE_CLOSE_LIMIT_TIMEOUT", 120)
REENTRY_CUTOFF = getattr(config, "REENTRY_CUTOFF_TIME", "13:00")
TRAILING_ACTIVATION = getattr(config, "TRAILING_ACTIVATION_RETRACEMENT", 0.75)
TRAILING_PCT = getattr(config, "TRAILING_STOP_PCT", 0.01)

# ── Leveraged ETF detection ─────────────────────────────────────────
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
trading_client = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)


def log(msg):
    now = dt.datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {msg}")


# ── Position tracking ──────────────────────────────────────────────
@dataclass
class LivePosition:
    symbol: str
    entry_price: float
    shares: int
    stop_price: float
    activation_price: float      # 0.4.8: price where trailing activates (75% retracement)
    open_price: float
    trade_type: str = "first"    # "first" or "reentry"
    trailing_active: bool = False
    remaining_shares: int = 0
    highest: float = 0.0
    prev_high: float = 0.0       # for reentry
    reentry_target: float = 0.0  # for reentry
    entry_time: dt.datetime = None
    protective_order_id: str = None
    reached_150: bool = False    # reentry only: target reached flag

    def __post_init__(self):
        self.remaining_shares = self.shares
        self.highest = self.entry_price


# ── State export ────────────────────────────────────────────────────
def save_state(positions, candidates, daily_trades, daily_stopped,
               entry_checked, day_highs, accumulator, events_log):
    all_syms = set([c["symbol"] for c in candidates] + [p.symbol for p in positions])
    state = {
        "updated": dt.datetime.now().isoformat(),
        "version": "0.4.8",
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
                "activation_price": p.activation_price,
                "highest": p.highest, "trade_type": p.trade_type,
                "trailing_active": p.trailing_active,
                "open_price": p.open_price,
                "entry_time": p.entry_time.isoformat() if p.entry_time else None,
                "prev_high": p.prev_high, "reentry_target": p.reentry_target,
                "protective_order_id": p.protective_order_id,
            }
            for p in positions if p.remaining_shares > 0
        ],
        "entry_checked": list(entry_checked),
        "day_highs": {k: round(v, 4) for k, v in day_highs.items()},
        "bar_counts": {sym: accumulator.bar_count(sym) for sym in all_syms},
        "events": events_log[-50:],
    }
    with open("live_state.json", "w") as f:
        json.dump(state, f, indent=2)


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


# ── Data helpers ───────────────────────────────────────────────────
def get_snapshots(symbols):
    request = StockSnapshotRequest(symbol_or_symbols=symbols, feed=DataFeed.IEX)
    return data_client.get_stock_snapshot(request)


def get_prev_day_atr(symbol):
    today = dt.date.today()
    start = today - pd.Timedelta(days=30)
    request = StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
        start=start, end=today, adjustment=Adjustment.RAW, feed=DataFeed.IEX,
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
            start=yesterday, end=end, adjustment=Adjustment.RAW, feed=DataFeed.IEX,
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
    try:
        result = trading_client.close_position(symbol, cancel_orders=True)
        if result:
            log(f"FORCE SELL (close_position): {symbol} {qty} shares")
            return True
    except Exception as e:
        log(f"close_position failed for {symbol}: {e}")

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


# ── Entry detection ────────────────────────────────────────────────
def check_entry(symbol, open_price, accumulator):
    bars = accumulator.get_5min_bars(symbol)
    if len(bars) < 2:
        return 0, False
    for i in range(len(bars)):
        if bars[i]["low"] < open_price:
            pullback_price = bars[i]["low"]
            if not config.ENTRY_CONFIRMATION:
                return pullback_price, True
            if i + 1 < len(bars) and bars[i + 1]["low"] >= pullback_price:
                return pullback_price, True
            for j in range(i + 2, len(bars)):
                bar = bars[j]
                prev = bars[j - 1]
                if bar["low"] < open_price and prev["low"] >= bar["low"]:
                    if j + 1 < len(bars) and bars[j + 1]["low"] >= bar["low"]:
                        return bar["low"], True
            return pullback_price, True
    return 0, False


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


# ── Main trading loop ──────────────────────────────────────────────
def run_live():
    log("=" * 60)
    log("Stone 0.4.8 Live Paper Trading")
    log(f"Capital: ${config.INITIAL_CAPITAL:,.0f} | Max daily trades: {config.MAX_DAILY_TRADES}")
    log(f"Pure trailing: activate at {TRAILING_ACTIVATION:.0%} retracement, trail {TRAILING_PCT:.0%}")
    log(f"Entry buffer: +{ENTRY_LIMIT_BUFFER:.1%} | Stop-limit buffer: -{STOP_LIMIT_BUFFER:.1%}")
    log(f"Force-close timeout: {FORCE_CLOSE_LIMIT_TIMEOUT}s")
    log(f"Re-entry cutoff: {REENTRY_CUTOFF} EST | Leveraged ETF filter: ON")
    log("=" * 60)

    if not test_connectivity():
        log("Data connectivity failed. Cannot trade.")
        return

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

    while True:
        now = dt.datetime.now(tz=ZoneInfo("America/New_York"))
        if now.hour >= 9 and now.minute >= 30:
            break
        if now.hour >= 10:
            break
        log("Waiting for market open (9:30 AM EST)...")
        time.sleep(30)

    # ── Scan gaps ──
    log("Scanning for gap stocks...")
    candidates = scan_gaps()
    if not candidates:
        log("No gap stocks found today. Exiting.")
        return

    deployable = calc_position_size(capital)
    pos_per_stock = min(deployable, config.MAX_POSITION_SIZE)
    max_stocks = max(config.MAX_POSITIONS_PER_DAY, int(deployable / pos_per_stock))
    candidates = candidates[:max_stocks]

    log(f"Candidates: {[c['symbol'] for c in candidates]}")
    for c in candidates:
        log(f"  {c['symbol']}: gap +{c['gap_pct']:.1%}, open=${c['open_price']:.4f}")
        day_highs[c['symbol']] = c['open_price']

    # ── Main loop ──
    force_close_time = dt.time(int(config.FORCE_CLOSE_TIME[:2]), int(config.FORCE_CLOSE_TIME[3:]))
    cutoff_time = dt.time(10, 0)
    reentry_cutoff_time = dt.time(int(REENTRY_CUTOFF[:2]), int(REENTRY_CUTOFF[3:]))
    force_close_started = {}

    while True:
        now_est = dt.datetime.now(tz=ZoneInfo("America/New_York"))
        now_time = now_est.time()
        poll_count += 1

        # ── Reconcile with Alpaca positions (every 4th poll) ──
        if poll_count % 4 == 0:
            try:
                alpaca_positions = trading_client.get_all_positions()
                held_on_alpaca = {p.symbol: int(p.qty) for p in alpaca_positions}
                tracked_symbols = {p.symbol for p in positions if p.remaining_shares > 0}
                for sym, qty in held_on_alpaca.items():
                    if sym not in tracked_symbols and sym not in pending_buys:
                        log(f"RECONCILE: {sym} has {qty} shares on Alpaca but not tracked — force selling")
                        force_sell_position(sym, qty)
            except Exception as e:
                log(f"Reconcile check error: {e}")

        # ── Check pending buy fills ──
        for symbol in list(pending_buys.keys()):
            order_id, pos_data = pending_buys[symbol]
            if check_order_filled(order_id):
                log(f"BUY FILLED: {symbol} order {order_id}")
                pos = LivePosition(**pos_data)
                positions.append(pos)
                place_protective_stop(pos)
                events_log.append(f"{now_est.strftime('%H:%M:%S')} BUY FILLED {symbol} @ ${pos.entry_price:.4f}")
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
                daily_trades += 1
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
                    # 0.4.8: If trailing never activated, exit at breakeven
                    if pos.trade_type == "first" and not pos.trailing_active:
                        limit_price = round(pos.entry_price * 0.99, 2)
                        log(f"BREAKEVEN EXIT: {pos.symbol} @ ${limit_price:.2f} (trailing never activated)")
                    else:
                        snap = get_snapshots([pos.symbol]).get(pos.symbol)
                        bid_price = float(snap.latest_trade.price) if snap and snap.latest_trade else 0
                        if bid_price > 0:
                            limit_price = round(bid_price * 0.99, 2)
                        else:
                            limit_price = round(pos.entry_price * 0.99, 2)
                    place_sell_limit(pos.symbol, pos.remaining_shares, limit_price)
                    force_close_started[pos.symbol] = now_est
                    events_log.append(f"{now_est.strftime('%H:%M:%S')} FORCE CLOSE {pos.symbol} {pos.remaining_shares} @ ${limit_price:.2f}")
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
                                pos.remaining_shares = 0
                                pos.protective_order_id = None
                            else:
                                log(f"PULLBACK STOP SELL FAILED: {pos.symbol} — keeping tracking")
                    break

        if daily_stopped:
            _force_close_remaining(positions)
            positions = [p for p in positions if p.remaining_shares > 0]
            save_state(positions, candidates, daily_trades, daily_stopped,
                       entry_checked, day_highs, accumulator, events_log)
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
                if sold:
                    pos.remaining_shares = 0
                    daily_trades += 1
                    positions.remove(pos)
                    continue
                else:
                    log(f"STOP LOSS FORCE SELL FAILED: {pos.symbol} — will retry next poll")

            if pos.trade_type == "first":
                # ── 0.4.8: Pure trailing stop ──
                # Check activation: price reaches 75% retracement
                if not pos.trailing_active and cur_price >= pos.activation_price:
                    pos.trailing_active = True
                    log(f"TRAILING ACTIVATED: {pos.symbol} @ ${cur_price:.4f} (activation=${pos.activation_price:.4f})")
                    events_log.append(f"{now_est.strftime('%H:%M:%S')} TRAILING ACTIVATED {pos.symbol} @ ${cur_price:.4f}")
                    # Replace stop-limit with trailing stop
                    replace_with_trailing_stop(pos, TRAILING_PCT)

                # Trailing stop (polled fallback after activation)
                if pos.trailing_active and pos.remaining_shares > 0:
                    tsp = round(pos.highest * (1 - TRAILING_PCT), 2)
                    tsp = max(tsp, pos.entry_price)  # breakeven protection
                    if cur_price <= tsp:
                        log(f"TRAILING STOP (polled): {pos.symbol} @ ${tsp:.4f} (high=${pos.highest:.4f}, trail={TRAILING_PCT:.0%})")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} TRAILING STOP {pos.symbol} @ ${tsp:.4f}")
                        if pos.protective_order_id:
                            cancel_order(pos.protective_order_id)
                            pos.protective_order_id = None
                        sold = force_sell_position(pos.symbol, pos.remaining_shares)
                        if sold:
                            pos.remaining_shares = 0
                            daily_trades += 1
                            positions.remove(pos)
                            continue
                        else:
                            log(f"TRAILING STOP FORCE SELL FAILED: {pos.symbol} — will retry next poll")

            # ── Re-entry profit targets (same as 0.4.5) ──
            elif pos.trade_type == "reentry":
                need_replace_protective = False

                if not pos.reached_150 and pos.highest >= pos.reentry_target:
                    pos.reached_150 = True
                    n = pos.remaining_shares // 3
                    if n > 0:
                        sell_price = round(pos.reentry_target * 0.997, 2)
                        log(f"RE-ENTRY TARGET: {pos.symbol} selling {n} @ ${sell_price:.4f}")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} RE-ENTRY TARGET {pos.symbol} sell {n} @ ${sell_price:.4f}")
                        if pos.protective_order_id:
                            cancel_order(pos.protective_order_id)
                            pos.protective_order_id = None
                        place_sell_limit(pos.symbol, n, sell_price)
                        pos.remaining_shares -= n
                        need_replace_protective = True

                if need_replace_protective and pos.remaining_shares > 0:
                    replace_with_trailing_stop(pos, config.REENTRY_TRAILING_PCT)

                if pos.reached_150 and pos.remaining_shares > 0:
                    tsp = round(pos.highest * (1 - config.REENTRY_TRAILING_PCT), 2)
                    tsp = max(tsp, pos.entry_price)
                    if cur_price <= tsp:
                        log(f"RE-ENTRY TRAILING (polled): {pos.symbol} @ ${tsp:.4f} (high=${pos.highest:.4f})")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} RE-ENTRY TRAILING {pos.symbol} @ ${tsp:.4f}")
                        if pos.protective_order_id:
                            cancel_order(pos.protective_order_id)
                            pos.protective_order_id = None
                        sold = force_sell_position(pos.symbol, pos.remaining_shares)
                        if sold:
                            pos.remaining_shares = 0
                            daily_trades += 1
                            positions.remove(pos)
                            continue
                        else:
                            log(f"RE-ENTRY TRAILING FORCE SELL FAILED: {pos.symbol} — will retry next poll")

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

                if is_leveraged_etf(symbol):
                    log(f"  {symbol}: leveraged ETF, skipping entry")
                    entry_checked.add(symbol)
                    continue

                entry_price, confirmed = check_entry(symbol, cand["open_price"], accumulator)
                if not confirmed or entry_price <= 0:
                    continue

                bars_5m = accumulator.get_5min_bars(symbol)
                if len(bars_5m) >= 2:
                    atr = calc_atr(bars_5m, period=14)
                else:
                    atr = get_prev_day_atr(symbol)

                stop = calc_stop_price(entry_price, atr)
                # 0.4.8: activation price = 75% retracement from entry to open
                activation_price = round(entry_price + TRAILING_ACTIVATION * (cand["open_price"] - entry_price), 2)

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
                        "stop_price": stop, "activation_price": activation_price,
                        "open_price": cand["open_price"],
                        "entry_time": now_est,
                    }
                    pending_buys[symbol] = (order.id, pos_data)
                    entry_checked.add(symbol)
                    log(f"BUY PENDING {symbol}: entry=${entry_price:.4f}, limit=${limit_price:.4f}, "
                        f"stop=${stop:.4f}, activation=${activation_price:.4f}, shares={shares}")
                    events_log.append(f"{now_est.strftime('%H:%M:%S')} BUY PENDING {symbol} @ ${limit_price:.4f}")

        # ── Check re-entry ──
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

                stop_price = round(entry_price * (1 - config.REENTRY_STOP_PCT), 2)
                target = round(entry_price + config.REENTRY_PROFIT_RETRACEMENT * (prev_high - entry_price), 2)

                pos_size = min(calc_position_size(capital), config.MAX_POSITION_SIZE)
                shares = int(pos_size / entry_price)
                if shares <= 0:
                    reentry_checked.add(symbol)
                    continue

                limit_price = round(entry_price * (1 + ENTRY_LIMIT_BUFFER), 2)
                order = place_buy_limit(symbol, shares, limit_price)
                if order:
                    pos = LivePosition(
                        symbol=symbol, entry_price=entry_price, shares=shares,
                        stop_price=stop_price, activation_price=0,
                        open_price=cand["open_price"],
                        trade_type="reentry", prev_high=prev_high,
                        reentry_target=target, entry_time=now_est,
                    )
                    positions.append(pos)
                    place_protective_stop(pos)
                    reentry_checked.add(symbol)
                    log(f"RE-ENTERED {symbol}: entry=${entry_price:.4f}, limit=${limit_price:.4f}, "
                        f"stop=${stop_price:.4f}, target=${target:.4f}, prev_high=${prev_high:.4f}, shares={shares}")
                    events_log.append(f"{now_est.strftime('%H:%M:%S')} RE-ENTERED {symbol} @ ${entry_price:.4f}")
        elif now_time >= reentry_cutoff_time and poll_count == 1:
            log(f"Re-entry cutoff reached ({REENTRY_CUTOFF} EST). No more re-entries.")

        # ── Cleanup fully exited positions ──
        positions = [p for p in positions if p.remaining_shares > 0]

        # ── Save state ──
        save_state(positions, candidates, daily_trades, daily_stopped,
                   entry_checked, day_highs, accumulator, events_log)

        # ── Status log ──
        if poll_count % 4 == 0 and positions:
            for pos in positions:
                snap = snaps.get(pos.symbol)
                cur = float(snap.latest_trade.price) if snap and snap.latest_trade else 0
                pnl = (cur - pos.entry_price) * pos.remaining_shares if cur > 0 else 0
                trail_status = f"ACTIVE(trail={TRAILING_PCT:.0%})" if pos.trailing_active else f"PENDING(act=${pos.activation_price:.4f})"
                protective = f", prot={pos.protective_order_id[:8] if pos.protective_order_id else 'none'}"
                log(f"  {pos.symbol}({pos.trade_type}): {pos.remaining_shares} shares, "
                    f"entry=${pos.entry_price:.4f} cur=${cur:.4f} high=${pos.highest:.4f} pnl=${pnl:.2f} "
                    f"trailing={trail_status}{protective}")

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
