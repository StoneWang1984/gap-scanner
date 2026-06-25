"""Gap pullback strategy — Stone 0.2 with multi-entry, short selling, gap tiering."""

from dataclasses import dataclass
import config


def calc_price_at_retracement(pullback: float, open_price: float, retracement: float) -> float:
    return round(pullback + retracement * (open_price - pullback), 4)


def calc_atr(bars: list[dict], period: int = 14) -> float:
    """Calculate ATR from bar data. Each bar must have: high, low, close."""
    if len(bars) < 2:
        return 0.0

    true_ranges = []
    for i in range(1, min(len(bars), period + 1)):
        bar = bars[i]
        prev_close = bars[i - 1]["close"]
        tr = max(
            bar["high"] - bar["low"],
            abs(bar["high"] - prev_close),
            abs(bar["low"] - prev_close),
        )
        true_ranges.append(tr)

    if not true_ranges:
        return 0.0
    return sum(true_ranges) / len(true_ranges)


def calc_stop_price(pullback: float, atr: float, atr_mult: float = None) -> float:
    """ATR-based stop loss. Fallback to fixed % if ATR unavailable or too wide/narrow."""
    if atr_mult is None:
        atr_mult = config.STOP_LOSS_ATR_MULT
    if atr <= 0:
        return round(pullback * (1 - config.STOP_LOSS_PCT_FALLBACK), 4)

    atr_stop = pullback - atr_mult * atr

    # Clamp: stop must be between 5% and 30% below entry
    min_stop = pullback * 0.70
    max_stop = pullback * 0.95
    atr_stop = max(min_stop, min(max_stop, atr_stop))

    return round(atr_stop, 4)


def calc_shares(position_size: float, price: float) -> int:
    if price <= 0:
        return 0
    return int(position_size / price)


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


def build_trade_plan(symbol: str, open_price: float, pullback: float, atr: float = 0.0) -> TradePlan:
    target_75 = calc_price_at_retracement(pullback, open_price, config.PROFIT_RETRACEMENT_75)
    target_150 = calc_price_at_retracement(pullback, open_price, config.PROFIT_RETRACEMENT_150)
    stop_price = calc_stop_price(pullback, atr)
    shares = calc_shares(config.POSITION_SIZE, pullback)

    return TradePlan(
        symbol=symbol,
        open_price=open_price,
        pullback=pullback,
        target_75=target_75,
        target_150=target_150,
        stop_price=stop_price,
        shares=shares,
        atr=atr,
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
    trailing_high: float = 0.0
    trailing_exit_price: float = 0.0
    atr: float = 0.0
    exit_bar_idx: int = -1  # bar index where exit occurred (relative to bars_after_entry)
    direction: str = "long"  # "long" or "short"
    entry_index: int = 0     # which entry (1, 2, or 3) for this stock


def evaluate_trade_stone(
    plan: TradePlan,
    bars_after_entry: list[dict],
    force_close_price: float | None = None,
    trail_pct_75: float = None,
    trail_pct_150: float = None,
) -> TradeResult:
    """Stone strategy: evaluate long trade with configurable trailing + exit bar tracking."""
    if trail_pct_75 is None:
        trail_pct_75 = config.TRAILING_STOP_PCT_75
    if trail_pct_150 is None:
        trail_pct_150 = config.TRAILING_STOP_PCT_150

    reached_75 = reached_150 = sold_partial = False
    partial_sell_price = 0.0
    partial_sell_shares = 0
    highest = plan.pullback
    remaining_shares = plan.shares

    for bi, bar in enumerate(bars_after_entry):
        bh, bl = bar["high"], bar["low"]
        if bh > highest:
            highest = bh

        if bl <= plan.stop_price:
            pp = (partial_sell_price - plan.pullback) * partial_sell_shares if sold_partial else 0
            pr = (plan.stop_price - plan.pullback) * remaining_shares
            pnl = pp + pr
            pnl_pct = pnl / (plan.pullback * plan.shares) if plan.pullback > 0 else 0
            return TradeResult(
                symbol=plan.symbol, date=str(bar["timestamp"].date()),
                entry_price=plan.pullback, exit_price=plan.stop_price, shares=plan.shares,
                pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4), exit_reason="stop_loss",
                open_price=plan.open_price, sell_target=plan.target_150,
                stop_price=plan.stop_price, partial_sell_price=partial_sell_price,
                partial_sell_shares=partial_sell_shares, trailing_high=highest,
                trailing_exit_price=plan.stop_price, atr=plan.atr,
                exit_bar_idx=bi,
            )

        if not reached_150 and bh >= plan.target_150:
            reached_150 = reached_75 = True
            if not sold_partial:
                sold_partial = True
                partial_sell_price = plan.target_150
                partial_sell_shares = plan.shares // 3
                remaining_shares = plan.shares - partial_sell_shares

        if not reached_75 and bh >= plan.target_75:
            reached_75 = True

        if reached_75:
            pct = trail_pct_150 if reached_150 else trail_pct_75
            tsp = round(highest * (1 - pct), 4)
            tsp = max(tsp, plan.pullback)
            if bl <= tsp:
                pp = (partial_sell_price - plan.pullback) * partial_sell_shares if sold_partial else 0
                pr = (tsp - plan.pullback) * remaining_shares
                pnl = pp + pr
                pnl_pct = pnl / (plan.pullback * plan.shares) if plan.pullback > 0 else 0
                return TradeResult(
                    symbol=plan.symbol, date=str(bar["timestamp"].date()),
                    entry_price=plan.pullback, exit_price=tsp, shares=plan.shares,
                    pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4),
                    exit_reason="trailing_stop_150" if reached_150 else "trailing_stop_75",
                    open_price=plan.open_price, sell_target=plan.target_150,
                    stop_price=plan.stop_price, partial_sell_price=partial_sell_price,
                    partial_sell_shares=partial_sell_shares, trailing_high=highest,
                    trailing_exit_price=tsp, atr=plan.atr,
                    exit_bar_idx=bi,
                )

    if force_close_price is not None:
        exit_price = force_close_price
    else:
        exit_price = bars_after_entry[-1]["close"] if bars_after_entry else plan.pullback
    if reached_75 and not sold_partial:
        exit_price = max(exit_price, plan.pullback)
    elif not reached_75:
        exit_price = plan.pullback

    pp = (partial_sell_price - plan.pullback) * partial_sell_shares if sold_partial else 0
    pr = (exit_price - plan.pullback) * remaining_shares
    pnl = pp + pr
    pnl_pct = pnl / (plan.pullback * plan.shares) if plan.pullback > 0 else 0

    return TradeResult(
        symbol=plan.symbol, date="",
        entry_price=plan.pullback, exit_price=exit_price, shares=plan.shares,
        pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4), exit_reason="force_close",
        open_price=plan.open_price, sell_target=plan.target_150,
        stop_price=plan.stop_price, partial_sell_price=partial_sell_price,
        partial_sell_shares=partial_sell_shares, trailing_high=highest,
        trailing_exit_price=exit_price, atr=plan.atr,
        exit_bar_idx=len(bars_after_entry) - 1,
    )


def evaluate_short_trade(
    symbol: str,
    entry_price: float,
    open_price: float,
    shares: int,
    atr: float,
    bars_after_entry: list[dict],
    force_close_price: float | None = None,
) -> TradeResult:
    """Evaluate short trade: fixed stop/target, conservative approach."""
    stop_price = round(entry_price * (1 + config.SHORT_STOP_PCT), 4)
    target_price = round(entry_price * (1 - config.SHORT_TARGET_PCT), 4)

    highest = entry_price
    for bi, bar in enumerate(bars_after_entry):
        bh, bl = bar["high"], bar["low"]
        if bh > highest:
            highest = bh

        # Stop loss (price goes above stop)
        if bh >= stop_price:
            pnl = (entry_price - stop_price) * shares
            pnl_pct = pnl / (entry_price * shares) if entry_price > 0 else 0
            return TradeResult(
                symbol=symbol, date=str(bar["timestamp"].date()),
                entry_price=entry_price, exit_price=stop_price, shares=shares,
                pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4), exit_reason="stop_loss",
                open_price=open_price, stop_price=stop_price,
                trailing_high=highest, trailing_exit_price=stop_price,
                atr=atr, exit_bar_idx=bi, direction="short",
            )

        # Take profit (price drops to target)
        if bl <= target_price:
            pnl = (entry_price - target_price) * shares
            pnl_pct = pnl / (entry_price * shares) if entry_price > 0 else 0
            return TradeResult(
                symbol=symbol, date=str(bar["timestamp"].date()),
                entry_price=entry_price, exit_price=target_price, shares=shares,
                pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4), exit_reason="take_profit",
                open_price=open_price, stop_price=stop_price,
                sell_target=target_price, trailing_high=highest,
                trailing_exit_price=target_price, atr=atr,
                exit_bar_idx=bi, direction="short",
            )

    # Force close
    exit_price = force_close_price if force_close_price else (
        bars_after_entry[-1]["close"] if bars_after_entry else entry_price)
    pnl = (entry_price - exit_price) * shares
    pnl_pct = pnl / (entry_price * shares) if entry_price > 0 else 0
    return TradeResult(
        symbol=symbol, date="",
        entry_price=entry_price, exit_price=exit_price, shares=shares,
        pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4), exit_reason="force_close",
        open_price=open_price, stop_price=stop_price,
        sell_target=target_price, trailing_high=highest,
        trailing_exit_price=exit_price, atr=atr,
        exit_bar_idx=len(bars_after_entry) - 1, direction="short",
    )
