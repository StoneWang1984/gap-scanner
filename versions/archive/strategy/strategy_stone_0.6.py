"""Stone 0.6 Fusion Strategies — Combining Stone 0.4 and Stone 0.5.

0.6.1: 0.4 entry (pullback+confirmation) + three-tier exit + MACD 2nd-derivative early exit + MACD re-entry
0.6.2: MACD 2nd-derivative entry + 0.4 exit (three-tier + trailing + ATR stop), no re-entry
0.6.3: Dual entry (pullback OR MACD buy, whichever first) + three-tier + MACD sell early exit + MACD re-entry
"""

from dataclasses import dataclass

import config
from strategy import (
    TradePlan, build_trade_plan, calc_atr, calc_stop_price,
    calc_position_size, calc_price_at_retracement,
)
from strategy_05 import (
    calc_ema, calc_macd, check_macd_buy_signal, check_macd_sell_signal,
)


@dataclass
class TradeResult06:
    symbol: str
    date: str
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    pnl_pct: float
    exit_reason: str
    variant: str = ""
    open_price: float = 0.0
    sell_target: float = 0.0
    stop_price: float = 0.0
    entry_bar_idx: int = -1
    exit_bar_idx: int = -1
    position_size: float = 0.0
    trailing_high: float = 0.0
    trade_type: str = "first"
    partial_sell_price: float = 0.0
    partial_sell_shares: int = 0
    partial2_sell_price: float = 0.0
    partial2_sell_shares: int = 0
    partial3_sell_price: float = 0.0
    partial3_sell_shares: int = 0
    macd_at_entry: float = 0.0
    signal_at_entry: float = 0.0
    macd_at_exit: float = 0.0
    signal_at_exit: float = 0.0
    entry_method: str = ""


def evaluate_trade_three_tier_plus_macd(
    plan: TradePlan,
    bars_after_entry: list[dict],
    macd_line: list[float | None],
    signal_line: list[float | None],
    entry_bar_abs_idx: int,
    force_close_price: float | None = None,
    trail_pct_75: float = None,
    trail_pct_1125: float = None,
    trail_pct_150: float = None,
) -> TradeResult06:
    """Three-tier profit + trailing stops + ATR stop + MACD 2nd-derivative sell as early exit.

    Used by 0.6.1 and 0.6.3 first trades.
    bars_after_entry: list of bar dicts starting from the bar after entry.
    entry_bar_abs_idx: absolute index in macd_line/signal_line where entry occurs.
    """
    if trail_pct_75 is None:
        trail_pct_75 = config.TRAILING_STOP_PCT_75
    if trail_pct_1125 is None:
        trail_pct_1125 = config.TRAILING_STOP_PCT_1125
    if trail_pct_150 is None:
        trail_pct_150 = config.TRAILING_STOP_PCT_150

    macd_sell_abs_idx = check_macd_sell_signal(macd_line, signal_line, entry_bar_abs_idx + 1)

    reached_75 = reached_1125 = reached_150 = False
    sold_partial_75 = sold_partial_1125 = sold_partial_150 = False
    partial_sell_price_75 = 0.0
    partial_sell_shares_75 = 0
    partial_sell_price_1125 = 0.0
    partial_sell_shares_1125 = 0
    partial_sell_price_150 = 0.0
    partial_sell_shares_150 = 0
    highest = plan.pullback
    remaining_shares = plan.shares

    def _macd_val(arr, idx):
        if 0 <= idx < len(arr) and arr[idx] is not None:
            return float(arr[idx])
        return 0.0

    def _make_result(reason, exit_price, bi, abs_idx):
        pnl_75 = (partial_sell_price_75 - plan.pullback) * partial_sell_shares_75 if sold_partial_75 else 0
        pnl_1125 = (partial_sell_price_1125 - plan.pullback) * partial_sell_shares_1125 if sold_partial_1125 else 0
        pnl_150 = (partial_sell_price_150 - plan.pullback) * partial_sell_shares_150 if sold_partial_150 else 0
        pnl_rest = (exit_price - plan.pullback) * remaining_shares
        pnl = pnl_75 + pnl_1125 + pnl_150 + pnl_rest
        pnl_pct = pnl / (plan.pullback * plan.shares) if plan.pullback > 0 else 0

        return TradeResult06(
            symbol=plan.symbol, date="", entry_price=plan.pullback,
            exit_price=exit_price, shares=plan.shares,
            pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4), exit_reason=reason,
            open_price=plan.open_price, sell_target=plan.target_150,
            stop_price=plan.stop_price, exit_bar_idx=bi,
            position_size=plan.pullback * plan.shares,
            trailing_high=highest, trade_type="first",
            partial_sell_price=partial_sell_price_75,
            partial_sell_shares=partial_sell_shares_75,
            partial2_sell_price=partial_sell_price_1125,
            partial2_sell_shares=partial_sell_shares_1125,
            partial3_sell_price=partial_sell_price_150,
            partial3_sell_shares=partial_sell_shares_150,
            macd_at_entry=_macd_val(macd_line, entry_bar_abs_idx),
            signal_at_entry=_macd_val(signal_line, entry_bar_abs_idx),
            macd_at_exit=_macd_val(macd_line, abs_idx),
            signal_at_exit=_macd_val(signal_line, abs_idx),
        )

    for bi, bar in enumerate(bars_after_entry):
        abs_idx = entry_bar_abs_idx + 1 + bi
        bh, bl = bar["high"], bar["low"]
        if bh > highest:
            highest = bh

        # Stop loss
        if bl <= plan.stop_price:
            return _make_result("stop_loss", plan.stop_price, bi, abs_idx)

        # MACD sell signal (early exit)
        if macd_sell_abs_idx >= 0 and abs_idx == macd_sell_abs_idx:
            return _make_result("macd_sell", bar["close"], bi, abs_idx)

        # Three-tier targets
        if not reached_150 and bh >= plan.target_150:
            reached_150 = reached_1125 = reached_75 = True
            if not sold_partial_150:
                sold_partial_150 = True
                partial_sell_price_150 = plan.target_150
                partial_sell_shares_150 = remaining_shares // 3
                remaining_shares -= partial_sell_shares_150
            if not sold_partial_1125:
                sold_partial_1125 = True
                partial_sell_price_1125 = plan.target_150
                partial_sell_shares_1125 = remaining_shares // 3
                remaining_shares -= partial_sell_shares_1125
            if not sold_partial_75:
                sold_partial_75 = True
                partial_sell_price_75 = plan.target_150
                partial_sell_shares_75 = plan.shares // 4
                remaining_shares -= partial_sell_shares_75

        if not reached_1125 and bh >= plan.target_1125:
            reached_1125 = reached_75 = True
            if not sold_partial_1125:
                sold_partial_1125 = True
                partial_sell_price_1125 = plan.target_1125
                partial_sell_shares_1125 = remaining_shares // 3
                remaining_shares -= partial_sell_shares_1125
            if not sold_partial_75:
                sold_partial_75 = True
                partial_sell_price_75 = plan.target_1125
                partial_sell_shares_75 = plan.shares // 4
                remaining_shares -= partial_sell_shares_75

        if not reached_75 and bh >= plan.target_75:
            reached_75 = True
            if not sold_partial_75:
                sold_partial_75 = True
                partial_sell_price_75 = plan.target_75
                partial_sell_shares_75 = plan.shares // 4
                remaining_shares -= partial_sell_shares_75

        # Trailing stops
        if reached_75:
            if reached_150:
                pct = trail_pct_150
            elif reached_1125:
                pct = trail_pct_1125
            else:
                pct = trail_pct_75
            tsp = round(highest * (1 - pct), 4)
            tsp = max(tsp, plan.pullback)
            if bl <= tsp:
                suffix = "_150" if reached_150 else "_1125" if reached_1125 else "_75"
                return _make_result(f"trailing_stop{suffix}", tsp, bi, abs_idx)

    # Force close
    if force_close_price is not None:
        exit_price = force_close_price
    else:
        exit_price = bars_after_entry[-1]["close"] if bars_after_entry else plan.pullback
    if not reached_75 and not sold_partial_75 and not sold_partial_1125 and not sold_partial_150:
        exit_price = plan.pullback

    abs_idx_final = entry_bar_abs_idx + len(bars_after_entry)
    return _make_result("force_close", exit_price, len(bars_after_entry) - 1, abs_idx_final)


def evaluate_reentry_macd(
    symbol: str,
    open_price: float,
    shares: int,
    all_closes: list[float],
    all_lows: list[float],
    macd_line: list[float | None],
    signal_line: list[float | None],
    entry_bar_abs_idx: int,
    stop_price: float,
    force_close_abs_idx: int | None = None,
) -> TradeResult06:
    """MACD re-entry trade: MACD buy entry + MACD sell exit + stop loss + force close.

    Used by 0.6.1 and 0.6.3 for re-entry trades.
    All indices are absolute in all_closes/macd_line arrays.
    """
    max_idx = force_close_abs_idx if force_close_abs_idx is not None else len(all_closes) - 1

    exit_price = 0.0
    exit_reason = ""
    actual_exit_idx = entry_bar_abs_idx

    # Pre-compute MACD sell signal
    macd_sell_idx = check_macd_sell_signal(macd_line, signal_line, entry_bar_abs_idx + 1)

    for i in range(entry_bar_abs_idx + 1, max_idx + 1):
        # Stop loss
        if i < len(all_lows) and all_lows[i] <= stop_price:
            exit_price = stop_price
            exit_reason = "reentry_stop"
            actual_exit_idx = i
            break

        # MACD sell signal
        if macd_sell_idx >= 0 and i == macd_sell_idx:
            exit_price = all_closes[i]
            exit_reason = "reentry_macd_sell"
            actual_exit_idx = i
            break

        # Pullback stop
        if i < len(all_lows):
            pullback_from_high = max(
                all_closes[j] for j in range(entry_bar_abs_idx, i + 1)
            ) - all_lows[i]
            if pullback_from_high / max(all_closes[j] for j in range(entry_bar_abs_idx, i + 1)) > config.PULLBACK_STOP_THRESHOLD:
                exit_price = all_closes[i]
                exit_reason = "reentry_pullback_stop"
                actual_exit_idx = i
                break
    else:
        exit_price = all_closes[max_idx]
        exit_reason = "reentry_force_close"
        actual_exit_idx = max_idx

    pnl = (exit_price - all_closes[entry_bar_abs_idx]) * shares
    cost = all_closes[entry_bar_abs_idx] * shares
    pnl_pct = pnl / cost if cost > 0 else 0.0

    def _macd_val(arr, idx):
        if 0 <= idx < len(arr) and arr[idx] is not None:
            return float(arr[idx])
        return 0.0

    return TradeResult06(
        symbol=symbol, date="",
        entry_price=all_closes[entry_bar_abs_idx],
        exit_price=exit_price, shares=shares,
        pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4),
        exit_reason=exit_reason, open_price=open_price,
        stop_price=stop_price,
        entry_bar_idx=entry_bar_abs_idx,
        exit_bar_idx=actual_exit_idx,
        position_size=all_closes[entry_bar_abs_idx] * shares,
        trade_type="reentry",
        macd_at_entry=_macd_val(macd_line, entry_bar_abs_idx),
        signal_at_entry=_macd_val(signal_line, entry_bar_abs_idx),
        macd_at_exit=_macd_val(macd_line, actual_exit_idx),
        signal_at_exit=_macd_val(signal_line, actual_exit_idx),
        entry_method="macd_reentry",
    )
