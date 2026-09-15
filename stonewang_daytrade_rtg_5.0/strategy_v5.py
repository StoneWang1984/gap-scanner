"""Strategy — stonewang_daytrade_rtg_5.0: ORB + ATR Stops + Progressive Trail.

Based on Cam Connor and Brian Shannon's day trading principles:
  1. ATR-based stops: stop = max(ATR×mult, |gap|×0.3), clamped to min%-max%
  2. Progressive trailing: profit>5%→1.5%, >10%→1%, >15%→0.5%
  3. Failed-entry cut: +1% in 3 min or exit at market
  4. No profit target — trail + progressive trail manage exit
"""

from dataclasses import dataclass, field
import config


@dataclass
class TradeResult:
    symbol: str
    date: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    shares: int = 0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""
    open_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    trailing_high: float = 0.0
    exit_bar_idx: int = -1
    position_size: float = 0.0
    entry_bar_idx: int = 0
    signal_type: str = ""
    entry_ts: float = 0.0  # For failed-entry timing


def _get_progressive_trail(profit_pct):
    """Return trail percentage based on current profit, tightening as profit grows."""
    tiers = getattr(config, "PROGRESSIVE_TRAIL_TIERS", [])
    if not tiers:
        return config.RTG_TRAIL_PCT
    for threshold, trail in sorted(tiers, reverse=True):
        if profit_pct >= threshold:
            return trail
    return config.RTG_TRAIL_PCT


def calc_atr_stop(entry_price, atr, gap_pct, rvol):
    """ATR-based stop with gap expansion. Returns stop_pct."""
    mult = 2.0
    for threshold, m in sorted(getattr(config, "ATR_MULT_TIERS", []), reverse=True):
        if rvol >= threshold:
            mult = m
            break
    atr_stop = (atr * mult) / entry_price if entry_price > 0 else 0
    gap_stop = abs(gap_pct) * getattr(config, "GAP_STOP_FACTOR", 0.3)
    stop_pct = max(atr_stop, gap_stop)
    stop_pct = max(stop_pct, getattr(config, "ATR_STOP_MIN_PCT", 0.02))
    stop_pct = min(stop_pct, getattr(config, "ATR_STOP_MAX_PCT", 0.08))
    return stop_pct


def evaluate_trade_rtg(
    entry_price: float,
    shares: int,
    bars_after_entry: list,
    symbol: str = "",
    open_price: float = 0.0,
    force_close_price: float | None = None,
    entry_bar_idx: int = 0,
    signal_type: str = "",
    stop_pct: float | None = None,
    target_pct: float | None = None,
    trail_activate_pct: float | None = None,
    trail_pct: float | None = None,
    time_limit_sec: int | None = None,
    gap_pct: float = 0.0,
    rvol: float = 0.0,
    atr: float = 0.0,
) -> TradeResult:
    """RTG 5.0 exit with ATR stops, progressive trailing, and failed-entry cut.

    bars_after_entry: list of dicts with keys "high", "low", "close", "open", "volume", "timestamp"
    """
    if not bars_after_entry or entry_price <= 0 or shares <= 0:
        return TradeResult(
            symbol=symbol, entry_price=entry_price, exit_price=entry_price,
            shares=shares, exit_reason="no_bars", open_price=open_price,
            signal_type=signal_type,
        )

    # --- ATR-based stop calculation ---
    if atr > 0 and stop_pct is None:
        stop_pct = calc_atr_stop(entry_price, atr, gap_pct, rvol)
    _stop_pct = stop_pct if stop_pct is not None else config.RTG_STOP_PCT
    _target_pct = target_pct if target_pct is not None else config.RTG_TARGET_PCT
    _trail_act = trail_activate_pct if trail_activate_pct is not None else config.RTG_TRAIL_ACTIVATE_PCT
    _trail_pct = trail_pct if trail_pct is not None else config.RTG_TRAIL_PCT
    _time_sec = time_limit_sec if time_limit_sec is not None else config.RTG_TIME_LIMIT_SEC

    stop_price = round(entry_price * (1 - _stop_pct), 4)
    target_price = round(entry_price * (1 + _target_pct), 4) if _target_pct > 0 else 0.0
    trail_activate = entry_price * (1 + _trail_act)
    time_limit_bars = 0 if _time_sec == 0 else max(1, _time_sec // 60)

    slippage = getattr(config, "SLIPPAGE_EXIT_PCT", 0.0)

    # Failed-entry cut parameters
    fe_enabled = getattr(config, "FAILED_ENTRY_ENABLED", True)
    fe_min_gain = getattr(config, "FAILED_ENTRY_MIN_GAIN_PCT", 0.01)
    fe_max_sec = getattr(config, "FAILED_ENTRY_MAX_SECONDS", 180)

    highest = entry_price
    trail_active = False
    trail_stop = 0.0
    current_trail_pct = _trail_pct
    exit_price = 0.0
    reason = ""
    exit_bi = 0

    for bi, bar in enumerate(bars_after_entry):
        bar_open = float(bar["open"])
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bar_close = float(bar["close"])

        if bar_high > highest:
            highest = bar_high

        # 1. Hard stop (highest priority)
        if bar_low <= stop_price:
            if bar_open < stop_price:
                # Gap-through: bar opened below stop, fill at open with slippage
                exit_price = round(bar_open * (1 - slippage), 4)
            else:
                exit_price = stop_price
            reason = "stop_loss"
            exit_bi = bi
            break

        # 2. Progressive trailing stop
        # Recalculate trail width based on current profit
        current_profit = (highest - entry_price) / entry_price
        current_trail_pct = _get_progressive_trail(current_profit)

        if not trail_active and highest >= trail_activate:
            trail_active = True
            trail_stop = round(highest * (1 - current_trail_pct), 4)
        if trail_active:
            new_trail = round(highest * (1 - current_trail_pct), 4)
            if new_trail > trail_stop:
                trail_stop = new_trail
            if bar_low <= trail_stop:
                if bar_open < trail_stop:
                    # Gap-through: bar opened below trail stop, fill at open with slippage
                    exit_price = round(bar_open * (1 - slippage), 4)
                else:
                    exit_price = trail_stop
                reason = "trail_stop"
                exit_bi = bi
                break

        # 3. Target (disabled when 0%)
        if _target_pct > 0 and target_price > 0 and bar_high >= target_price:
            exit_price = target_price
            reason = "target"
            exit_bi = bi
            break

        # 4. Failed-entry cut: if stock hasn't gained +1% within 3 min, exit
        if fe_enabled and bi >= fe_max_sec // 60:
            stock_gain = (highest - entry_price) / entry_price
            if stock_gain < fe_min_gain:
                exit_price = bar_close
                reason = "failed_entry"
                exit_bi = bi
                break

        # 5. Time limit (0 = disabled)
        if time_limit_bars > 0 and bi >= time_limit_bars:
            exit_price = bar_close
            reason = "time_limit"
            exit_bi = bi
            break

        exit_bi = bi
    else:
        # Loop completed without break — force close at end
        if force_close_price is not None and force_close_price > 0:
            exit_price = force_close_price
        else:
            exit_price = float(bars_after_entry[-1]["close"])
        reason = "force_close"
        exit_bi = len(bars_after_entry) - 1

    # Apply exit slippage (not for stop/trail/target — those are limit orders)
    if reason == "force_close":
        fc_slippage = getattr(config, "SLIPPAGE_FORCE_CLOSE_PCT", 0.01)
        exit_price = round(exit_price * (1 - fc_slippage), 4)
    elif slippage > 0 and reason not in ("stop_loss", "trail_stop", "target"):
        exit_price = round(exit_price * (1 - slippage), 4)

    pnl = round((exit_price - entry_price) * shares, 2)

    # Regulatory fees on sale
    sale_proceeds = exit_price * shares
    sec_fee = sale_proceeds * 0.000027   # SEC fee: 0.0027% of sale proceeds
    finra_taf = shares * 0.000166        # FINRA TAF: $0.000166 per share
    reg_fees = round(sec_fee + finra_taf, 4)
    pnl = round(pnl - reg_fees, 2)
    pnl_pct = round(pnl / (entry_price * shares), 4) if entry_price > 0 else 0.0

    return TradeResult(
        symbol=symbol,
        date="",
        entry_price=round(entry_price, 4),
        exit_price=round(exit_price, 4),
        shares=shares,
        pnl=pnl,
        pnl_pct=pnl_pct,
        exit_reason=reason,
        open_price=round(open_price, 4) if open_price else 0.0,
        stop_price=stop_price,
        target_price=target_price,
        trailing_high=round(highest, 4),
        exit_bar_idx=exit_bi,
        entry_bar_idx=entry_bar_idx,
        position_size=round(entry_price * shares, 2),
        signal_type=signal_type,
    )
