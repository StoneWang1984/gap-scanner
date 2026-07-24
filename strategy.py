"""Gap pullback strategy — Stone 1.0: 6-tier first trade + re-entry."""

from dataclasses import dataclass
import config


def calc_price_at_retracement(pullback: float, open_price: float, retracement: float) -> float:
    return round(pullback + retracement * (open_price - pullback), 2)


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
        stop = pullback * (1 - config.STOP_LOSS_PCT_FALLBACK)
    else:
        atr_stop = pullback - atr_mult * atr
        min_stop = pullback * 0.70
        max_stop = pullback * 0.95
        stop = max(min_stop, min(max_stop, atr_stop))
    # 0.4.14: Cap stop loss at max percentage from entry
    max_pct = getattr(config, "STOP_LOSS_MAX_PCT", 0)
    if max_pct > 0:
        stop = max(stop, pullback * (1 - max_pct))
    return round(stop, 2)


def calc_position_size(equity: float) -> float:
    size = equity * config.EQUITY_POSITION_RATIO
    return max(size, config.MIN_POSITION_SIZE)


@dataclass
class TradePlan:
    symbol: str
    open_price: float
    pullback: float
    targets: list       # list of target prices (6 tiers)
    sell_ratios: list   # list of sell ratios per tier (6 tiers)
    trail_pcts: list    # list of trailing stop pcts per tier (6 tiers)
    stop_price: float
    shares: int = 0
    atr: float = 0.0
    target_mode: str = "retracement"  # "retracement" or "capped"

    # Legacy fields for backward compat
    @property
    def target_75(self):
        return self.targets[2] if len(self.targets) > 2 else 0.0

    @property
    def target_1125(self):
        return self.targets[4] if len(self.targets) > 4 else 0.0

    @property
    def target_150(self):
        return self.targets[5] if len(self.targets) > 5 else 0.0


def build_trade_plan(symbol: str, open_price: float, pullback: float, atr: float = 0.0,
                     position_size: float = None) -> TradePlan:
    if position_size is None:
        position_size = config.MIN_POSITION_SIZE

    retracements = getattr(config, "PROFIT_RETRACEMENT_TIERS", [0.25, 0.50, 0.75, 1.00, 1.25, 1.50])
    caps = getattr(config, "TARGET_CAP_TIERS", [0.05, 0.10, 0.15, 0.20, 0.25, 0.35])
    sell_ratios = getattr(config, "PARTIAL_SELL_RATIOS", [1/8]*6)
    trail_pcts = getattr(config, "TRAILING_STOP_PCTS", [0.02, 0.025, 0.03, 0.035, 0.04, 0.05])

    targets = []
    any_capped = False
    for i in range(len(retracements)):
        ret_price = calc_price_at_retracement(pullback, open_price, retracements[i])
        cap_price = round(pullback * (1 + caps[i]), 2)
        t = min(ret_price, cap_price)
        if t < ret_price:
            any_capped = True
        targets.append(t)

    target_mode = "capped" if any_capped else "retracement"

    stop_price = calc_stop_price(pullback, atr)
    shares = int(position_size / pullback) if pullback > 0 else 0
    return TradePlan(
        symbol=symbol, open_price=open_price, pullback=pullback,
        targets=targets, sell_ratios=sell_ratios, trail_pcts=trail_pcts,
        stop_price=stop_price, shares=shares, atr=atr, target_mode=target_mode,
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
    partial_sells: list = None  # list of (price, shares) tuples per tier
    trailing_high: float = 0.0
    trailing_exit_price: float = 0.0
    atr: float = 0.0
    exit_bar_idx: int = -1
    position_size: float = 0.0
    trade_type: str = "first"  # "first" or "reentry"

    # Legacy fields for backward compat
    @property
    def partial_sell_price(self):
        ps = self.partial_sells
        return ps[0][0] if ps and len(ps) > 0 and ps[0][1] > 0 else 0.0

    @property
    def partial_sell_shares(self):
        ps = self.partial_sells
        return ps[0][1] if ps and len(ps) > 0 else 0

    @property
    def partial2_sell_price(self):
        ps = self.partial_sells
        return ps[1][0] if ps and len(ps) > 1 and ps[1][1] > 0 else 0.0

    @property
    def partial2_sell_shares(self):
        ps = self.partial_sells
        return ps[1][1] if ps and len(ps) > 1 else 0

    @property
    def partial3_sell_price(self):
        ps = self.partial_sells
        return ps[2][0] if ps and len(ps) > 2 and ps[2][1] > 0 else 0.0

    @property
    def partial3_sell_shares(self):
        ps = self.partial_sells
        return ps[2][1] if ps and len(ps) > 2 else 0


def evaluate_trade_stone(
    plan: TradePlan,
    bars_after_entry: list[dict],
    force_close_price: float | None = None,
    trail_pct_75: float = None,
    trail_pct_1125: float = None,
    trail_pct_150: float = None,
    time_limit_bars: int = 0,
) -> TradeResult:
    """Stone 0.4 first trade: N-tier partial sells with trailing stop.

    Sells sell_ratios[i] of original shares at each target.
    time_limit_bars: if > 0 and no target hit within this many bars,
                     sell all when price >= entry price.
    """
    n_tiers = len(plan.targets)
    reached = [False] * n_tiers
    sold = [False] * n_tiers
    partial_prices = [0.0] * n_tiers
    partial_shares = [0] * n_tiers
    remaining_shares = plan.shares
    highest = plan.pullback
    time_limit_active = False

    def _make_result(reason, exit_price, bi):
        pnl = 0.0
        partial_sells = []
        for i in range(n_tiers):
            if sold[i]:
                pnl += (partial_prices[i] - plan.pullback) * partial_shares[i]
            partial_sells.append((partial_prices[i], partial_shares[i]))
        pnl_rest = (exit_price - plan.pullback) * remaining_shares
        pnl += pnl_rest
        pnl_pct = pnl / (plan.pullback * plan.shares) if plan.pullback > 0 else 0
        return TradeResult(
            symbol=plan.symbol, date=str(bar.get("timestamp", pd.Timestamp.now()).date()) if bi >= 0 else "",
            entry_price=plan.pullback, exit_price=exit_price, shares=plan.shares,
            pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4), exit_reason=reason,
            open_price=plan.open_price, sell_target=plan.targets[-1],
            stop_price=plan.stop_price,
            partial_sells=partial_sells,
            trailing_high=highest, trailing_exit_price=exit_price, atr=plan.atr,
            exit_bar_idx=bi, position_size=plan.pullback * plan.shares,
            trade_type="first",
        )

    import pandas as pd

    for bi, bar in enumerate(bars_after_entry):
        bh, bl = bar["high"], bar["low"]
        if bh > highest:
            highest = bh

        if bl <= plan.stop_price:
            return _make_result("stop_loss", plan.stop_price, bi)

        # Time limit: if no target hit within time_limit_bars, sell at breakeven or better
        if time_limit_bars > 0 and not reached[0] and bi >= time_limit_bars:
            time_limit_active = True
        if time_limit_active and bh >= plan.pullback:
            exit_price = max(bh, plan.pullback)
            return _make_result("time_limit_exit", exit_price, bi)

        # Check targets from highest to lowest (skip-gap handling)
        for ti in range(n_tiers - 1, -1, -1):
            if not reached[ti] and bh >= plan.targets[ti]:
                # Mark all lower tiers as reached
                for tj in range(ti + 1):
                    reached[tj] = True
                # Sell at this tier
                if not sold[ti]:
                    sold[ti] = True
                    partial_prices[ti] = plan.targets[ti]
                    sell_n = max(1, int(plan.shares * plan.sell_ratios[ti]))
                    sell_n = min(sell_n, remaining_shares)
                    partial_shares[ti] = sell_n
                    remaining_shares -= sell_n
                # Handle lower unsold tiers in a skip-gap
                for tj in range(ti):
                    if not sold[tj]:
                        sold[tj] = True
                        partial_prices[tj] = plan.targets[ti]
                        sell_n = max(1, int(plan.shares * plan.sell_ratios[tj]))
                        sell_n = min(sell_n, remaining_shares)
                        partial_shares[tj] = sell_n
                        remaining_shares -= sell_n

        # Trailing stop after first target reached
        if reached[0]:
            # Find highest reached tier
            highest_tier = 0
            for ti in range(n_tiers - 1, -1, -1):
                if reached[ti]:
                    highest_tier = ti
                    break
            pct = plan.trail_pcts[highest_tier]
            tsp = round(highest * (1 - pct), 2)
            tsp = max(tsp, plan.pullback)
            if bl <= tsp:
                retracements = getattr(config, "PROFIT_RETRACEMENT_TIERS", [0.25, 0.50, 0.75, 1.00, 1.25, 1.50])
                suffix = f"_{int(retracements[highest_tier] * 100)}"
                return _make_result(f"trailing_stop{suffix}", tsp, bi)

    if force_close_price is not None:
        exit_price = force_close_price
    else:
        exit_price = bars_after_entry[-1]["close"] if bars_after_entry else plan.pullback
    if not any(reached):
        exit_price = plan.pullback

    return _make_result("force_close", exit_price, len(bars_after_entry) - 1)


def find_reentry_point(bars: list[dict], open_price: float, initial_highest: float = 0.0):
    """Find re-entry after first trade exits: peak then pullback with confirmation.
    Requires volume-price confirmation: confirmation bar must be bullish (close > open)
    and volume > average of recent bars.
    initial_highest: highest price from first trade, carried forward for peak detection.
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

        if i < 1:
            continue
        prev_low = bars[i - 1]["low"]
        if bl < prev_low:
            # Potential pullback found, check confirmation
            if i + 1 < len(bars) and bars[i + 1]["low"] >= bl:
                # === Volume-price confirmation ===
                conf_bar = bars[i + 1]
                # 1. Price: bullish bar (close > open)
                price_ok = conf_bar["close"] > conf_bar["open"]
                # 2. Volume: confirmation bar volume > recent average
                vol_start = max(0, i + 1 - vol_avg_window)
                recent_vols = [bars[j].get("volume", 0) for j in range(vol_start, i + 1)]
                avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else 0
                conf_vol = conf_bar.get("volume", 0)
                vol_ok = conf_vol > avg_vol * 1.2 if avg_vol > 0 else True  # 20% above average

                if price_ok and vol_ok:
                    entry_price = bl
                    prev_high = highest
                    return entry_price, prev_high, i, True

    return 0, 0, -1, False


def evaluate_reentry_trade(
    entry_price: float,
    prev_high: float,
    shares: int,
    symbol: str,
    open_price: float,
    bars_after_entry: list[dict],
    force_close_price: float | None = None,
    stop_price: float | None = None,
    reentry_profit_retracement_1: float | None = None,
    reentry_trailing_pct_2: float | None = None,
    reentry_sell_ratio_1: float | None = None,
) -> TradeResult:
    """Re-entry trade: sell 50% at retracement target, then 3% trailing stop.

    reentry_profit_retracement_1: tier-1 retracement (default config.REENTRY_PROFIT_RETRACEMENT_1 or 0.75)
    reentry_trailing_pct_2: trailing stop % after tier-1 (default config.REENTRY_TRAILING_PCT_2 or 0.03)
    reentry_sell_ratio_1: fraction to sell at tier-1 (default config.REENTRY_SELL_RATIO_1 or 0.5)
    """
    if reentry_profit_retracement_1 is None:
        reentry_profit_retracement_1 = getattr(config, "REENTRY_PROFIT_RETRACEMENT_1", 0.75)
    if reentry_trailing_pct_2 is None:
        reentry_trailing_pct_2 = getattr(config, "REENTRY_TRAILING_PCT_2", 0.03)
    if reentry_sell_ratio_1 is None:
        reentry_sell_ratio_1 = getattr(config, "REENTRY_SELL_RATIO_1", 0.5)

    if stop_price is None:
        stop_price = round(entry_price * (1 - config.REENTRY_STOP_PCT), 2)

    target_1 = round(entry_price + reentry_profit_retracement_1 * (prev_high - entry_price), 2)

    highest = entry_price
    reached_tier1 = False
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
            symbol=symbol, date=str(bar.get("timestamp", pd.Timestamp.now()).date()) if bi >= 0 else "",
            entry_price=entry_price, exit_price=exit_price, shares=shares,
            pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4), exit_reason=reason,
            open_price=open_price, sell_target=target_1, stop_price=stop_price,
            partial_sells=[(partial_sell_price, partial_sell_shares)] if sold_partial else [],
            trailing_high=highest, trailing_exit_price=exit_price,
            exit_bar_idx=bi, position_size=entry_price * shares,
            trade_type="reentry",
        )

    import pandas as pd

    for bi, bar in enumerate(bars_after_entry):
        bh, bl = bar["high"], bar["low"]
        if bh > highest:
            highest = bh

        # Stop loss
        if bl <= stop_price:
            return _make_result("reentry_stop", stop_price, bi)

        # Tier-1: sell reentry_sell_ratio_1 at retracement target
        if not reached_tier1 and bh >= target_1:
            reached_tier1 = True
            if not sold_partial:
                sold_partial = True
                partial_sell_price = target_1
                partial_sell_shares = int(remaining_shares * reentry_sell_ratio_1)
                remaining_shares -= partial_sell_shares

        # Trailing stop after tier-1
        if reached_tier1 and remaining_shares > 0:
            tsp = round(highest * (1 - reentry_trailing_pct_2), 2)
            tsp = max(tsp, entry_price)
            if bl <= tsp:
                return _make_result("reentry_trailing", tsp, bi)

    # Force close
    if force_close_price is not None:
        exit_price = force_close_price
    else:
        exit_price = bars_after_entry[-1]["close"] if bars_after_entry else entry_price
    if not reached_tier1:
        exit_price = entry_price
    return _make_result("reentry_force_close", exit_price, len(bars_after_entry) - 1)
