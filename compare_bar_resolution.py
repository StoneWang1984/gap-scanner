"""Compare 1-min vs 5-min bars for position management after entry.

Runs the same entry detection (1-min pullback) for both,
then evaluates the trade using 1-min bars vs 5-min bars.
"""

import pandas as pd
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import Adjustment, DataFeed

import config
from scanner import get_data_client, get_tradable_symbols
from backtest import is_leveraged_etf
from strategy import (
    calc_atr, calc_stop_price, build_trade_plan,
    evaluate_trade_stone, TradeResult,
)
from backtest import (
    get_trading_days, bulk_scan_gaps, get_1min_bars, get_5min_bars,
    find_entry_with_confirmation_1min, locate_5min_bar_index,
    _bars_to_list,
)


def run_comparison(n_days=30):
    client = get_data_client()
    end_date = pd.Timestamp.now(tz="America/New_York")
    trading_days = get_trading_days(client, end_date, n_days)

    symbols = get_tradable_symbols()
    symbols = [s for s in symbols if not is_leveraged_etf(s)]
    print(f"Found {len(symbols)} tradable symbols")

    gap_data = bulk_scan_gaps(client, trading_days, symbols)
    print(f"Found {sum(len(v) for v in gap_data.values())} gap entries across {len(gap_data)} days")

    # Time limit: 5-min=8 bars (40min), 1-min=40 bars (40min) — same real time
    time_limit_5m = getattr(config, "FIRST_TRADE_TIME_LIMIT_BARS", 8)
    time_limit_1m = time_limit_5m * 5  # 40 bars × 1min = 40min

    results_5m = []  # trades using 5-min bars for position management
    results_1m = []  # trades using 1-min bars for position management

    equity_5m = config.INITIAL_CAPITAL
    equity_1m = config.INITIAL_CAPITAL

    for date in trading_days:
        date_key = date.date()
        if date_key not in gap_data or gap_data[date_key].empty:
            continue

        n_cands = len(gap_data[date_key])
        max_stocks = min(config.MAX_POSITIONS_PER_DAY, n_cands)

        for mode, equity_ref, results_ref, tl in [("5m", equity_5m, results_5m, time_limit_5m),
                                                     ("1m", equity_1m, results_1m, time_limit_1m)]:
            pos_per_stock = equity_ref / max_stocks if max_stocks > 0 else equity_ref
            pos_per_stock = min(pos_per_stock, config.MAX_POSITION_SIZE)
            candidates = gap_data[date_key].head(max_stocks)

            for _, row in candidates.iterrows():
                symbol = row["symbol"]
                open_price = row["open_price"]

                bars_5m_df = get_5min_bars(client, symbol, date)
                if bars_5m_df.empty or len(bars_5m_df) < 3:
                    continue
                bars_1m_df = get_1min_bars(client, symbol, date)

                # Same entry detection (1-min pullback) for both
                if bars_1m_df.empty or len(bars_1m_df) < 2:
                    from backtest import find_entry_with_confirmation
                    pullback, entry_bar_idx, confirmed = find_entry_with_confirmation(bars_5m_df, open_price)
                else:
                    pullback, entry_bar_idx_1m, confirmed = find_entry_with_confirmation_1min(bars_1m_df, open_price)

                if not confirmed or pullback <= 0:
                    continue
                if pullback >= open_price:
                    continue

                # ATR (5-min bars for both)
                entry_bar_idx_5m = locate_5min_bar_index(bars_5m_df, bars_1m_df.index[entry_bar_idx_1m]) if not bars_1m_df.empty and entry_bar_idx_1m >= 0 else entry_bar_idx
                bars_for_atr = []
                for j in range(min(entry_bar_idx_5m + 1, len(bars_5m_df))):
                    b = bars_5m_df.iloc[j]
                    bars_for_atr.append({"high": b["high"], "low": b["low"], "close": b["close"]})
                atr = calc_atr(bars_for_atr, period=14)

                stop_price = calc_stop_price(pullback, atr)
                stop_max_pct = getattr(config, "STOP_LOSS_MAX_PCT", 0)
                if stop_max_pct > 0:
                    min_stop = round(pullback * (1 - stop_max_pct), 2)
                    stop_price = max(stop_price, min_stop)

                plan = build_trade_plan(symbol, open_price, pullback, atr,
                                         position_size=min(pos_per_stock, config.MAX_POSITION_SIZE))

                # Position management: different bar resolutions
                if mode == "5m":
                    all_bars_list = _bars_to_list(bars_5m_df)
                    remaining_list = all_bars_list[entry_bar_idx_5m + 1:]
                else:
                    all_bars_list = _bars_to_list(bars_1m_df)
                    # Find 1-min entry bar index directly
                    remaining_list = all_bars_list[entry_bar_idx_1m + 1:] if not bars_1m_df.empty and entry_bar_idx_1m >= 0 else all_bars_list

                force_close_price = remaining_list[-1]["close"] if remaining_list else None

                trail_pcts = plan.trail_pcts
                result = evaluate_trade_stone(
                    plan, remaining_list, force_close_price,
                    time_limit_bars=tl,
                    trail_pct_75=trail_pcts[2] if len(trail_pcts) > 2 else 0.03,
                    trail_pct_1125=trail_pcts[4] if len(trail_pcts) > 4 else 0.04,
                    trail_pct_150=trail_pcts[5] if len(trail_pcts) > 5 else 0.05,
                )
                result.date = str(date_key)
                result.open_price = open_price
                results_ref.append(result)

                # Update equity for compounding
                shares = int(pos_per_stock / pullback)
                if shares <= 0:
                    shares = 1
                pnl_real = result.pnl * shares
                if mode == "5m":
                    equity_5m += pnl_real
                else:
                    equity_1m += pnl_real

    # Summary comparison
    print(f"\n{'='*80}")
    print(f" COMPARISON: 1-min vs 5-min bars for position management")
    print(f" Backtest period: {trading_days[0].date()} to {trading_days[-1].date()} ({n_days} days)")
    print(f" Entry detection: 1-min pullback (same for both)")
    print(f"{'='*80}")

    for label, results, eq in [("5-min bars", results_5m, equity_5m), ("1-min bars", results_1m, equity_1m)]:
        total_pnl = sum(r.pnl for r in results)
        wins = sum(1 for r in results if r.pnl > 0)
        losses = sum(1 for r in results if r.pnl <= 0)
        win_rate = wins / len(results) * 100 if results else 0
        avg_win = sum(r.pnl for r in results if r.pnl > 0) / wins if wins else 0
        avg_loss = sum(r.pnl for r in results if r.pnl <= 0) / losses if losses else 0
        pf = sum(r.pnl for r in results if r.pnl > 0) / max(abs(sum(r.pnl for r in results if r.pnl < 0)), 1)
        max_dd = max(r.pnl_pct for r in results) if results else 0
        min_dd = min(r.pnl_pct for r in results) if results else 0

        # Tier stats
        tier_counts = [0]*6
        for r in results:
            if r.partial_sells:
                for ti, (p, s) in enumerate(r.partial_sells):
                    if s > 0 and ti < 6:
                        tier_counts[ti] += 1

        # Exit reason stats
        exit_reasons = {}
        for r in results:
            reason = r.exit_reason
            exit_reasons.setdefault(reason, []).append(r)

        print(f"\n  ── {label} ──")
        print(f"  Trades: {len(results)} | Win rate: {win_rate:.1f}% | Final equity: $${eq:.2f}")
        print(f"  Total P&L: $${total_pnl:+.2f} | Avg win: $${avg_win:+.2f} | Avg loss: $${avg_loss:+.2f}")
        print(f"  Profit Factor: {pf:.2f}")
        print(f"  Best P&L%: {max_dd:+.2%} | Worst P&L%: {min_dd:+.2%}")
        print(f"  Tiers hit: {tier_counts} (total={sum(tier_counts)})")
        print(f"  Exit reasons:")
        for reason, trades in sorted(exit_reasons.items()):
            avg = sum(t.pnl for t in trades) / len(trades)
            print(f"    {reason}: {len(trades)} trades, avg P&L=$${avg:+.2f}")

    # Per-trade comparison
    print(f"\n{'='*80}")
    print(f" PER-TRADE DIFFERENCE (1min - 5min)")
    print(f"{'='*80}")
    diff_total = 0
    better_1m = 0
    better_5m = 0
    same = 0
    for i, (r5, r1) in enumerate(zip(results_5m, results_1m)):
        diff = r1.pnl - r5.pnl
        diff_total += diff
        if diff > 0.01:
            better_1m += 1
        elif diff < -0.01:
            better_5m += 1
        else:
            same += 1
        if abs(diff) > 0.5:  # Only show significant differences
            print(f"  {r5.date} {r5.entry_price:.4f}→{r5.exit_price:.4f} vs {r1.entry_price:.4f}→{r1.exit_price:.4f} "
                  f"5m={r5.pnl:+.2f}({r5.exit_reason}) 1m={r1.pnl:+.2f}({r1.exit_reason}) diff={diff:+.2f}")

    print(f"\n  Total P&L diff: $${diff_total:+.2f}")
    print(f"  1-min better: {better_1m} trades | 5-min better: {better_5m} trades | Same: {same}")
    pct_1m_better = better_1m / len(results_5m) * 100 if results_5m else 0
    print(f"  1-min wins {pct_1m_better:.1f}% of trades")

    # Conclusion
    if diff_total > 0:
        print(f"\n  CONCLUSION: 1-min bars for position management is BETTER (+$${diff_total:.2f})")
    else:
        print(f"\n  CONCLUSION: 5-min bars for position management is BETTER (+$${-diff_total:.2f})")


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    run_comparison(n_days=n)
