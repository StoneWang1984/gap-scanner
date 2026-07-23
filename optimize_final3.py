"""Sweep only the 3 remaining variations."""

import sys, os, json
sys.path.insert(0, os.path.dirname(__file__))
from optimize_params import run_backtest_with_params

REMAINING = {
    "TARGET_CAP_TIERS": [
        [0.06, 0.12, 0.18, 0.24, 0.30, 0.40],
    ],
    "REENTRY_MIN_PULLBACK": [0.02, 0.04, 0.05],
}

def sweep_final(n_days=30):
    import config

    print("=" * 80)
    print(" FINAL 3 VARIATIONS")
    print("=" * 80)

    baseline = run_backtest_with_params({}, n_days=n_days)
    if baseline:
        print(f"\nBaseline: Trades={baseline['n_trades']} | WR={baseline['win_rate']}% | "
              f"P&L=${baseline['total_pnl']} | PF={baseline['profit_factor']}")

    all_results = []

    for param_name, values in REMAINING.items():
        print(f"\n── Sweeping {param_name} ──")
        for value in values:
            current = getattr(config, param_name)
            if value == current:
                print(f"  {param_name}={value} — skipped (baseline)")
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

    by_pnl = sorted(all_results, key=lambda x: x["total_pnl"], reverse=True)
    print(f"\n{'='*80}")
    print(" RANKING BY P&L")
    print(f"{'='*80}")
    for i, r in enumerate(by_pnl):
        print(f"  {i+1}. {r['label']}: P&L=${r['total_pnl']}, PF={r['profit_factor']}, "
              f"WR={r['win_rate']}%, trades={r['n_trades']}")

    output_file = os.path.join(os.path.dirname(__file__), "optimization_final_results.json")
    with open(output_file, "w") as f:
        json.dump({"baseline": baseline, "results": all_results}, f, indent=2)
    print(f"\nResults saved to {output_file}")
    return all_results, baseline

if __name__ == "__main__":
    n_days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    sweep_final(n_days=n_days)
