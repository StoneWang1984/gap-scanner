"""Strategy — stonewang_daytrade_rtg_2mins_all: RTG entry + 5% stop + 3-min time limit (all day).

Exit logic (evaluate_trade_rtg):
  1. Hard stop: bar low <= entry × (1 - RTG_STOP_PCT) → exit at stop price
  2. Time limit: bi >= RTG_TIME_LIMIT_SEC // 60 → exit at bar close
  3. Force close: end of bars → exit at force_close_price or last close

Min hold 60s: no switch in first 60 seconds.
No trailing stop, no progressive trail, no profit protection, no target.
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
    signal_type: str = ""


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
    """RTG exit with 5% hard stop + 3-min time limit (matches live_trade.py).

    bars_after_entry: list of dicts with keys "high", "low", "close", "open", "volume", "timestamp"
    """
    if not bars_after_entry or entry_price <= 0 or shares <= 0:
        return TradeResult(
            symbol=symbol, entry_price=entry_price, exit_price=entry_price,
            shares=shares, exit_reason="no_bars", open_price=open_price,
            signal_type=signal_type,
        )

    _stop_pct = stop_pct if stop_pct is not None else config.RTG_STOP_PCT
    _time_sec = time_limit_sec if time_limit_sec is not None else config.RTG_TIME_LIMIT_SEC

    stop_price = round(entry_price * (1 - _stop_pct), 4)
    time_limit_bars = 0 if _time_sec == 0 else max(1, _time_sec // 60)

    slippage = getattr(config, "SLIPPAGE_EXIT_PCT", 0.0)

    highest = entry_price
    exit_price = 0.0
    reason = ""
    exit_bi = 0

    for bi, bar in enumerate(bars_after_entry):
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bar_close = float(bar["close"])
        bar_open = float(bar["open"])

        if bar_high > highest:
            highest = bar_high

        # 1. Hard stop (with gap-through model)
        if bar_low <= stop_price:
            if bar_open < stop_price:
                exit_price = round(bar_open * (1 - slippage), 4)
            else:
                exit_price = stop_price
            reason = "stop_loss"
            exit_bi = bi
            break

        # 2. Time limit (0 = disabled)
        if time_limit_bars > 0 and bi >= time_limit_bars:
            exit_price = round(bar_close * (1 - slippage), 4)
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

    # Apply exit slippage for force_close
    if slippage > 0 and reason == "force_close":
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
        target_price=0,
        trailing_high=round(highest, 4),
        exit_bar_idx=exit_bi,
        entry_bar_idx=entry_bar_idx,
        position_size=round(entry_price * shares, 2),
        signal_type=signal_type,
    )
