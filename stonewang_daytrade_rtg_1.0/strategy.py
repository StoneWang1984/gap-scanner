"""Strategy — stonewang_daytrade_rtg_1.0: Red-to-Green Volume Breakout.

Exit logic (evaluate_trade_rtg):
  1. Hard stop: bar low <= entry × (1 - RTG_STOP_PCT) → exit at stop price
  2. Target: bar high >= entry × (1 + RTG_TARGET_PCT) → exit at target price
  3. Trailing: after entry × (1 + RTG_TRAIL_ACTIVATE_PCT) reached,
     trail at highest × (1 - RTG_TRAIL_PCT). If bar low <= trail → exit.
  4. Time limit: bi >= RTG_TIME_LIMIT_SEC // 60 → exit at bar close
  5. Force close: end of bars → exit at force_close_price or last close

Priority: stop > trailing > target > time_limit (checked in this order per bar).
"""

from dataclasses import dataclass
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
    signal_type: str = ""  # "rtg" (red-to-green) or "gapgo" (gap-and-go)


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
) -> TradeResult:
    """RTG exit with adaptive per-trade parameters.

    bars_after_entry: list of dicts with keys "high", "low", "close", "open", "volume", "timestamp"
    """
    if not bars_after_entry or entry_price <= 0 or shares <= 0:
        return TradeResult(
            symbol=symbol, entry_price=entry_price, exit_price=entry_price,
            shares=shares, exit_reason="no_bars", open_price=open_price,
            signal_type=signal_type,
        )

    _stop_pct = stop_pct if stop_pct is not None else config.RTG_STOP_PCT
    _target_pct = target_pct if target_pct is not None else config.RTG_TARGET_PCT
    _trail_act = trail_activate_pct if trail_activate_pct is not None else config.RTG_TRAIL_ACTIVATE_PCT
    _trail_pct = trail_pct if trail_pct is not None else config.RTG_TRAIL_PCT
    _time_sec = time_limit_sec if time_limit_sec is not None else config.RTG_TIME_LIMIT_SEC

    stop_price = round(entry_price * (1 - _stop_pct), 4)
    target_price = round(entry_price * (1 + _target_pct), 4)
    trail_activate = entry_price * (1 + _trail_act)
    time_limit_bars = max(1, _time_sec // 60)

    slippage = getattr(config, "SLIPPAGE_EXIT_PCT", 0.0)

    highest = entry_price
    trail_active = False
    trail_stop = 0.0
    exit_price = 0.0
    reason = ""
    exit_bi = 0

    for bi, bar in enumerate(bars_after_entry):
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bar_close = float(bar["close"])

        if bar_high > highest:
            highest = bar_high

        # 1. Hard stop (highest priority)
        if bar_low <= stop_price:
            exit_price = stop_price
            reason = "stop_loss"
            exit_bi = bi
            break

        # 2. Trailing stop (after activation)
        if not trail_active and highest >= trail_activate:
            trail_active = True
            trail_stop = round(highest * (1 - _trail_pct), 4)
        if trail_active:
            new_trail = round(highest * (1 - _trail_pct), 4)
            if new_trail > trail_stop:
                trail_stop = new_trail
            if bar_low <= trail_stop:
                exit_price = trail_stop
                reason = "trail_stop"
                exit_bi = bi
                break

        # 3. Target
        if bar_high >= target_price:
            exit_price = target_price
            reason = "target"
            exit_bi = bi
            break

        # 4. Time limit
        if bi >= time_limit_bars:
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

    # Apply exit slippage
    if slippage > 0 and reason not in ("stop_loss", "trail_stop", "target"):
        exit_price = round(exit_price * (1 - slippage), 4)

    pnl = round((exit_price - entry_price) * shares, 2)
    pnl_pct = round(pnl / (entry_price * shares), 4) if entry_price > 0 else 0.0

    return TradeResult(
        symbol=symbol,
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
