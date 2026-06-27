"""Backtesting engine — Stone 0.4.1: Same as 0.4 but with 5 max daily trades."""

import config
from backtest import run_backtest


def run_backtest_041(end_date=None, n_days=config.BACKTEST_DAYS):
    """Run Stone 0.4.1 backtest: 0.4 strategy with 5 max daily trades."""
    original_trades = config.MAX_DAILY_TRADES
    original_positions = config.MAX_POSITIONS_PER_DAY

    config.MAX_DAILY_TRADES = 5
    config.MAX_POSITIONS_PER_DAY = 5

    try:
        return run_backtest(end_date=end_date, n_days=n_days)
    finally:
        config.MAX_DAILY_TRADES = original_trades
        config.MAX_POSITIONS_PER_DAY = original_positions
