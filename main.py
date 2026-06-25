"""Gap Pullback Strategy — CLI entry point."""

import argparse
import sys

from backtest import run_backtest
from report import print_report, plot_results, plot_trade_charts
from plot_signals import plot_all_signal_charts

import config


def main():
    parser = argparse.ArgumentParser(description="Gap Pullback Trading Strategy")
    parser.add_argument("--backtest", action="store_true", help="Run backtest mode")
    parser.add_argument("--days", type=int, default=config.BACKTEST_DAYS,
                        help=f"Number of trading days to backtest (default: {config.BACKTEST_DAYS})")
    parser.add_argument("--capital", type=float, default=config.INITIAL_CAPITAL,
                        help=f"Initial capital (default: ${config.INITIAL_CAPITAL:,.0f})")
    parser.add_argument("--output", type=str, default=".", help="Output directory for charts")
    args = parser.parse_args()

    if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
        print("Error: Set ALPACA_API_KEY and ALPACA_SECRET_KEY environment variables.")
        print("  export ALPACA_API_KEY='your-key'")
        print("  export ALPACA_SECRET_KEY='your-secret'")
        sys.exit(1)

    if args.capital != config.INITIAL_CAPITAL:
        config.INITIAL_CAPITAL = args.capital

    if args.backtest:
        print("Running backtest...")
        trades = run_backtest(n_days=args.days)
        print_report(trades)
        plot_results(trades, output_dir=args.output)
        plot_trade_charts(trades, output_dir=args.output)
        plot_all_signal_charts(trades, output_dir=args.output)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
