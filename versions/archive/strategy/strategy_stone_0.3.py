"""Gap pullback strategy — Stone 0.3: equity compounding, dynamic partial profit."""

from dataclasses import dataclass
import config


def calc_price_at_retracement(pullback: float, open_price: float, retracement: float) -> float:
    return round(pullback + retracement * (open_price - pullback), 4)


def calc_atr(bars: list[dict], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    true_ranges = []
    for i in range(1, min(len(bars), period + 1)):
        bar = bars[i]
        prev_close = bars[i - 1]["close"]
        tr = max(bar["high"] - bar["low"], abs(bar["high"] - prev_close), abs(bar["low"] - prev_close))
        true_ranges.append(tr)
    if not true_ranges:
        return 0.0
    return sum(true_ranges) / len(true_ranges)


def calc_stop_price(pullback: float, atr: float, atr_mult: float = None) -> float:
    if atr_mult is None:
        atr_mult = config.STOP_LOSS_ATR_MULT
    if atr <= 0:
        return round(pullback * (1 - config.STOP_LOSS_PCT_FALLBACK), 4)
    atr_stop = pullback - atr_mult * atr
    min_stop = pullback * 0.70
    max_stop = pullback * 0.95
    atr_stop = max(min_stop, min(max_stop, atr_stop))
    return round(atr_stop, 4)


def calc_position_size(equity: float) -> float:
    """Equity-compounding position sizing."""
    size = equity * config.EQUITY_POSITION_RATIO
    return max(size, config.MIN_POSITION_SIZE)


@dataclass
class TradePlan:
    symbol: str
    open_price: float
    pullback: float
    target_75: float
    target_150: float
    stop_price: float
    shares: int = 0
    atr: float = 0.0


def build_trade_plan(symbol: str, open_price: float, pullback: float, atr: float = 0.0,
                     position_size: float = None) -> TradePlan:
    if position_size is None:
        position_size = config.MIN_POSITION_SIZE
    target_75 = calc_price_at_retracement(pullback, open_price, config.PROFIT_RETRACEMENT_75)
    target_150 = calc_price_at_retracement(pullback, open_price, config.PROFIT_RETRACEMENT_150)
    stop_price = calc_stop_price(pullback, atr)
    shares = int(position_size / pullback) if pullback > 0 else 0
    return TradePlan(
        symbol=symbol, open_price=open_price, pullback=pullback,
        target_75=target_75, target_150=target_150,
        stop_price=stop_price, shares=shares, atr=atr,
    )


@dataclass
class TradeResult:
    symbol: str
    date: str
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    pnl_pct: float
    exit_reason: str
    open_price: float = 0.0
    sell_target: float = 0.0
    stop_price: float = 0.0
    bars_5m: list | None = None
    partial_sell_price: float = 0.0
    partial_sell_shares: int = 0
    partial2_sell_price: float = 0.0
    partial2_sell_shares: int = 0
    trailing_high: float = 0.0
    trailing_exit_price: float = 0.0
    atr: float = 0.0
    exit_bar_idx: int = -1
    position_size: float = 0.0


def evaluate_trade_stone(
    plan: TradePlan,
    bars_after_entry: list[dict],
    force_close_price: float | None = None,
    trail_pct_75: float = None,
    trail_pct_150: float = None,
) -> TradeResult:
    """Stone 0.3: sell 1/4 at 75%, sell 1/3 of remaining at 150%, then trail."""
    if trail_pct_75 is None:
        trail_pct_75 = config.TRAILING_STOP_PCT_75
    if trail_pct_150 is None:
        trail_pct_150 = config.TRAILING_STOP_PCT_150

    reached_75 = reached_150 = False
    sold_partial_75 = False  # sold at 75% target
    sold_partial_150 = False  # sold at 150% target
    partial_sell_price_75 = 0.0
    partial_sell_shares_75 = 0
    partial_sell_price_150 = 0.0
    partial_sell_shares_150 = 0
    highest = plan.pullback
    remaining_shares = plan.shares

    def _make_result(reason, exit_price, bi):
        pnl_75 = (partial_sell_price_75 - plan.pullback) * partial_sell_shares_75 if sold_partial_75 else 0
        pnl_150 = (partial_sell_price_150 - plan.pullback) * partial_sell_shares_150 if sold_partial_150 else 0
        pnl_rest = (exit_price - plan.pullback) * remaining_shares
        pnl = pnl_75 + pnl_150 + pnl_rest
        pnl_pct = pnl / (plan.pullback * plan.shares) if plan.pullback > 0 else 0
        return TradeResult(
            symbol=plan.symbol, date=str(bar.get("timestamp", pd.Timestamp.now()).date()) if bi >= 0 else "",
            entry_price=plan.pullback, exit_price=exit_price, shares=plan.shares,
            pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4), exit_reason=reason,
            open_price=plan.open_price, sell_target=plan.target_150,
            stop_price=plan.stop_price,
            partial_sell_price=partial_sell_price_75, partial_sell_shares=partial_sell_shares_75,
            partial2_sell_price=partial_sell_price_150, partial2_sell_shares=partial_sell_shares_150,
            trailing_high=highest, trailing_exit_price=exit_price, atr=plan.atr,
            exit_bar_idx=bi, position_size=plan.pullback * plan.shares,
        )

    import pandas as pd  # for timestamp fallback

    for bi, bar in enumerate(bars_after_entry):
        bh, bl = bar["high"], bar["low"]
        if bh > highest:
            highest = bh

        # 1. Stop loss
        if bl <= plan.stop_price:
            exit_price = plan.stop_price
            return _make_result("stop_loss", exit_price, bi)

        # 2. Check 150% target — sell 1/3 of remaining
        if not reached_150 and bh >= plan.target_150:
            reached_150 = True
            reached_75 = True
            if not sold_partial_150:
                sold_partial_150 = True
                partial_sell_price_150 = plan.target_150
                partial_sell_shares_150 = remaining_shares // 3
                remaining_shares -= partial_sell_shares_150
            # Also sell 1/4 at 75% if not already done
            if not sold_partial_75:
                sold_partial_75 = True
                partial_sell_price_75 = plan.target_150  # sells at current price
                partial_sell_shares_75 = plan.shares // 4
                remaining_shares -= partial_sell_shares_75

        # 3. Check 75% target — sell 1/4
        if not reached_75 and bh >= plan.target_75:
            reached_75 = True
            if not sold_partial_75:
                sold_partial_75 = True
                partial_sell_price_75 = plan.target_75
                partial_sell_shares_75 = plan.shares // 4
                remaining_shares -= partial_sell_shares_75

        # 4. Tiered trailing stop
        if reached_75:
            pct = trail_pct_150 if reached_150 else trail_pct_75
            tsp = round(highest * (1 - pct), 4)
            tsp = max(tsp, plan.pullback)
            if bl <= tsp:
                return _make_result(
                    "trailing_stop_150" if reached_150 else "trailing_stop_75",
                    tsp, bi,
                )

    # 5. Force close at EOD
    if force_close_price is not None:
        exit_price = force_close_price
    else:
        exit_price = bars_after_entry[-1]["close"] if bars_after_entry else plan.pullback
    if reached_75 and remaining_shares > 0 and not sold_partial_75:
        exit_price = max(exit_price, plan.pullback)
    elif not reached_75:
        # No partial sold, force close at breakeven for remaining
        if not sold_partial_75 and not sold_partial_150:
            exit_price = plan.pullback

    return _make_result("force_close", exit_price, len(bars_after_entry) - 1)
