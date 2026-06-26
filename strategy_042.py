"""Gap pullback strategy — Stone 0.4.2: five optimizations over 0.4.

1. Stop loss: 5% fixed for low-price stocks (<$1), early stop in first 3 bars
2. Re-entry quality: require 3%+ pullback, early exit if no gain in 2 bars
3. Entry quality: volume ratio filter, prior gain filter
4. Late-day management: tighten trailing after 14:00, close if 75% not reached by 14:30
5. Three-tier: 1/3@75% + 1/3@100% + 1/3@150%
"""

import pandas as pd
import config
from strategy import (
    calc_atr, calc_position_size, calc_price_at_retracement,
    TradePlan, TradeResult,
)


# ── 1. Stop loss optimization ──────────────────────────────────

def calc_stop_price_042(pullback: float, atr: float, open_price: float) -> float:
    """Stop price for 0.4.2: 5% fixed for low-price stocks, ATR-based otherwise."""
    if open_price < config.LOW_PRICE_THRESHOLD:
        return round(pullback * (1 - config.LOW_PRICE_STOP_PCT), 4)
    # Same ATR logic as 0.4
    if atr <= 0:
        return round(pullback * (1 - config.STOP_LOSS_PCT_FALLBACK), 4)
    atr_stop = pullback - config.STOP_LOSS_ATR_MULT * atr
    min_stop = pullback * 0.70
    max_stop = pullback * 0.95
    atr_stop = max(min_stop, min(max_stop, atr_stop))
    return round(atr_stop, 4)


def check_early_stop(bars_after_entry: list[dict], entry_price: float,
                      n_bars: int = None) -> tuple[bool, float, int]:
    """Check if price stays below entry for first N bars after entry.
    Returns (triggered, stop_price, bar_index).
    """
    if n_bars is None:
        n_bars = config.EARLY_STOP_BARS
    check_bars = bars_after_entry[:n_bars]
    if len(check_bars) < n_bars:
        return False, 0.0, -1
    all_below = all(bar["close"] < entry_price for bar in check_bars)
    if all_below:
        stop_price = min(bar["low"] for bar in check_bars)
        return True, round(stop_price, 4), n_bars - 1
    return False, 0.0, -1


# ── 2. Re-entry quality control ────────────────────────────────

def find_reentry_point_042(bars: list[dict], open_price: float,
                            initial_highest: float = 0.0):
    """Find re-entry with deeper pullback requirement (3%+ from peak).
    Returns (entry_price, prev_high, entry_bar_idx, confirmed) or (0, 0, -1, False).
    """
    if len(bars) < 3:
        return 0, 0, -1, False

    highest = initial_highest
    peak_found = False
    vol_avg_window = 5

    for i in range(len(bars)):
        bh = bars[i]["high"]
        if bh > highest:
            highest = bh

        if not peak_found and highest > open_price * 1.03:
            peak_found = True

        if not peak_found:
            continue

        bl = bars[i]["low"]
        if (highest - bl) / highest > config.PULLBACK_STOP_THRESHOLD:
            return 0, 0, -1, False

        # ── 0.4.2: require deeper pullback (3%+ from peak) ──
        pullback_pct = (highest - bl) / highest
        if pullback_pct < config.REENTRY_MIN_PULLBACK_042:
            continue

        if i < 1:
            continue
        prev_low = bars[i - 1]["low"]
        if bl < prev_low:
            if i + 1 < len(bars) and bars[i + 1]["low"] >= bl:
                conf_bar = bars[i + 1]
                price_ok = conf_bar["close"] > conf_bar["open"]
                vol_start = max(0, i + 1 - vol_avg_window)
                recent_vols = [bars[j].get("volume", 0) for j in range(vol_start, i + 1)]
                avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else 0
                conf_vol = conf_bar.get("volume", 0)
                vol_ok = conf_vol > avg_vol * 1.2 if avg_vol > 0 else True

                if price_ok and vol_ok:
                    return bl, highest, i, True

    return 0, 0, -1, False


def evaluate_reentry_trade_042(
    entry_price: float,
    prev_high: float,
    shares: int,
    symbol: str,
    open_price: float,
    bars_after_entry: list[dict],
    force_close_price: float | None = None,
) -> TradeResult:
    """Re-entry trade with early exit: if no gain in 2 bars, exit early."""
    stop_price = round(entry_price * (1 - config.REENTRY_STOP_PCT), 4)
    target = round(entry_price + config.REENTRY_PROFIT_RETRACEMENT * (prev_high - entry_price), 4)

    highest = entry_price
    reached_target = False
    sold_partial = False
    partial_sell_price = 0.0
    partial_sell_shares = 0
    remaining_shares = shares

    def _make_result(reason, exit_price, bi):
        pnl_partial = (partial_sell_price - entry_price) * partial_sell_shares if sold_partial else 0
        pnl_rest = (exit_price - entry_price) * remaining_shares
        pnl = pnl_partial + pnl_rest
        pnl_pct = pnl / (entry_price * shares) if entry_price > 0 else 0
        return TradeResult(
            symbol=symbol,
            date=str(bar.get("timestamp", pd.Timestamp.now()).date()) if bi >= 0 else "",
            entry_price=entry_price, exit_price=exit_price, shares=shares,
            pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4), exit_reason=reason,
            open_price=open_price, sell_target=target, stop_price=stop_price,
            partial_sell_price=partial_sell_price, partial_sell_shares=partial_sell_shares,
            trailing_high=highest, trailing_exit_price=exit_price,
            exit_bar_idx=bi, position_size=entry_price * shares,
            trade_type="reentry",
        )

    # ── Early exit check for first N bars ──
    n_early = config.REENTRY_EARLY_EXIT_BARS
    if len(bars_after_entry) >= n_early:
        early_bars = bars_after_entry[:n_early]
        all_below = all(bar["close"] < entry_price for bar in early_bars)
        if all_below:
            exit_price = early_bars[-1]["close"]
            bar = early_bars[-1]
            return _make_result("reentry_early_exit", exit_price, n_early - 1)

    for bi, bar in enumerate(bars_after_entry):
        bh, bl = bar["high"], bar["low"]
        if bh > highest:
            highest = bh

        if bl <= stop_price:
            return _make_result("reentry_stop", stop_price, bi)

        if not reached_target and bh >= target:
            reached_target = True
            if not sold_partial:
                sold_partial = True
                partial_sell_price = target
                partial_sell_shares = remaining_shares // 3
                remaining_shares -= partial_sell_shares

        if reached_target and remaining_shares > 0:
            tsp = round(highest * (1 - config.REENTRY_TRAILING_PCT), 4)
            tsp = max(tsp, entry_price)
            if bl <= tsp:
                return _make_result("reentry_trailing", tsp, bi)

    if force_close_price is not None:
        exit_price = force_close_price
    else:
        exit_price = bars_after_entry[-1]["close"] if bars_after_entry else entry_price
    if not reached_target:
        exit_price = entry_price
    return _make_result("reentry_force_close", exit_price, len(bars_after_entry) - 1)


# ── 3. Entry quality filters ───────────────────────────────────

def check_volume_ratio(bars_5m, min_ratio: float = None) -> bool:
    """Check if opening bar volume is >= min_ratio times the average of next few bars.
    Uses first bar volume vs bars 2-6 average.
    """
    if min_ratio is None:
        min_ratio = config.VOLUME_RATIO_MIN
    if bars_5m.empty or len(bars_5m) < 3:
        return True  # not enough data to filter, allow

    first_vol = int(bars_5m.iloc[0]["volume"])
    # Average of bars 2-6 (skip bar 0)
    end = min(6, len(bars_5m))
    if end <= 1:
        return True
    avg_vol = bars_5m.iloc[1:end]["volume"].mean()
    if avg_vol <= 0:
        return True
    return first_vol >= avg_vol * min_ratio


def check_prior_gain(client, symbol: str, date, max_gain: float = None,
                      n_days: int = None) -> bool:
    """Check if stock had excessive gains in prior N days.
    Returns True if OK to trade, False if should skip.
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import Adjustment, DataFeed

    if max_gain is None:
        max_gain = config.PRIOR_GAIN_MAX
    if n_days is None:
        n_days = config.PRIOR_GAIN_DAYS

    start = date - pd.Timedelta(days=n_days * 2 + 5)
    request = StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
        start=start, end=date - pd.Timedelta(days=1),
        adjustment=Adjustment.RAW, feed=DataFeed.IEX,
    )
    try:
        bars = client.get_stock_bars(request)
    except Exception:
        return True  # API error, don't filter

    if bars.df.empty:
        return True

    df = bars.df
    closes = df["close"].values
    if len(closes) < 2:
        return True

    # Check cumulative gain over last N days
    recent = closes[-(n_days):]
    if len(recent) < 2:
        return True
    cumulative_gain = (recent[-1] / recent[0]) - 1
    return cumulative_gain <= max_gain


# ── 4. Late-day management ─────────────────────────────────────

def _is_late_bar(entry_bar_idx: int, bi: int) -> bool:
    """Check if current bar is after 14:00 (absolute bar index >= LATE_TIGHTEN_TIME_BAR)."""
    return (entry_bar_idx + 1 + bi) >= config.LATE_TIGHTEN_TIME_BAR


def _is_very_late_bar(entry_bar_idx: int, bi: int) -> bool:
    """Check if current bar is after 14:30."""
    return (entry_bar_idx + 1 + bi) >= config.LATE_CLOSE_TIME_BAR


# ── 5. Three-tier + late-day + early stop ───────────────────────

def evaluate_trade_stone_042(
    plan: TradePlan,
    bars_after_entry: list[dict],
    force_close_price: float | None = None,
    entry_bar_idx: int = 0,
    trail_pct_75: float = None,
    trail_pct_1125: float = None,
    trail_pct_150: float = None,
) -> TradeResult:
    """Stone 0.4.2 first trade: 1/3@75% + 1/3@100% + 1/3@150%,
    late-day trailing tightening, 14:30 close if 75% not reached.
    """
    if trail_pct_75 is None:
        trail_pct_75 = config.TRAILING_STOP_PCT_75
    if trail_pct_1125 is None:
        trail_pct_1125 = config.TRAILING_STOP_PCT_1125
    if trail_pct_150 is None:
        trail_pct_150 = config.TRAILING_STOP_PCT_150

    # 0.4.2 targets
    target_75 = calc_price_at_retracement(plan.pullback, plan.open_price, config.PROFIT_RETRACEMENT_75)
    target_100 = calc_price_at_retracement(plan.pullback, plan.open_price, config.PROFIT_RETRACEMENT_100_042)
    target_150 = calc_price_at_retracement(plan.pullback, plan.open_price, config.PROFIT_RETRACEMENT_150)

    reached_75 = reached_100 = reached_150 = False
    sold_partial_75 = sold_partial_100 = sold_partial_150 = False
    partial_sell_price_75 = 0.0
    partial_sell_shares_75 = 0
    partial_sell_price_100 = 0.0
    partial_sell_shares_100 = 0
    partial_sell_price_150 = 0.0
    partial_sell_shares_150 = 0
    highest = plan.pullback
    remaining_shares = plan.shares

    def _make_result(reason, exit_price, bi):
        pnl_75 = (partial_sell_price_75 - plan.pullback) * partial_sell_shares_75 if sold_partial_75 else 0
        pnl_100 = (partial_sell_price_100 - plan.pullback) * partial_sell_shares_100 if sold_partial_100 else 0
        pnl_150 = (partial_sell_price_150 - plan.pullback) * partial_sell_shares_150 if sold_partial_150 else 0
        pnl_rest = (exit_price - plan.pullback) * remaining_shares
        pnl = pnl_75 + pnl_100 + pnl_150 + pnl_rest
        pnl_pct = pnl / (plan.pullback * plan.shares) if plan.pullback > 0 else 0
        return TradeResult(
            symbol=plan.symbol,
            date=str(bar.get("timestamp", pd.Timestamp.now()).date()) if bi >= 0 else "",
            entry_price=plan.pullback, exit_price=exit_price, shares=plan.shares,
            pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4), exit_reason=reason,
            open_price=plan.open_price, sell_target=plan.target_150,
            stop_price=plan.stop_price,
            partial_sell_price=partial_sell_price_75, partial_sell_shares=partial_sell_shares_75,
            partial2_sell_price=partial_sell_price_100, partial2_sell_shares=partial_sell_shares_100,
            partial3_sell_price=partial_sell_price_150, partial3_sell_shares=partial_sell_shares_150,
            trailing_high=highest, trailing_exit_price=exit_price, atr=plan.atr,
            exit_bar_idx=bi, position_size=plan.pullback * plan.shares,
            trade_type="first",
        )

    for bi, bar in enumerate(bars_after_entry):
        bh, bl = bar["high"], bar["low"]
        if bh > highest:
            highest = bh

        # Stop loss
        if bl <= plan.stop_price:
            return _make_result("stop_loss", plan.stop_price, bi)

        # ── Late-day close: 14:30 and 75% not reached ──
        if _is_very_late_bar(entry_bar_idx, bi) and not reached_75:
            return _make_result("late_close", bl, bi)

        # ── Three-tier partial sells ──

        # Skip to 150 if price gaps through
        if not reached_150 and bh >= target_150:
            reached_150 = reached_100 = reached_75 = True
            if not sold_partial_150:
                sold_partial_150 = True
                partial_sell_price_150 = target_150
                partial_sell_shares_150 = remaining_shares // 3
                remaining_shares -= partial_sell_shares_150
            if not sold_partial_100:
                sold_partial_100 = True
                partial_sell_price_100 = target_150
                partial_sell_shares_100 = remaining_shares // 3
                remaining_shares -= partial_sell_shares_100
            if not sold_partial_75:
                sold_partial_75 = True
                partial_sell_price_75 = target_150
                partial_sell_shares_75 = plan.shares // 3
                remaining_shares -= partial_sell_shares_75

        if not reached_100 and bh >= target_100:
            reached_100 = reached_75 = True
            if not sold_partial_100:
                sold_partial_100 = True
                partial_sell_price_100 = target_100
                partial_sell_shares_100 = remaining_shares // 3
                remaining_shares -= partial_sell_shares_100
            if not sold_partial_75:
                sold_partial_75 = True
                partial_sell_price_75 = target_100
                partial_sell_shares_75 = plan.shares // 3
                remaining_shares -= partial_sell_shares_75

        if not reached_75 and bh >= target_75:
            reached_75 = True
            if not sold_partial_75:
                sold_partial_75 = True
                partial_sell_price_75 = target_75
                partial_sell_shares_75 = plan.shares // 3
                remaining_shares -= partial_sell_shares_75

        # ── Trailing stop with late-day tightening ──
        if reached_75:
            if reached_150:
                pct = trail_pct_150
            elif reached_100:
                pct = trail_pct_1125
            else:
                pct = trail_pct_75

            # Late-day tightening
            if _is_late_bar(entry_bar_idx, bi):
                pct *= config.TRAILING_LATE_FACTOR

            tsp = round(highest * (1 - pct), 4)
            tsp = max(tsp, plan.pullback)
            if bl <= tsp:
                suffix = "_150" if reached_150 else "_100" if reached_100 else "_75"
                return _make_result(f"trailing_stop{suffix}", tsp, bi)

    # Force close
    if force_close_price is not None:
        exit_price = force_close_price
    else:
        exit_price = bars_after_entry[-1]["close"] if bars_after_entry else plan.pullback
    if not reached_75 and not sold_partial_75 and not sold_partial_100 and not sold_partial_150:
        exit_price = plan.pullback

    return _make_result("force_close", exit_price, len(bars_after_entry) - 1)
