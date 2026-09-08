"""A/B comparison: isolate each strategy modification. Runs all variants sequentially."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from backtest import run_backtest
from collections import Counter


def run_and_stats(label):
    trades = run_backtest(n_days=63)
    total_pnl = sum(t.pnl for t in trades)
    first = [t for t in trades if t.trade_type == "first"]
    reentry = [t for t in trades if t.trade_type == "reentry"]
    wins = [t for t in trades if t.pnl > 0]
    return {
        "label": label,
        "equity": 1000 + total_pnl,
        "pnl": total_pnl,
        "n": len(trades),
        "win_rate": len(wins)/len(trades)*100 if trades else 0,
        "first_reasons": dict(Counter(t.exit_reason for t in first)),
        "reentry_reasons": dict(Counter(t.exit_reason for t in reentry)),
        "first_pnl": sum(t.pnl for t in first),
        "reentry_pnl": sum(t.pnl for t in reentry),
    }


print("=" * 60)
print("A: 0.4.12 基线")
config.FIRST_TRADE_TIME_LIMIT_BARS = 8
config.REENTRY_TIME_LIMIT_BARS = 12
rA = run_and_stats("0.4.12基线")

print("\n" + "=" * 60)
print("B: 关闭40分钟首次时间限制")
config.FIRST_TRADE_TIME_LIMIT_BARS = 0
config.REENTRY_TIME_LIMIT_BARS = 12
rB = run_and_stats("无40min限制")

print("\n" + "=" * 60)
print("C: 关闭1小时再入场时间限制")
config.FIRST_TRADE_TIME_LIMIT_BARS = 8
config.REENTRY_TIME_LIMIT_BARS = 0
rC = run_and_stats("无1hr再入场限制")

print("\n" + "=" * 60)
print("D: 关闭所有时间限制")
config.FIRST_TRADE_TIME_LIMIT_BARS = 0
config.REENTRY_TIME_LIMIT_BARS = 0
rD = run_and_stats("无时间限制")

# Reset to baseline for next test
config.FIRST_TRADE_TIME_LIMIT_BARS = 8
config.REENTRY_TIME_LIMIT_BARS = 12

# ── Summary ──
results = [rA, rB, rC, rD]
print("\n" + "=" * 80)
print("对比总结")
print("=" * 80)
print(f"{'测试':<20} {'最终权益':>12} {'总P&L':>12} {'交易':>6} {'胜率':>7} {'首次P&L':>10} {'再入P&L':>10} {'vs基线':>10}")
print("-" * 80)
base = rA["equity"]
for r in results:
    diff = r["equity"] - base
    sign = "+" if diff >= 0 else ""
    print(f"{r['label']:<20} ${r['equity']:>10,.0f} ${r['pnl']:>10,.0f} {r['n']:>4} {r['win_rate']:>5.1f}% ${r['first_pnl']:>8,.0f} ${r['reentry_pnl']:>8,.0f} {sign}{diff:>+9,.0f}")

print("\n── 首次交易退出原因 ──")
all_reasons = set()
for r in results:
    all_reasons.update(r["first_reasons"].keys())
print(f"{'原因':<25}", end="")
for r in results:
    print(f" {r['label'][:8]:>8}", end="")
print()
for reason in sorted(all_reasons):
    print(f"{reason:<25}", end="")
    for r in results:
        print(f" {r['first_reasons'].get(reason, 0):>8}", end="")
    print()

print("\n── 再入场退出原因 ──")
all_reasons = set()
for r in results:
    all_reasons.update(r["reentry_reasons"].keys())
print(f"{'原因':<25}", end="")
for r in results:
    print(f" {r['label'][:8]:>8}", end="")
print()
for reason in sorted(all_reasons):
    print(f"{reason:<25}", end="")
    for r in results:
        print(f" {r['reentry_reasons'].get(reason, 0):>8}", end="")
    print()
