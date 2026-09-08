"""Strategy — stonewang_daytrade_getgreenbar_1.0: 连续绿bar骑乘

Exit logic (evaluate_trade_greenbar):
  1. Hard stop: bar low <= entry × (1 - stop_pct) → exit at stop price
  2. Red bar: bar close < bar open → exit at bar close (核心: 绿bar序列结束)
  3. Trailing: after entry × (1 + trail_activate_pct) reached,
     trail at highest × (1 - trail_pct). If bar low <= trail → exit.
  4. Target: bar high >= entry × (1 + target_pct) → exit at target price
  5. Force close: end of bars → exit at force_close_price or last close

Priority: stop > red_bar > trailing > target (checked in this order per bar).
"""

from dataclasses import dataclass
from collections import deque
import config


# ── EMA / MACD 计算 ────────────────────────────────────────────────

def ema_update(value, prev_ema, period):
    """Incremental EMA update. Returns new EMA value."""
    if prev_ema is None:
        return value
    k = 2.0 / (period + 1)
    return value * k + prev_ema * (1 - k)


class MACDCalculator:
    """Incremental MACD calculator fed bar-by-bar."""

    def __init__(self, fast=12, slow=26, signal=9):
        self.fast = fast
        self.slow = slow
        self.signal = signal
        self._ema_fast = None
        self._ema_slow = None
        self._ema_signal = None
        self._prev_macd = None
        self.macd = 0.0
        self.signal_line = 0.0
        self.histogram = 0.0

    def update(self, close_price):
        """Feed a new bar close price. Updates MACD, signal, histogram."""
        self._ema_fast = ema_update(close_price, self._ema_fast, self.fast)
        self._ema_slow = ema_update(close_price, self._ema_slow, self.slow)
        self.macd = self._ema_fast - self._ema_slow
        self._ema_signal = ema_update(self.macd, self._ema_signal, self.signal)
        self.signal_line = self._ema_signal
        self.histogram = self.macd - self.signal_line
        self._prev_macd = self.macd

    def is_bullish(self, mode="above_zero"):
        """Check if MACD confirms bullish entry."""
        if mode == "above_zero":
            return self.macd > 0
        elif mode == "cross_signal":
            return self.histogram > 0  # MACD above signal line
        return True

    def is_bullish_cross(self):
        """MACD just crossed above signal line (golden cross)."""
        return self.histogram > 0 and self._prev_macd is not None


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
    signal_type: str = ""  # "greenbar" or "greenbar_re"


def evaluate_trade_greenbar(
    entry_price, shares, bars_after_entry,
    symbol="", open_price=0.0,
    stop_pct=None, target_pct=None,
    trail_activate_pct=None, trail_pct=None,
    exit_on_red_bar=True,
    force_close_price=None,
    entry_bar_idx=0, signal_type="greenbar",
):
    """绿bar骑乘回测: bar变红退出 / 止损 / 追踪 / 目标。"""
    if not bars_after_entry:
        return TradeResult(
            symbol=symbol, entry_price=entry_price, shares=shares,
            exit_reason="no_bars", open_price=open_price,
            signal_type=signal_type,
        )

    _stop_pct = stop_pct if stop_pct is not None else config.GBAR_STOP_PCT
    _target_pct = target_pct if target_pct is not None else config.GBAR_TARGET_PCT
    _trail_act = trail_activate_pct if trail_activate_pct is not None else config.GBAR_TRAIL_ACTIVATE_PCT
    _trail_pct = trail_pct if trail_pct is not None else config.GBAR_TRAIL_PCT

    stop_price = round(entry_price * (1 - _stop_pct), 4)
    target_price = round(entry_price * (1 + _target_pct), 4)
    trail_activate = entry_price * (1 + _trail_act)

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
        bar_open = float(bar["open"])
        is_red = bar_close < bar_open

        if bar_high > highest:
            highest = bar_high

        # 1. Hard stop (highest priority)
        if bar_low <= stop_price:
            exit_price = stop_price
            reason = "stop_loss"
            exit_bi = bi
            break

        # 2. Red bar — 绿bar序列结束 (core exit logic)
        if exit_on_red_bar and is_red:
            exit_price = bar_close
            reason = "bar_turned_red"
            exit_bi = bi
            break

        # 3. Trailing stop (after activation)
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

        # 4. Target
        if bar_high >= target_price:
            exit_price = target_price
            reason = "target"
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

    pnl = (exit_price - entry_price) * shares
    pnl_pct = pnl / (entry_price * shares) if entry_price * shares else 0

    return TradeResult(
        symbol=symbol,
        entry_price=entry_price,
        exit_price=round(exit_price, 4),
        shares=shares,
        pnl=round(pnl, 2),
        pnl_pct=round(pnl_pct, 4),
        exit_reason=reason,
        open_price=open_price,
        stop_price=stop_price,
        target_price=target_price,
        trailing_high=round(highest, 4),
        exit_bar_idx=exit_bi,
        entry_bar_idx=entry_bar_idx,
        signal_type=signal_type,
    )
