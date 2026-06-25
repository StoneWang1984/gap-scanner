"""Stone 0.4 Live Paper Trading — runs during market hours.

Uses Alpaca snapshot API for real-time data (works with free IEX feed).
Accumulates minute bars from snapshots into 5-min bars for entry/ATR/re-entry.
"""

import json
import time
import datetime as dt
from collections import defaultdict
from dataclasses import dataclass

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
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

    def __post_init__(self):
        self.remaining_shares = self.shares
        self.highest = self.entry_price


# ── State export for Streamlit dashboard ────────────────────────────
def save_state(positions, candidates, daily_trades, daily_stopped,
               entry_checked, day_highs, accumulator, events_log):
    """Write current trading state to live_state.json for the web dashboard."""
    all_syms = set([c["symbol"] for c in candidates] + [p.symbol for p in positions])
    state = {
        "updated": dt.datetime.now().isoformat(),
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
            }
            for p in positions if p.remaining_shares > 0
        ],
        "entry_checked": list(entry_checked),
        "day_highs": {k: round(v, 4) for k, v in day_highs.items()},
        "bar_counts": {sym: accumulator.bar_count(sym) for sym in all_syms},
        "events": events_log[-50:],  # last 50 events
    }
    with open("live_state.json", "w") as f:
        json.dump(state, f, indent=2)


# ── 5-min bar accumulator from minute-bar snapshots ────────────────
class BarAccumulator:
    """Build 5-min bars from minute bar snapshots for entry/ATR/re-entry logic."""

    def __init__(self):
        self._seen_ts = defaultdict(set)
        self._minute_bars = defaultdict(list)
        self._5min_cache = defaultdict(list)

    def add_bar(self, symbol, bar):
        """Add a minute bar (from snapshot.minute_bar). Returns True if new."""
        ts = bar.timestamp
        if hasattr(ts, "to_pydatetime"):
            ts = ts.to_pydatetime()
        ts_key = ts.replace(second=0, microsecond=0)
        if ts_key in self._seen_ts[symbol]:
            return False
        self._seen_ts[symbol].add(ts_key)
        self._minute_bars[symbol].append({
            "timestamp": ts_key,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": int(bar.volume),
        })
        self._rebuild_5min(symbol)
        return True

    def _rebuild_5min(self, symbol):
        """Aggregate minute bars into completed 5-min bars."""
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
                    "timestamp": bucket_start,
                    "open": m["open"],
                    "high": m["high"],
                    "low": m["low"],
                    "close": m["close"],
                    "volume": m["volume"],
                    "count": 1,
                }
            else:
                b = buckets[bucket_start]
                b["high"] = max(b["high"], m["high"])
                b["low"] = min(b["low"], m["low"])
                b["close"] = m["close"]
                b["volume"] += m["volume"]
                b["count"] += 1
        # Completed 5-min bars: all but the last bucket (which may still be forming)
        sorted_ts = sorted(buckets)
        completed = []
        for i, ts in enumerate(sorted_ts):
            if i < len(sorted_ts) - 1:
                b = buckets[ts]
                completed.append({
                    "timestamp": b["timestamp"],
                    "open": b["open"],
                    "high": b["high"],
                    "low": b["low"],
                    "close": b["close"],
                    "volume": b["volume"],
                })
        self._5min_cache[symbol] = completed

    def get_5min_bars(self, symbol):
        return list(self._5min_cache.get(symbol, []))

    def bar_count(self, symbol):
        return len(self._5min_cache.get(symbol, []))


# ── Data helpers ───────────────────────────────────────────────────
def get_snapshots(symbols):
    """Get current snapshots (works with IEX for current day)."""
    request = StockSnapshotRequest(symbol_or_symbols=symbols, feed=DataFeed.IEX)
    return data_client.get_stock_snapshot(request)


def get_prev_day_atr(symbol):
    """Get ATR from previous daily bars for stop-loss calculation."""
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
    """Scan for gap-up stocks at market open using previous day + today's open."""
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
def place_buy(symbol, shares, price=None):
    try:
        if price:
            order = trading_client.submit_order(LimitOrderRequest(
                symbol=symbol, qty=shares, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY, limit_price=round(price, 2),
            ))
        else:
            order = trading_client.submit_order(MarketOrderRequest(
                symbol=symbol, qty=shares, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            ))
        log(f"BUY {symbol} {shares} shares @ {'${:.2f}'.format(price) if price else 'MARKET'} -> order {order.id}")
        return order
    except Exception as e:
        log(f"BUY FAILED {symbol}: {e}")
        return None


def place_sell(symbol, shares, price=None):
    try:
        if price:
            order = trading_client.submit_order(LimitOrderRequest(
                symbol=symbol, qty=shares, side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY, limit_price=round(price, 2),
            ))
        else:
            order = trading_client.submit_order(MarketOrderRequest(
                symbol=symbol, qty=shares, side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            ))
        log(f"SELL {symbol} {shares} shares @ {'${:.2f}'.format(price) if price else 'MARKET'} -> order {order.id}")
        return order
    except Exception as e:
        log(f"SELL FAILED {symbol}: {e}")
        return None


def cancel_all_orders():
    try:
        trading_client.cancel_orders()
    except:
        pass


def close_all_positions():
    try:
        positions = trading_client.get_all_positions()
        for pos in positions:
            log(f"EOD CLOSE: selling {pos.qty} {pos.symbol}")
            trading_client.close_position(pos.symbol)
    except Exception as e:
        log(f"Close positions error: {e}")


# ── Entry detection using 5-min bars ───────────────────────────────
def check_entry(symbol, open_price, accumulator):
    """Check for pullback entry using accumulated 5-min bars."""
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


# ── Test data connectivity ─────────────────────────────────────────
def test_connectivity():
    """Test that snapshot API works for real-time data."""
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
    log("Stone 0.4 Live Paper Trading")
    log(f"Capital: ${config.INITIAL_CAPITAL:,.0f} | Max daily trades: {config.MAX_DAILY_TRADES}")
    log("=" * 60)

    # Test connectivity first
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

    # Wait for market open
    while True:
        now = dt.datetime.now(tz=dt.timezone(dt.timedelta(hours=-4)))
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

    while True:
        now_est = dt.datetime.now(tz=dt.timezone(dt.timedelta(hours=-4)))
        now_time = now_est.time()
        poll_count += 1

        # Force close
        if now_time >= force_close_time:
            log("Force close time reached!")
            cancel_all_orders()
            for pos in positions:
                if pos.remaining_shares > 0:
                    place_sell(pos.symbol, pos.remaining_shares)
            close_all_positions()
            break

        # ── Collect snapshot data ──
        all_symbols = list(set(
            [c['symbol'] for c in candidates] +
            [p.symbol for p in positions]
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

        # ── Accumulate minute bars into 5-min bars ──
        for symbol in all_symbols:
            snap = snaps.get(symbol)
            if snap and snap.minute_bar:
                accumulator.add_bar(symbol, snap.minute_bar)

        # ── Track day highs for pullback stop ──
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
                            place_sell(pos.symbol, pos.remaining_shares)
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

            # Update highest since entry (only from current price, avoids pre-entry spikes)
            if cur_price > pos.highest:
                pos.highest = cur_price

            # Stop loss
            if cur_price <= pos.stop_price:
                log(f"STOP LOSS: {pos.symbol} @ ${pos.stop_price:.4f} (cur=${cur_price:.4f})")
                events_log.append(f"{now_est.strftime('%H:%M:%S')} STOP LOSS {pos.symbol} @ ${pos.stop_price:.4f}")
                place_sell(pos.symbol, pos.remaining_shares)
                pos.remaining_shares = 0
                daily_trades += 1
                positions.remove(pos)
                continue

            # ── First trade profit targets ──
            if pos.trade_type == "first":
                if not pos.reached_150 and pos.highest >= pos.target_150:
                    pos.reached_150 = pos.reached_1125 = pos.reached_75 = True
                    n75 = pos.shares // 4
                    n1125 = (pos.shares - n75) // 3
                    n150 = (pos.shares - n75 - n1125) // 3
                    total_sell = n75 + n1125 + n150
                    if total_sell > 0 and total_sell <= pos.remaining_shares:
                        log(f"150% TARGET: {pos.symbol} selling {total_sell} @ ${pos.target_150:.4f}")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} 150% TARGET {pos.symbol} sell {total_sell} @ ${pos.target_150:.4f}")
                        place_sell(pos.symbol, total_sell, pos.target_150)
                        pos.sold_75_shares += n75
                        pos.sold_1125_shares += n1125
                        pos.sold_150_shares += n150
                        pos.remaining_shares -= total_sell

                elif not pos.reached_1125 and pos.highest >= pos.target_1125:
                    pos.reached_1125 = pos.reached_75 = True
                    n75 = pos.shares // 4
                    n1125 = (pos.shares - n75) // 3
                    total_sell = n75 + n1125
                    if total_sell > 0 and total_sell <= pos.remaining_shares:
                        log(f"112.5% TARGET: {pos.symbol} selling {total_sell} @ ${pos.target_1125:.4f}")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} 112.5% TARGET {pos.symbol} sell {total_sell} @ ${pos.target_1125:.4f}")
                        place_sell(pos.symbol, total_sell, pos.target_1125)
                        pos.sold_75_shares += n75
                        pos.sold_1125_shares += n1125
                        pos.remaining_shares -= total_sell

                elif not pos.reached_75 and pos.highest >= pos.target_75:
                    pos.reached_75 = True
                    n75 = pos.shares // 4
                    if n75 > 0 and n75 <= pos.remaining_shares:
                        log(f"75% TARGET: {pos.symbol} selling {n75} @ ${pos.target_75:.4f}")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} 75% TARGET {pos.symbol} sell {n75} @ ${pos.target_75:.4f}")
                        place_sell(pos.symbol, n75, pos.target_75)
                        pos.sold_75_shares = n75
                        pos.remaining_shares -= n75

                # Trailing stop
                if pos.reached_75 and pos.remaining_shares > 0:
                    if pos.reached_150:
                        pct = config.TRAILING_STOP_PCT_150
                    elif pos.reached_1125:
                        pct = config.TRAILING_STOP_PCT_1125
                    else:
                        pct = config.TRAILING_STOP_PCT_75
                    tsp = round(pos.highest * (1 - pct), 4)
                    tsp = max(tsp, pos.entry_price)
                    if cur_price <= tsp:
                        tier = "150%" if pos.reached_150 else "112.5%" if pos.reached_1125 else "75%"
                        log(f"TRAILING STOP({tier}): {pos.symbol} @ ${tsp:.4f} (high=${pos.highest:.4f})")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} TRAILING STOP({tier}) {pos.symbol} @ ${tsp:.4f}")
                        place_sell(pos.symbol, pos.remaining_shares)
                        pos.remaining_shares = 0
                        daily_trades += 1
                        positions.remove(pos)
                        continue

            # ── Re-entry profit targets ──
            elif pos.trade_type == "reentry":
                if not pos.reached_150 and pos.highest >= pos.reentry_target:
                    pos.reached_150 = True
                    n = pos.remaining_shares // 3
                    if n > 0:
                        log(f"RE-ENTRY TARGET: {pos.symbol} selling {n} @ ${pos.reentry_target:.4f}")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} RE-ENTRY TARGET {pos.symbol} sell {n} @ ${pos.reentry_target:.4f}")
                        place_sell(pos.symbol, n, pos.reentry_target)
                        pos.remaining_shares -= n

                if pos.reached_150 and pos.remaining_shares > 0:
                    tsp = round(pos.highest * (1 - config.REENTRY_TRAILING_PCT), 4)
                    tsp = max(tsp, pos.entry_price)
                    if cur_price <= tsp:
                        log(f"RE-ENTRY TRAILING: {pos.symbol} @ ${tsp:.4f} (high=${pos.highest:.4f})")
                        events_log.append(f"{now_est.strftime('%H:%M:%S')} RE-ENTRY TRAILING {pos.symbol} @ ${tsp:.4f}")
                        place_sell(pos.symbol, pos.remaining_shares)
                        pos.remaining_shares = 0
                        daily_trades += 1
                        positions.remove(pos)
                        continue

        # ── Check entries for candidates ──
        if now_time < cutoff_time and daily_trades < config.MAX_DAILY_TRADES and not daily_stopped:
            for cand in candidates:
                symbol = cand["symbol"]
                if symbol in entry_checked:
                    continue
                if symbol in [p.symbol for p in positions]:
                    continue

                entry_price, confirmed = check_entry(symbol, cand["open_price"], accumulator)
                if not confirmed or entry_price <= 0:
                    continue

                # ATR from accumulated 5-min bars or previous daily bars
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

                order = place_buy(symbol, shares, entry_price)
                if order:
                    pos = LivePosition(
                        symbol=symbol, entry_price=entry_price, shares=shares,
                        stop_price=stop, target_75=target_75, target_1125=target_1125,
                        target_150=target_150, open_price=cand["open_price"],
                        entry_time=now_est,
                    )
                    positions.append(pos)
                    entry_checked.add(symbol)
                    log(f"ENTERED {symbol}: entry=${entry_price:.4f}, stop=${stop:.4f}, "
                        f"target75=${target_75:.4f}, target150=${target_150:.4f}, shares={shares}")
                    events_log.append(f"{now_est.strftime('%H:%M:%S')} ENTERED {symbol} @ ${entry_price:.4f} stop=${stop:.4f}")

        # ── Check re-entry for exited positions ──
        if daily_trades < config.MAX_DAILY_TRADES and not daily_stopped:
            exited_symbols = entry_checked - {p.symbol for p in positions}
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

                stop_price = round(entry_price * (1 - config.REENTRY_STOP_PCT), 4)
                target = round(entry_price + config.REENTRY_PROFIT_RETRACEMENT * (prev_high - entry_price), 4)

                pos_size = min(calc_position_size(config.INITIAL_CAPITAL), config.MAX_POSITION_SIZE)
                shares = int(pos_size / entry_price)
                if shares <= 0:
                    reentry_checked.add(symbol)
                    continue

                order = place_buy(symbol, shares, entry_price)
                if order:
                    pos = LivePosition(
                        symbol=symbol, entry_price=entry_price, shares=shares,
                        stop_price=stop_price, target_75=0, target_1125=0,
                        target_150=0, open_price=cand["open_price"],
                        trade_type="reentry", prev_high=prev_high,
                        reentry_target=target, entry_time=now_est,
                    )
                    positions.append(pos)
                    reentry_checked.add(symbol)
                    log(f"RE-ENTERED {symbol}: entry=${entry_price:.4f}, stop=${stop_price:.4f}, "
                        f"target=${target:.4f}, prev_high=${prev_high:.4f}, shares={shares}")
                    events_log.append(f"{now_est.strftime('%H:%M:%S')} RE-ENTERED {symbol} @ ${entry_price:.4f}")

        # ── Cleanup fully exited positions ──
        positions = [p for p in positions if p.remaining_shares > 0]

        # ── Save state for web dashboard ──
        save_state(positions, candidates, daily_trades, daily_stopped,
                   entry_checked, day_highs, accumulator, events_log)

        # ── Status log (every 4th poll) ──
        if poll_count % 4 == 0 and positions:
            for pos in positions:
                snap = snaps.get(pos.symbol)
                cur = float(snap.latest_trade.price) if snap and snap.latest_trade else 0
                pnl = (cur - pos.entry_price) * pos.remaining_shares if cur > 0 else 0
                log(f"  {pos.symbol}({pos.trade_type}): {pos.remaining_shares} shares, "
                    f"entry=${pos.entry_price:.4f} cur=${cur:.4f} pnl=${pnl:.2f}")

        # Faster polling during first 15 min after open for quicker entry detection
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
    except:
        pass
    log(f"Daily trades: {daily_trades}")
    log("=" * 60)

    # Final state save with equity
    events_log.append(f"EOD equity=${equity:,.2f} trades={daily_trades}")
    save_state(positions, candidates, daily_trades, daily_stopped,
               entry_checked, day_highs, accumulator, events_log)


if __name__ == "__main__":
    run_live()
