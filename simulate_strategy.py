"""Simulate stock price curves to verify the 6-tier trading strategy.

Generates synthetic 5-min bar data with configurable scenarios,
then runs the strategy evaluation to produce P&L and charts.

Usage:
    python3 simulate_strategy.py              # All scenarios
    python3 simulate_strategy.py --chart       # Show charts
    python3 simulate_strategy.py --scenario steady_rise
"""

import sys
import math
import random
from dataclasses import dataclass
import pandas as pd

import config
from strategy import (
    TradePlan, evaluate_trade_stone,
    calc_stop_price, calc_position_size,
)


# ── Price curve generators ──────────────────────────────────────────

@dataclass
class Scenario:
    name: str
    description: str
    # Parameters for price generation
    gap_pct: float = 0.10        # gap-up at open
    prev_close: float = 10.0
    vol_pct: float = 0.02        # intraday volatility (std dev of returns per bar)
    trend: float = 0.0           # drift per bar (positive = rising, negative = falling)
    reversal_bar: int = 0        # bar index where trend reverses (0 = no reversal)
    reversal_trend: float = 0.0  # trend after reversal
    n_bars: int = 78             # 6.5 hours * 12 bars/hour = 78 five-min bars
    spike_bar: int = 0           # bar with sudden spike
    spike_pct: float = 0.0       # spike magnitude


SCENARIOS = {
    "steady_rise": Scenario(
        name="steady_rise", description="持续上涨 — 股价稳步上升",
        gap_pct=0.10, vol_pct=0.008, trend=0.002,
    ),
    "gap_and_reverse": Scenario(
        name="gap_and_reverse", description="跳空后回落 — gap up then sell off",
        gap_pct=0.12, vol_pct=0.012, trend=-0.002,
    ),
    "spike_then_crash": Scenario(
        name="spike_then_crash", description="暴涨后暴跌 — spike up then crash",
        gap_pct=0.10, vol_pct=0.01, trend=0.001,
        spike_bar=12, spike_pct=0.08,
        reversal_bar=15, reversal_trend=-0.005,
    ),
    "choppy_sideways": Scenario(
        name="choppy_sideways", description="震荡横盘 — choppy, no direction",
        gap_pct=0.10, vol_pct=0.02, trend=0.0,
    ),
    "slow_bleed": Scenario(
        name="slow_bleed", description="缓慢下跌 — slow decline to stop loss",
        gap_pct=0.10, vol_pct=0.01, trend=-0.003,
    ),
    "v_shape_recovery": Scenario(
        name="v_shape_recovery", description="V形反转 — drop then strong recovery",
        gap_pct=0.10, vol_pct=0.012, trend=-0.003,
        reversal_bar=30, reversal_trend=0.004,
    ),
    "double_top": Scenario(
        name="double_top", description="双顶 — rise, pullback, rise again, then fall",
        gap_pct=0.10, vol_pct=0.01, trend=0.002,
        reversal_bar=25, reversal_trend=-0.001,
        spike_bar=40, spike_pct=0.04,  # second top
    ),
    "big_gap_momentum": Scenario(
        name="big_gap_momentum", description="大跳空动量 — large gap, strong momentum",
        gap_pct=0.25, vol_pct=0.015, trend=0.003,
    ),
}


def generate_bars(scenario: Scenario, seed: int = 42) -> list[dict]:
    """Generate synthetic 5-min bars for a given scenario."""
    rng = random.Random(seed)
    open_price = scenario.prev_close * (1 + scenario.gap_pct)
    price = open_price

    bars = []
    for i in range(scenario.n_bars):
        # Determine current trend
        current_trend = scenario.trend
        if scenario.reversal_bar and i >= scenario.reversal_bar:
            current_trend = scenario.reversal_trend

        # Apply spike
        spike = 0
        if scenario.spike_bar and i == scenario.spike_bar:
            spike = scenario.spike_pct

        # Generate bar
        ret = current_trend + rng.gauss(0, scenario.vol_pct) + spike
        close = price * (1 + ret)
        close = max(close, 0.01)

        high = max(price, close) * (1 + abs(rng.gauss(0, scenario.vol_pct * 0.3)))
        low = min(price, close) * (1 - abs(rng.gauss(0, scenario.vol_pct * 0.3)))
        low = max(low, 0.01)

        bars.append({
            "timestamp": pd.Timestamp(f"2026-07-22 09:30") + pd.Timedelta(minutes=i*5),
            "open": round(price, 4),
            "high": round(high, 4),
            "low": round(low, 4),
            "close": round(close, 4),
            "volume": rng.randint(10000, 100000),
        })
        price = close

    return bars


def run_scenario(scenario: Scenario, shares: int = 8, seed: int = 42, pullback_pct: float = 0.015) -> dict:
    """Run strategy evaluation on a synthetic scenario.

    pullback_pct: how far below open_price the entry occurs (simulates pullback entry).
    """
    from strategy import build_trade_plan

    bars = generate_bars(scenario, seed)
    open_price = bars[0]["open"]
    prev_close = scenario.prev_close

    # Find pullback entry: lowest low in first few bars below open
    pullback = open_price
    for b in bars[:12]:  # first hour
        if b["low"] < pullback:
            pullback = b["low"]
    # Ensure pullback is at least pullback_pct below open
    pullback = min(pullback, open_price * (1 - pullback_pct))

    atr = scenario.prev_close * scenario.vol_pct * math.sqrt(78)
    position_size = pullback * shares

    plan = build_trade_plan(
        symbol=f"SIM_{scenario.name}",
        open_price=open_price,
        pullback=pullback,
        atr=atr,
        position_size=position_size,
    )

    # Find entry bar index (first bar whose low <= pullback)
    entry_bar_idx = 0
    for i, b in enumerate(bars):
        if b["low"] <= pullback:
            entry_bar_idx = i
            break

    # Bars after entry (same as backtest: bars_after_entry starts from entry bar)
    bars_after_entry = bars[entry_bar_idx:]
    trail_pcts = plan.trail_pcts
    result = evaluate_trade_stone(
        plan, bars_after_entry,
        force_close_price=bars_after_entry[-1]["close"],
        trail_pct_75=trail_pcts[2] if len(trail_pcts) > 2 else 0.03,
        trail_pct_1125=trail_pcts[4] if len(trail_pcts) > 4 else 0.04,
        trail_pct_150=trail_pcts[5] if len(trail_pcts) > 5 else 0.05,
    )

    return {
        "scenario": scenario,
        "bars": bars,
        "plan": plan,
        "result": result,
    }


def print_results(results: list[dict]):
    """Print summary table of all scenarios."""
    print(f"\n{'Scenario':<22} {'Description':<28} {'P&L':>8} {'P&L%':>7} {'Exit':>20} {'Tiers':>6}")
    print("-" * 95)
    for r in results:
        s = r["scenario"]
        res = r["result"]
        tiers_hit = sum(1 for p, sh in res.partial_sells if sh > 0) if res.partial_sells else 0
        print(f"{s.name:<22} {s.description:<28} "
              f"${res.pnl:>+7.2f} {res.pnl_pct:>+6.1%} {res.exit_reason:>20} {tiers_hit:>6}")


def generate_chart(results: list[dict], output_path: str = "versions/sim_strategy.html"):
    """Generate interactive HTML chart with price curves and tier markers."""
    html_parts = []
    html_parts.append("""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Stone 1.1 Strategy Simulation</title>
<style>
body { font-family: monospace; background: #1a1a2e; color: #e0e0e0; margin: 20px; }
h1 { color: #00d4ff; }
h2 { color: #ff9500; margin-top: 30px; }
.chart { position: relative; height: 300px; margin: 10px 0 30px 0; border: 1px solid #333; padding: 5px; }
canvas { width: 100%; height: 100%; }
.stats { display: inline-block; background: #2a2a4a; padding: 8px 12px; margin: 4px; border-radius: 4px; }
.positive { color: #00ff88; }
.negative { color: #ff4444; }
.tier-line { position: absolute; left: 0; right: 0; border-top: 1px dashed; opacity: 0.7; font-size: 10px; padding-left: 2px; }
.stop-line { border-top-color: #ff4444; color: #ff4444; }
.target-line { border-top-color: #00ff88; color: #00ff88; }
.entry-line { border-top-color: #ffaa00; color: #ffaa00; }
.summary { background: #2a2a4a; padding: 15px; border-radius: 8px; margin: 20px 0; }
</style></head><body>
<h1>Stone 1.1 — 6-Tier Strategy Simulation</h1>
""")

    # Summary table
    html_parts.append('<div class="summary"><table><tr>')
    html_parts.append('<th>Scenario</th><th>P&L</th><th>P&L%</th><th>Exit Reason</th><th>Tiers Hit</th></tr>')
    for r in results:
        s = r["scenario"]
        res = r["result"]
        tiers_hit = sum(1 for p, sh in res.partial_sells if sh > 0) if res.partial_sells else 0
        cls = "positive" if res.pnl >= 0 else "negative"
        html_parts.append(f'<tr><td>{s.name}</td><td class="{cls}">${res.pnl:+.2f}</td>'
                          f'<td class="{cls}">{res.pnl_pct:+.1%}</td><td>{res.exit_reason}</td>'
                          f'<td>{tiers_hit}/6</td></tr>')
    html_parts.append('</table></div>')

    # Charts (using simple SVG)
    for r in results:
        s = r["scenario"]
        res = r["result"]
        plan = r["plan"]
        bars = r["bars"]

        html_parts.append(f'<h2>{s.name} — {s.description}</h2>')

        # Find price range
        all_prices = []
        for b in bars:
            all_prices.extend([b["high"], b["low"]])
        min_p = min(all_prices) * 0.98
        max_p = max(all_prices) * 1.02

        def y_pos(p):
            return 280 - (p - min_p) / (max_p - min_p) * 260

        def x_pos(i):
            return 20 + i * (760 / len(bars))

        # SVG chart
        svg = f'<svg width="800" height="300" style="background:#1a1a2e">'

        # Stop loss line
        svg += f'<line x1="20" y1="{y_pos(plan.stop_price)}" x2="780" y2="{y_pos(plan.stop_price)}" stroke="#ff4444" stroke-dasharray="4,4" opacity="0.6"/>'
        svg += f'<text x="782" y="{y_pos(plan.stop_price)+4}" fill="#ff4444" font-size="9">Stop ${plan.stop_price:.2f}</text>'

        # Entry line
        svg += f'<line x1="20" y1="{y_pos(plan.pullback)}" x2="780" y2="{y_pos(plan.pullback)}" stroke="#ffaa00" stroke-dasharray="4,4" opacity="0.6"/>'
        svg += f'<text x="782" y="{y_pos(plan.pullback)+4}" fill="#ffaa00" font-size="9">Entry ${plan.pullback:.2f}</text>'

        # Target lines
        colors = ["#66ff66", "#44dd44", "#22bb22", "#009900", "#007700", "#005500"]
        for ti, t in enumerate(plan.targets):
            c = colors[ti % len(colors)]
            svg += f'<line x1="20" y1="{y_pos(t)}" x2="780" y2="{y_pos(t)}" stroke="{c}" stroke-dasharray="2,4" opacity="0.5"/>'
            svg += f'<text x="782" y="{y_pos(t)+4}" fill="{c}" font-size="9">T{ti+1} ${t:.2f}</text>'

        # Price line
        points = []
        for i, b in enumerate(bars):
            points.append(f'{x_pos(i)},{y_pos(b["close"])}')
        svg += f'<polyline points="{" ".join(points)}" fill="none" stroke="#00d4ff" stroke-width="1.5"/>'

        # Exit marker
        if res.exit_bar_idx >= 0 and res.exit_bar_idx < len(bars):
            ex = x_pos(res.exit_bar_idx)
            ey = y_pos(res.exit_price)
            svg += f'<circle cx="{ex}" cy="{ey}" r="5" fill="#ff4444" stroke="white"/>'
            svg += f'<text x="{ex+8}" y="{ey-5}" fill="white" font-size="9">{res.exit_reason} ${res.exit_price:.2f}</text>'

        # Partial sell markers
        if res.partial_sells:
            for ti, (p, sh) in enumerate(res.partial_sells):
                if sh > 0:
                    # Find bar where this price was reached
                    for bi, b in enumerate(bars):
                        if b["high"] >= p:
                            sx = x_pos(bi)
                            sy = y_pos(p)
                            svg += f'<circle cx="{sx}" cy="{sy}" r="3" fill="#00ff88"/>'
                            break

        svg += '</svg>'
        html_parts.append(svg)

        # Trade details
        cls = "positive" if res.pnl >= 0 else "negative"
        html_parts.append(f'<div class="stats">Entry: ${plan.pullback:.2f} | '
                          f'Exit: ${res.exit_price:.2f} | '
                          f'<span class="{cls}">P&L: ${res.pnl:+.2f} ({res.pnl_pct:+.1%})</span> | '
                          f'Reason: {res.exit_reason}</div>')

    html_parts.append('</body></html>')

    with open(output_path, "w") as f:
        f.write("\n".join(html_parts))
    print(f"\nChart saved to {output_path}")


def main():
    show_chart = "--chart" in sys.argv
    scenario_name = None
    for arg in sys.argv[1:]:
        if arg != "--chart" and arg in SCENARIOS:
            scenario_name = arg

    scenarios = [SCENARIOS[scenario_name]] if scenario_name else list(SCENARIOS.values())

    print("=" * 95)
    print("  Stone 1.1 Strategy Simulation — Synthetic Price Curves")
    print("=" * 95)
    print(f"  Tiers: {len(config.PROFIT_RETRACEMENT_TIERS)} | "
          f"Retracements: {config.PROFIT_RETRACEMENT_TIERS}")
    print(f"  Caps: {config.TARGET_CAP_TIERS}")
    print(f"  Sell ratios: {[round(r, 3) for r in config.PARTIAL_SELL_RATIOS]} (total={sum(config.PARTIAL_SELL_RATIOS):.2f})")
    print(f"  Trail pcts: {config.TRAILING_STOP_PCTS}")
    print(f"  Shares per trade: 8 (FORCE_QTY)")
    print("=" * 95)

    results = []
    for s in scenarios:
        # Run with multiple seeds for robustness
        seed_results = []
        for seed in [42, 123, 456, 789, 1000]:
            r = run_scenario(s, shares=8, seed=seed)
            seed_results.append(r)

        # Use seed 42 as the representative
        results.append(seed_results[0])

        print(f"\n  ── {s.name}: {s.description} ──")
        print(f"  Gap: +{s.gap_pct:.0%} | Vol: {s.vol_pct:.1%}/bar | Trend: {s.trend:+.3f}/bar")

        # Aggregate across seeds
        pnls = [sr["result"].pnl for sr in seed_results]
        avg_pnl = sum(pnls) / len(pnls)
        win_rate = sum(1 for p in pnls if p > 0) / len(pnls)
        print(f"  5-seed avg P&L: ${avg_pnl:+.2f} | Win rate: {win_rate:.0%}")
        for i, sr in enumerate(seed_results):
            res = sr["result"]
            tiers_hit = sum(1 for p, sh in res.partial_sells if sh > 0) if res.partial_sells else 0
            sd = [42,123,456,789,1000][i]
            print(f"    seed{sd:3d}: P&L=${res.pnl:+7.2f} ({res.pnl_pct:+6.1%}) exit={res.exit_reason:<20s} tiers={tiers_hit}/6")

    print_results(results)

    if show_chart or "--chart" not in sys.argv:
        generate_chart(results)


if __name__ == "__main__":
    main()
