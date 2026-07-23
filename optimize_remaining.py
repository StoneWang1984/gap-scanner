"""Continue parameter sweep — only test remaining 3 parameters."""

import sys, os, json
sys.path.insert(0, os.path.dirname(__file__))
from optimize_params import run_backtest_with_params
from collections import defaultdict

REMAINING_GRID = {
    "TRAILING_STOP_PCTS": [
        [0.015, 0.02, 0.025, 0.03, 0.04, 0.05],
        [0.02, 0.025, 0.03, 0.035, 0.04, 0.05],
        [0.025, 0.03, 0.035, 0.04, 0.05, 0.06],
        [0.03, 0.035, 0.04, 0.05, 0.06, 0.07],
    ],
    "TARGET_CAP_TIERS": [
        [0.04, 0.08, 0.12, 0.18, 0.25, 0.35],
        [0.05, 0.10, 0.15, 0.20, 0.25, 0.35],
        [0.06, 0.12, 0.18, 0.24, 0.30, 0.40],
    ],
    "REENTRY_MIN_PULLBACK": [0.02, 0.03, 0.04, 0.05],
}

def sweep_remaining(n_days=30):
    import config

    print("=" * 80)
    print(" PARAMETER SWEEP — REMAINING 3 PARAMETERS")
    print(f" Backtest period: {n_days} trading days")
    print("=" * 80)

    baseline = run_backtest_with_params({}, n_days=n_days)
    if baseline:
        print(f"\nBaseline: Trades={baseline['n_trades']} | WR={baseline['win_rate']}% | "
              f"P&L=${baseline['total_pnl']} | PF={baseline['profit_factor']}")

    all_results = []

    for param_name, values in REMAINING_GRID.items():
        print(f"\n── Sweeping {param_name} ──")
        for value in values:
            current = getattr(config, param_name)
            if value == current:
                label = f"{param_name}=baseline"
                print(f"  {param_name}={value} — skipped (same as baseline)")
                continue

            label = f"{param_name}={value}"
            if isinstance(value, list):
                label = f"{param_name}=[{','.join(str(v) for v in value)}]"

            result = run_backtest_with_params({param_name: value}, n_days=n_days)
            if result:
                d_pnl = result["total_pnl"] - baseline["total_pnl"]
                d_pf = result["profit_factor"] - baseline["profit_factor"]
                d_wr = result["win_rate"] - baseline["win_rate"]
                print(f"  {label}: P&L=${result['total_pnl']}({d_pnl:+.2f}) "
                      f"PF={result['profit_factor']}({d_pf:+.2f}) "
                      f"WR={result['win_rate']}%({d_wr:+.1f}%)")
                all_results.append({"label": label, **result})

    # Rankings
    by_pnl = sorted([r for r in all_results if r.get("total_pnl")],
                    key=lambda x: x["total_pnl"], reverse=True)
    by_pf = sorted([r for r in all_results if r.get("profit_factor")],
                   key=lambda x: x["profit_factor"], reverse=True)

    print(f"\n{'='*80}")
    print(" TOP 10 BY P&L")
    print(f"{'='*80}")
    for i, r in enumerate(by_pnl[:10]):
        print(f"  {i+1}. {r['label']}: P&L=${r['total_pnl']}, PF={r['profit_factor']}, "
              f"WR={r['win_rate']}%, trades={r['n_trades']}")

    print(f"\n{'='*80}")
    print(" TOP 10 BY PROFIT FACTOR")
    print(f"{'='*80}")
    for i, r in enumerate(by_pf[:10]):
        print(f"  {i+1}. {r['label']}: PF={r['profit_factor']}, P&L=${r['total_pnl']}, "
              f"WR={r['win_rate']}%, trades={r['n_trades']}")

    # Save results
    output_file = os.path.join(os.path.dirname(__file__), "optimization_remaining_results.json")
    with open(output_file, "w") as f:
        json.dump({"baseline": baseline, "sweep_results": all_results}, f, indent=2)
    print(f"\nResults saved to {output_file}")

    return all_results, baseline

if __name__ == "__main__":
    n_days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    results, baseline = sweep_remaining(n_days=n_days)
