"""Stone 0.5 Strategy — MACD with second-derivative signals on 5-minute bars.

Buy:  signal > MACD, AND the change of (signal - MACD) is increasing (2nd derivative > 0).
      This means convergence is decelerating — momentum shifting bullish before crossover.
Sell: MACD > signal, AND the change of (MACD - signal) is decreasing (2nd derivative < 0).
      This means convergence is accelerating — momentum shifting bearish before crossover.
Stop loss at configurable percentage. No take profit, no re-entry.
"""

from dataclasses import dataclass

import config


# ── MACD Calculation ────────────────────────────────────────────────

def calc_ema(prices: list[float], period: int) -> list[float | None]:
    """Calculate EMA. Returns list same length as prices; None where not enough data."""
    if len(prices) < period:
        return [None] * len(prices)

    result: list[float | None] = [None] * (period - 1)
    k = 2.0 / (period + 1)

    sma = sum(prices[:period]) / period
    result.append(sma)

    for i in range(period, len(prices)):
        ema = prices[i] * k + result[-1] * (1 - k)  # type: ignore
        result.append(ema)

    return result


def calc_macd(
    closes: list[float],
    fast: int = config.MACD_FAST,
    slow: int = config.MACD_SLOW,
    signal: int = config.MACD_SIGNAL,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Calculate MACD line, Signal line, Histogram."""
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)

    macd_line: list[float | None] = []
    valid_macd_values: list[float] = []
    valid_macd_indices: list[int] = []

    for i in range(len(closes)):
        if ema_fast[i] is not None and ema_slow[i] is None:
            macd_line.append(None)
            continue
        if ema_fast[i] is not None and ema_slow[i] is not None:
            val = ema_fast[i] - ema_slow[i]  # type: ignore
            macd_line.append(val)
            valid_macd_values.append(val)
            valid_macd_indices.append(i)
        else:
            macd_line.append(None)

    signal_ema = calc_ema(valid_macd_values, signal)

    signal_line: list[float | None] = [None] * len(closes)
    for j, idx in enumerate(valid_macd_indices):
        signal_line[idx] = signal_ema[j]

    histogram: list[float | None] = []
    for i in range(len(closes)):
        if macd_line[i] is not None and signal_line[i] is not None:
            histogram.append(macd_line[i] - signal_line[i])  # type: ignore
        else:
            histogram.append(None)

    return macd_line, signal_line, histogram


# ── Signal Detection (2nd derivative) ───────────────────────────────

def check_macd_buy_signal(
    macd_line: list[float | None],
    signal_line: list[float | None],
    start_idx: int = 0,
) -> int:
    """Find first bar where MACD buy signal occurs (2nd derivative).

    Buy: signal > MACD AND the change of (signal - MACD) is increasing.
    - gap = signal - MACD (positive)
    - gap_change[t] = gap[t] - gap[t-1]
    - gap_accel[t] = gap_change[t] - gap_change[t-1] > 0
    This means the convergence is decelerating — bullish momentum shift.

    Returns bar index, or -1 if no signal found.
    """
    for i in range(max(start_idx, 2), len(macd_line)):
        if any(v is None for v in [
            macd_line[i], signal_line[i],
            macd_line[i - 1], signal_line[i - 1],
            macd_line[i - 2], signal_line[i - 2],
        ]):
            continue

        # Signal above MACD
        if signal_line[i] <= macd_line[i]:  # type: ignore
            continue

        # 2nd derivative: gap change is increasing
        gap_2 = signal_line[i - 2] - macd_line[i - 2]  # type: ignore
        gap_1 = signal_line[i - 1] - macd_line[i - 1]  # type: ignore
        gap_0 = signal_line[i] - macd_line[i]  # type: ignore

        gap_change_prev = gap_1 - gap_2
        gap_change_curr = gap_0 - gap_1

        # gap_accel > 0: the change of the gap is increasing
        if gap_change_curr > gap_change_prev:
            return i

    return -1


def check_macd_sell_signal(
    macd_line: list[float | None],
    signal_line: list[float | None],
    start_idx: int = 0,
) -> int:
    """Find first bar where MACD sell signal occurs (2nd derivative).

    Sell: MACD > signal AND the change of (MACD - signal) is decreasing.
    - gap = MACD - signal (positive)
    - gap_change[t] = gap[t] - gap[t-1]
    - gap_accel[t] = gap_change[t] - gap_change[t-1] < 0
    This means the convergence is accelerating — bearish momentum shift.

    Returns bar index, or -1 if no signal found.
    """
    for i in range(max(start_idx, 2), len(macd_line)):
        if any(v is None for v in [
            macd_line[i], signal_line[i],
            macd_line[i - 1], signal_line[i - 1],
            macd_line[i - 2], signal_line[i - 2],
        ]):
            continue

        # MACD above signal
        if macd_line[i] <= signal_line[i]:  # type: ignore
            continue

        # 2nd derivative: gap change is decreasing
        gap_2 = macd_line[i - 2] - signal_line[i - 2]  # type: ignore
        gap_1 = macd_line[i - 1] - signal_line[i - 1]  # type: ignore
        gap_0 = macd_line[i] - signal_line[i]  # type: ignore

        gap_change_prev = gap_1 - gap_2
        gap_change_curr = gap_0 - gap_1

        # gap_accel < 0: the change of the gap is decreasing (decelerating)
        if gap_change_curr < gap_change_prev:
            return i

    return -1


# ── Trade Result ────────────────────────────────────────────────────

@dataclass
class TradeResult05:
    symbol: str
    date: str
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    pnl_pct: float
    exit_reason: str  # "macd_sell", "stop_loss", "force_close"
    open_price: float = 0.0
    entry_bar_idx: int = -1
    exit_bar_idx: int = -1
    position_size: float = 0.0
    stop_price: float = 0.0
    macd_at_entry: float = 0.0
    signal_at_entry: float = 0.0
    macd_at_exit: float = 0.0
    signal_at_exit: float = 0.0


# ── Position Sizing ─────────────────────────────────────────────────

def calc_position_size(equity: float) -> float:
    """Calculate deployable position size from current equity."""
    pos = equity * config.EQUITY_POSITION_RATIO
    return max(pos, config.MIN_POSITION_SIZE)


# ── Trade Evaluation ───────────────────────────────────────────────

def evaluate_trade_macd(
    symbol: str,
    open_price: float,
    shares: int,
    all_closes: list[float],
    all_lows: list[float],
    macd_line: list[float | None],
    signal_line: list[float | None],
    entry_bar_idx: int,
    stop_price: float,
    force_close_idx: int | None = None,
) -> TradeResult05:
    """Evaluate a single trade using MACD sell signal + stop loss.

    All index parameters are absolute indices into the all_closes/macd_line arrays.
    """
    max_idx = force_close_idx if force_close_idx is not None else len(all_closes) - 1

    exit_price: float = 0.0
    exit_reason: str = ""
    actual_exit_idx: int = entry_bar_idx

    for i in range(entry_bar_idx + 1, max_idx + 1):
        # Stop loss check (bar low touches stop)
        if i < len(all_lows) and all_lows[i] <= stop_price:
            exit_price = stop_price
            exit_reason = "stop_loss"
            actual_exit_idx = i
            break

        # MACD sell signal check
        sell_idx = check_macd_sell_signal(macd_line, signal_line, i)
        if sell_idx == i:
            exit_price = all_closes[i]
            exit_reason = "macd_sell"
            actual_exit_idx = i
            break
    else:
        exit_price = all_closes[max_idx]
        exit_reason = "force_close"
        actual_exit_idx = max_idx

    pnl = (exit_price - all_closes[entry_bar_idx]) * shares
    cost = all_closes[entry_bar_idx] * shares
    pnl_pct = pnl / cost if cost > 0 else 0.0

    macd_entry = macd_line[entry_bar_idx] if entry_bar_idx < len(macd_line) else None
    signal_entry = signal_line[entry_bar_idx] if entry_bar_idx < len(signal_line) else None
    macd_exit = macd_line[actual_exit_idx] if actual_exit_idx < len(macd_line) else None
    signal_exit = signal_line[actual_exit_idx] if actual_exit_idx < len(signal_line) else None

    return TradeResult05(
        symbol=symbol,
        date="",
        entry_price=all_closes[entry_bar_idx],
        exit_price=exit_price,
        shares=shares,
        pnl=pnl,
        pnl_pct=pnl_pct,
        exit_reason=exit_reason,
        open_price=open_price,
        entry_bar_idx=entry_bar_idx,
        exit_bar_idx=actual_exit_idx,
        position_size=all_closes[entry_bar_idx] * shares,
        stop_price=stop_price,
        macd_at_entry=float(macd_entry) if macd_entry is not None else 0.0,
        signal_at_entry=float(signal_entry) if signal_entry is not None else 0.0,
        macd_at_exit=float(macd_exit) if macd_exit is not None else 0.0,
        signal_at_exit=float(signal_exit) if signal_exit is not None else 0.0,
    )
