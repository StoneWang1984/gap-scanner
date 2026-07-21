"""Compare different tier configurations to find optimal number of tiers.

Tests 3/4/6/8/10/12 tiers over the same backtest period.
Each config keeps total sell ratio at 75%, distributes equally across tiers.
"""

import sys
import copy
import config
from backtest import run_backtest


# ── Tier configurations ─────────────────────────────────────────────
# All configs sell 75% in tiers + 25% trailing stop
# Retracement tiers spread evenly from 0.125 to 1.5
# Caps increase linearly from 3% to 35%
# Trailing stops increase linearly from 2% to 5%

def make_tier_config(n_tiers):
    """Generate tier parameters for n_tiers."""
    # Retracement: evenly spaced from ~12.5% to 150%
    if n_tiers == 1:
        retracements = [0.75]
    else:
        retracements = [round(0.125 + i * (1.5 - 0.125) / (n_tiers - 1), 3) for i in range(n_tiers)]

    # Caps: linearly from 3% to 35%
    if n_tiers == 1:
        caps = [0.15]
    else:
        caps = [round(0.03 + i * (0.35 - 0.03) / (n_tiers - 1), 3) for i in range(n_tiers)]

    # Sell ratios: equal across tiers, total = 0.75
    sell_ratio = round(0.75 / n_tiers, 4)
    sell_ratios = [sell_ratio] * n_tiers

    # Trailing stops: linearly from 2% to 5%
    if n_tiers == 1:
        trail_pcts = [0.03]
    else:
        trail_pcts = [round(0.02 + i * (0.05 - 0.02) / (n_tiers - 1), 3) for i in range(n_tiers)]

    return {
        "PROFIT_RETRACEMENT_TIERS": retracements,
        "TARGET_CAP_TIERS": caps,
        "PARTIAL_SELL_RATIOS": sell_ratios,
        "TRAILING_STOP_PCTS": trail_pcts,
    }


CONFIGS = {
    "3-tier": make_tier_config(3),
    "4-tier": make_tier_config(4),
    "6-tier (current)": make_tier_config(6),
    "8-tier": make_tier_config(8),
    "10-tier": make_tier_config(10),
    "12-tier": make_tier_config(12),
}


def run_comparison():
    # Shorten backtest for comparison speed
    n_days = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    config.BACKTEST_DAYS = n_days

    # Save original config values
    orig = {
        "PROFIT_RETRACEMENT_TIERS": config.PROFIT_RETRACEMENT_TIERS,
        "TARGET_CAP_TIERS": config.TARGET_CAP_TIERS,
        "PARTIAL_SELL_RATIOS": config.PARTIAL_SELL_RATIOS,
        "TRAILING_STOP_PCTS": config.TRAILING_STOP_PCTS,
    }

    results = {}

    for name, tier_cfg in CONFIGS.items():
        print(f"\n{'='*70}")
        print(f"  Testing: {name}")
        print(f"{'='*70}")

        # Apply config
        for k, v in tier_cfg.items():
            setattr(config, k, v)

        # Print tier details
        rets = tier_cfg["PROFIT_RETRACEMENT_TIERS"]
        caps = tier_cfg["TARGET_CAP_TIERS"]
        sells = tier_cfg["PARTIAL_SELL_RATIOS"]
        trails = tier_cfg["TRAILING_STOP_PCTS"]
        print(f"  Retracements: {rets}")
        print(f"  Caps:         {caps}")
        print(f"  Sell ratios:  {[round(s, 3) for s in sells]} (total={sum(sells):.2f})")
        print(f"  Trail pcts:   {trails}")

        try:
            trades = run_backtest()
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

        # Analyze results
        if not trades:
            print(f"  No trades.")
            results[name] = None
            continue

        pnls = [t.pnl for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        total_pnl = sum(pnls)

        # Win rate
        win_rate = len(wins) / len(pnls) if pnls else 0

        # Average win / average loss
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float('inf')

        # Max drawdown from equity curve
        equity = config.INITIAL_CAPITAL
        peak = equity
        max_dd = 0
        for t in trades:
            equity += t.pnl
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)

        # Exit reason distribution
        from collections import Counter
        exit_reasons = Counter(t.exit_reason for t in trades)

        # Tiers reached distribution
        tier_reached = [0] * len(rets)
        tier_sold_shares = [0] * len(rets)
        for t in trades:
            if t.partial_sells:
                for i, (price, shares) in enumerate(t.partial_sells):
                    if i < len(tier_reached) and shares > 0:
                        tier_reached[i] += 1
                        tier_sold_shares[i] += shares

        results[name] = {
            "n_tiers": len(rets),
            "total_trades": len(trades),
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(win_rate, 3),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else "inf",
            "max_drawdown": round(max_dd, 3),
            "exit_reasons": dict(exit_reasons),
            "tier_reached": tier_reached,
            "tier_sold_shares": tier_sold_shares,
            "final_equity": round(equity, 2),
        }

        r = results[name]
        print(f"\n  Results: P&L=${r['total_pnl']:,.2f} | Win rate={r['win_rate']:.1%} | "
              f"PF={r['profit_factor']} | MaxDD={r['max_drawdown']:.1%}")
        print(f"  Avg win=${r['avg_win']:,.2f} | Avg loss=${r['avg_loss']:,.2f}")
        print(f"  Exit reasons: {r['exit_reasons']}")
        print(f"  Tiers reached: {r['tier_reached']}")
        print(f"  Tiers shares sold: {r['tier_sold_shares']}")

    # Restore original config
    for k, v in orig.items():
        setattr(config, k, v)

    # ── Summary table ──
    print(f"\n\n{'='*90}")
    print(f"  TIER COMPARISON SUMMARY (backtest {config.BACKTEST_DAYS} days)")
    print(f"{'='*90}")
    print(f"{'Config':<16} {'P&L':>10} {'Win%':>7} {'PF':>6} {'MaxDD':>7} {'AvgWin':>9} {'AvgLoss':>9} {'Trades':>7}")
    print(f"{'-'*16} {'-'*10} {'-'*7} {'-'*6} {'-'*7} {'-'*9} {'-'*9} {'-'*7}")

    for name, r in results.items():
        if r is None:
            continue
        pf_str = f"{r['profit_factor']:.2f}" if isinstance(r['profit_factor'], float) else str(r['profit_factor'])
        print(f"{name:<16} ${r['total_pnl']:>8,.2f} {r['win_rate']:>6.1%} {pf_str:>6} {r['max_drawdown']:>6.1%} "
              f"${r['avg_win']:>7,.2f} ${r['avg_loss']:>7,.2f} {r['total_trades']:>7}")

    # ── Tier utilization detail ──
    print(f"\n  TIER UTILIZATION (how often each tier is reached)")
    for name, r in results.items():
        if r is None:
            continue
        total = r['total_trades']
        pcts = [f"{c/total*100:.0f}%" if total > 0 else "0%" for c in r['tier_reached']]
        print(f"  {name:<16} {' → '.join(pcts)}")

    print(f"\n  KEY: Earlier tiers (left) are reached more often; later tiers (right) capture bigger moves.")
    print(f"  Optimal tier count balances: more tiers = finer profit capture vs. diminishing returns & complexity.")


if __name__ == "__main__":
    run_comparison()
