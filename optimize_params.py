"""Backtest parameter optimization — sweep critical parameters and analyze with local model.

Usage:
    python3 optimize_params.py [n_days]

Then ask qwen2.5:72b via Ollama MCP to analyze results and suggest improvements.
"""

import sys, os, json, copy, itertools
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))

# ── Parameter grid ────────────────────────────────────────────────
# Tier 1: parameters most impactful on P&L

PARAM_GRID = {
    "STOP_LOSS_ATR_MULT":       [1.5, 2.0, 2.5, 3.0],
    "STOP_LOSS_MAX_PCT":        [0.05, 0.08, 0.10, 0.12, 0.15],
    "FIRST_TRADE_TIME_LIMIT_BARS": [0, 6, 8, 10, 12],
    "GAP_THRESHOLD":            [0.05, 0.08, 0.10, 0.12, 0.15],
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
    "REENTRY_MIN_PULLBACK":     [0.02, 0.03, 0.04, 0.05],
}


def run_backtest_with_params(params_override, n_days=30):
    """Run backtest with modified parameters, return summary stats."""
    import config
    originals = {}
    for key, value in params_override.items():
        originals[key] = getattr(config, key)
        setattr(config, key, value)

    try:
        from backtest import run_backtest
        results = run_backtest(n_days=n_days)
    finally:
        for key, value in originals.items():
            setattr(config, key, value)

    if not results:
        return None

    pnls = [r.pnl for r in results]
    total_pnl = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    n_trades = len(results)
    win_rate = wins / n_trades * 100 if n_trades else 0
    avg_win = sum(p for p in pnls if p > 0) / wins if wins else 0
    avg_loss = sum(p for p in pnls if p <= 0) / losses if losses else 0
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p <= 0))
    profit_factor = gross_profit / max(gross_loss, 1)

    exit_reasons = defaultdict(int)
    for r in results:
        exit_reasons[r.exit_reason] += 1

    return {
        "n_trades": n_trades,
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(win_rate, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "exit_reasons": dict(exit_reasons),
        "params": {k: (list(v) if isinstance(v, list) else v) for k, v in params_override.items()},
    }


def parameter_sweep(n_days=30):
    """Test each parameter independently, one at a time."""
    import config

    print("=" * 80)
    print(" PARAMETER SWEEP OPTIMIZATION")
    print(f" Backtest period: {n_days} trading days")
    print("=" * 80)

    # Baseline
    print("\n── Baseline (current parameters) ──")
    baseline = run_backtest_with_params({}, n_days=n_days)
    if baseline:
        print(f"  Trades: {baseline['n_trades']} | Win rate: {baseline['win_rate']}% | "
              f"P&L: ${baseline['total_pnl']} | PF: {baseline['profit_factor']}")

    all_results = [{"label": "baseline", **baseline}]

    for param_name, values in PARAM_GRID.items():
        print(f"\n── Sweeping {param_name} ──")
        for value in values:
            current = getattr(config, param_name)
            if value == current:
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

    # Format for model analysis
    analysis_text = format_for_model(all_results, baseline)

    # Save JSON results
    output_file = os.path.join(os.path.dirname(__file__), "optimization_results.json")
    with open(output_file, "w") as f:
        json.dump({"baseline": baseline, "sweep_results": all_results}, f, indent=2)
    print(f"\nResults saved to {output_file}")

    return all_results, baseline, analysis_text


def format_for_model(results, baseline):
    """Format results as text for LLM analysis."""
    lines = [
        "STONE 1.0 BACKTEST PARAMETER OPTIMIZATION RESULTS",
        "",
        f"Baseline: {baseline['n_trades']} trades, P&L=${baseline['total_pnl']}, "
        f"PF={baseline['profit_factor']}, WR={baseline['win_rate']}%",
        "",
        "PARAMETER VARIATIONS:",
        f"{'Label':40s} | {'Trades':>6} | {'P&L':>8} | {'ΔP&L':>8} | {'PF':>6} | {'ΔPF':>6} | {'WR':>5} | {'ΔWR':>6}",
        "-" * 100,
    ]

    for r in results:
        if not r.get("total_pnl"):
            continue
        d_pnl = r["total_pnl"] - baseline["total_pnl"]
        d_pf = r["profit_factor"] - baseline["profit_factor"]
        d_wr = r["win_rate"] - baseline["win_rate"]
        lines.append(f"{r['label']:40s} | {r['n_trades']:6d} | ${r['total_pnl']:7.2f} | "
                     f"${d_pnl:+7.2f} | {r['profit_factor']:5.2f} | {d_pf:+5.2f} | "
                     f"{r['win_rate']:4.1f}% | {d_wr:+5.1f}%")

    lines += [
        "",
        "ANALYSIS QUESTIONS:",
        "1. Which parameter changes most improve P&L and profit factor?",
        "2. Which parameters show a clear optimal value (not just marginal improvement)?",
        "3. Are there parameters where the current value is already optimal?",
        "4. What parameter combinations should be tested next (multi-parameter grid)?",
        "5. Are there trade-offs between P&L and win rate?",
        "6. Give top 3 recommended parameter changes with specific values.",
        "",
        "CURRENT CONFIG VALUES:",
        f"  STOP_LOSS_ATR_MULT = 2.0",
        f"  STOP_LOSS_MAX_PCT = 0.10",
        f"  FIRST_TRADE_TIME_LIMIT_BARS = 8",
        f"  GAP_THRESHOLD = 0.10",
        f"  TRAILING_STOP_PCTS = [0.02, 0.025, 0.03, 0.035, 0.04, 0.05]",
        f"  TARGET_CAP_TIERS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.35]",
        f"  REENTRY_MIN_PULLBACK = 0.03",
    ]

    return "\n".join(lines)


if __name__ == "__main__":
    n_days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    results, baseline, analysis = parameter_sweep(n_days=n_days)
