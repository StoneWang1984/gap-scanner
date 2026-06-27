"""Stone 0.4.4 Live Paper Trading — native Alpaca orders + slippage-aware.

Changes over 0.4:
- Entry: limit order with 0.5% buffer above pullback (improve fill rate)
- Stop loss: native stop-limit order (server-side, no polling delay)
- Trailing stop: native trailing stop order (server-side real-time tracking)
- Target sells: limit orders with 0.3% buffer below target (improve fill rate)
- Force close: limit order first, market fallback after timeout
- Protective orders auto-replaced when partial sells occur
"""

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
    calc_atr, calc_stop_price, calc_price_at_retracement, calc_position_size,
    find_reentry_point,
)

# ── 0.4.4 Parameters ────────────────────────────────────────────────
ENTRY_LIMIT_BUFFER = getattr(config, "ENTRY_LIMIT_BUFFER", 0.005)
STOP_LIMIT_BUFFER = getattr(config, "STOP_LIMIT_BUFFER", 0.03)
FORCE_CLOSE_LIMIT_TIMEOUT = getattr(config, "FORCE_CLOSE_LIMIT_TIMEOUT", 120)
TARGET_LIMIT_BUFFER = 0.003  # sell 0.3% below target for better fill rate

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
    protective_order_id: str = None  # stop-limit or trailing stop order ID

    def __post_init__(self):
        self.remaining_shares = self.shares
        self.highest = self.entry_price


# ── State export ────────────────────────────────────────────────────
def save_state(positions, candidates, daily_trades, daily_stopped,
               entry_checked, day_highs, accumulator, events_log):
    all_syms = set([c["symbol"] for c in candidates] + [p.symbol for p in positions])
    state = {
        "updated": dt.datetime.now().isoformat(),
        "version": "0.4.4",
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
    """Buy with limit order (with ENTRY_LIMIT_BUFFER added by caller)."""
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
    """Sell with limit order (target sells, with small buffer)."""
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
    """Sell with market order (fallback when limit didn't fill)."""
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
    """Place native stop-limit sell order (server-side stop loss)."""
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
    """Place native trailing stop sell order (server-side trailing)."""
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


def check_order_filled(order_id) -> bool:
    """Check if an order has been filled."""
    try:
        order = trading_client.get_order_by_id(order_id)
        return order.status == QueryOrderStatus.FILLED
    except Exception:
        return False


# ── Protective order management ────────────────────────────────────

def place_protective_stop(pos: LivePosition) -> str | None:
    """Place stop-limit protective order after entry."""
    limit_price = round(pos.stop_price * (1 - STOP_LIMIT_BUFFER), 2)
    order = place_stop_limit_sell(pos.symbol, pos.remaining_shares, pos.stop_price, limit_price)
    if order:
        pos.protective_order_id = order.id
        return order.id
    return None


def replace_with_trailing_stop(pos: LivePosition, trail_pct: float) -> str | None:
    """Cancel current protective order and place trailing stop."""
    if pos.protective_order_id:
        cancel_order(pos.protective_order_id)
        pos.protective_order_id = None
    order = place_trailing_stop_sell(pos.symbol, pos.remaining_shares, trail_pct * 100)
    if order:
        pos.protective_order_id = order.id
        return order.id
    # Fallback: re-place stop-limit if trailing stop fails
    log(f"Trailing stop failed for {pos.symbol}, falling back to stop-limit")
    return place_protective_stop(pos)


def replace_stop_for_remaining(pos: LivePosition) -> str | None:
    """Cancel current protective order and place new one for remaining shares."""
    if pos.protective_order_id:
        cancel_order(pos.protective_order_id)
        pos.protective_order_id = None

    if pos.remaining_shares <= 0:
        return None

    # Determine if we should use trailing stop or stop-limit
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

    # Default: stop-limit for remaining
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
    log("Stone 0.4.4 Live Paper Trading")
    log(f"Capital: ${config.INITIAL_CAPITAL:,.0f} | Max daily trades: {config.MAX_DAILY_TRADES}")
    log(f"Entry buffer: +{ENTRY_LIMIT_BUFFER:.1%} | Stop-limit buffer: -{STOP_LIMIT_BUFFER:.1%}")
    log(f"Target buffer: -{TARGET_LIMIT_BUFFER:.1%} | Force-close timeout: {FORCE_CLOSE_LIMIT_TIMEOUT}s")
    log("=" * 60)

    if not test_connectivity():
        log("Data connectivity failed. Cannot trade.")
        return

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
    pending_buys = {}  # symbol -> (order_id, position_data)

    # Wait for market open
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

    deployable = calc_position_size(config.INITIAL_CAPITAL)
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
    force_close_started = {}  # symbol -> timestamp when limit was placed

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
                # Place protective stop-limit immediately
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
                    # 0.4.4: try limit first, then market
                    snap = get_snapshots([pos.symbol]).get(pos.symbol)
                    bid_price = float(snap.latest_trade.price) if snap and snap.latest_trade else 0
                    if bid_price > 0:
                        limit_price = round(bid_price * 0.99, 2)  # 1% below current for quick fill
                        place_sell_limit(pos.symbol, pos.remaining_shares, limit_price)
                        force_close_started[pos.symbol] = now_est
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} FORCE CLOSE LIMIT {pos.symbol} {pos.remaining_shares} @ ${limit_price:.2f}")
                    else:
                        place_sell_market(pos.symbol, pos.remaining_shares)
                        pos.remaining_shares = 0
            # Wait for force-close limit orders to fill or timeout
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
                            if pos.protective_order_id:
                                cancel_order(pos.protective_order_id)
                            place_sell_market(pos.symbol, pos.remaining_shares)
                            pos.remaining_shares = 0
                    break

        if daily_stopped:
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

            # ── Stop loss (polled fallback — native stop-limit is primary) ──
            if cur_price <= pos.stop_price:
                log(f"STOP LOSS (polled): {pos.symbol} @ ${pos.stop_price:.4f} (cur=${cur_price:.4f})")
                events_log.append(f"{now_est.strftime('%H:%M:%S')} STOP LOSS {pos.symbol} @ ${pos.stop_price:.4f}")
                if pos.protective_order_id:
                    cancel_order(pos.protective_order_id)
                place_sell_market(pos.symbol, pos.remaining_shares)
                pos.remaining_shares = 0
                pos.protective_order_id = None
                daily_trades += 1
                positions.remove(pos)
                continue

            # ── First trade profit targets ──
            if pos.trade_type == "first":
                # Cancel and replace protective order logic after target sells
                need_replace_protective = False

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
                        if pos.protective_order_id:
                            cancel_order(pos.protective_order_id)
                            pos.protective_order_id = None
                        place_sell_limit(pos.symbol, n75, sell_price)
                        pos.sold_75_shares = n75
                        pos.remaining_shares -= n75
                        need_replace_protective = True

                # Replace protective order after partial sell
                if need_replace_protective and pos.remaining_shares > 0:
                    replace_stop_for_remaining(pos)

                # ── Trailing stop (polled fallback — native trailing stop is primary) ──
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
                        place_sell_market(pos.symbol, pos.remaining_shares)
                        pos.remaining_shares = 0
                        pos.protective_order_id = None
                        daily_trades += 1
                        positions.remove(pos)
                        continue

            # ── Re-entry profit targets ──
            elif pos.trade_type == "reentry":
                need_replace_protective = False

                if not pos.reached_150 and pos.highest >= pos.reentry_target:
                    pos.reached_150 = True
                    n = pos.remaining_shares // 3
                    if n > 0:
                        sell_price = round(pos.reentry_target * (1 - TARGET_LIMIT_BUFFER), 2)
                        log(f"RE-ENTRY TARGET: {pos.symbol} selling {n} @ ${sell_price:.4f}")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} RE-ENTRY TARGET {pos.symbol} sell {n} @ ${sell_price:.4f}")
                        if pos.protective_order_id:
                            cancel_order(pos.protective_order_id)
                            pos.protective_order_id = None
                        place_sell_limit(pos.symbol, n, sell_price)
                        pos.remaining_shares -= n
                        need_replace_protective = True

                if need_replace_protective and pos.remaining_shares > 0:
                    replace_stop_for_remaining(pos)

                if pos.reached_150 and pos.remaining_shares > 0:
                    tsp = round(pos.highest * (1 - config.REENTRY_TRAILING_PCT), 2)
                    tsp = max(tsp, pos.entry_price)
                    if cur_price <= tsp:
                        log(f"RE-ENTRY TRAILING (polled): {pos.symbol} @ ${tsp:.4f} (high=${pos.highest:.4f})")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} RE-ENTRY TRAILING {pos.symbol} @ ${tsp:.4f}")
                        if pos.protective_order_id:
                            cancel_order(pos.protective_order_id)
                        place_sell_market(pos.symbol, pos.remaining_shares)
                        pos.remaining_shares = 0
                        pos.protective_order_id = None
                        daily_trades += 1
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

                entry_price, confirmed = check_entry(symbol, cand["open_price"], accumulator)
                if not confirmed or entry_price <= 0:
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

                pos_size = min(calc_position_size(config.INITIAL_CAPITAL), config.MAX_POSITION_SIZE)
                shares = int(pos_size / entry_price)
                if shares <= 0:
                    entry_checked.add(symbol)
                    continue

                # 0.4.4: entry limit with buffer for better fill rate
                limit_price = round(entry_price * (1 + ENTRY_LIMIT_BUFFER), 2)
                order = place_buy_limit(symbol, shares, limit_price)
                if order:
                    pos_data = {
                        "symbol": symbol, "entry_price": entry_price, "shares": shares,
                        "stop_price": stop, "target_75": target_75, "target_1125": target_1125,
                        "target_150": target_150, "open_price": cand["open_price"],
                        "entry_time": now_est,
                    }
                    pending_buys[symbol] = (order.id, pos_data)
                    entry_checked.add(symbol)
                    log(f"BUY PENDING {symbol}: entry=${entry_price:.4f}, limit=${limit_price:.4f}, "
                        f"stop=${stop:.4f}, target75=${target_75:.4f}, target150=${target_150:.4f}, shares={shares}")
                    events_log.append(f"{now_est.strftime('%H:%M:%S')} BUY PENDING {symbol} @ ${limit_price:.4f}")

        # ── Check re-entry ──
        if daily_trades < config.MAX_DAILY_TRADES and not daily_stopped:
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

                pos_size = min(calc_position_size(config.INITIAL_CAPITAL), config.MAX_POSITION_SIZE)
                shares = int(pos_size / entry_price)
                if shares <= 0:
                    reentry_checked.add(symbol)
                    continue

                # 0.4.4: re-entry limit with buffer
                limit_price = round(entry_price * (1 + ENTRY_LIMIT_BUFFER), 2)
                order = place_buy_limit(symbol, shares, limit_price)
                if order:
                    pos = LivePosition(
                        symbol=symbol, entry_price=entry_price, shares=shares,
                        stop_price=stop_price, target_75=0, target_1125=0,
                        target_150=0, open_price=cand["open_price"],
                        trade_type="reentry", prev_high=prev_high,
                        reentry_target=target, entry_time=now_est,
                    )
                    positions.append(pos)
                    # Place protective stop immediately
                    place_protective_stop(pos)
                    reentry_checked.add(symbol)
                    log(f"RE-ENTERED {symbol}: entry=${entry_price:.4f}, limit=${limit_price:.4f}, "
                        f"stop=${stop_price:.4f}, target=${target:.4f}, prev_high=${prev_high:.4f}, shares={shares}")
                    events_log.append(f"{now_est.strftime('%H:%M:%S')} RE-ENTERED {symbol} @ ${entry_price:.4f}")

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
                protective = f", prot={pos.protective_order_id[:8] if pos.protective_order_id else 'none'}"
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


def _wait_force_close(force_close_started: dict, positions: list[LivePosition]):
    """Wait for force-close limit orders, convert to market after timeout."""
    deadline = dt.datetime.now() + dt.timedelta(seconds=FORCE_CLOSE_LIMIT_TIMEOUT)
    while dt.datetime.now() < deadline and force_close_started:
        time.sleep(5)
        for symbol in list(force_close_started.keys()):
            # Check if all positions for this symbol are gone
            still_holding = any(p.symbol == symbol and p.remaining_shares > 0 for p in positions)
            if not still_holding:
                del force_close_started[symbol]
                continue
            # Check if any open sell orders for this symbol are filled
            try:
                open_orders = trading_client.get_orders(filter={
                    "status": "open",
                    "symbols": symbol,
                })
                sell_orders = [o for o in open_orders if o.side == OrderSide.SELL]
                if not sell_orders:
                    # Order filled or cancelled
                    del force_close_started[symbol]
            except Exception:
                pass

    # Timeout: convert remaining to market orders
    for symbol in list(force_close_started.keys()):
        cancel_all_orders()
        for pos in positions:
            if pos.symbol == symbol and pos.remaining_shares > 0:
                log(f"FORCE CLOSE MARKET FALLBACK: {symbol} {pos.remaining_shares} shares")
                place_sell_market(pos.symbol, pos.remaining_shares)
                pos.remaining_shares = 0
        del force_close_started[symbol]


if __name__ == "__main__":
    run_live()
