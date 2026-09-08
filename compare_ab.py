"""A/B comparison: isolate the effect of each strategy modification.

Tests:
A. 0.4.12 baseline (all changes)
B. No 40-min first trade time limit
C. No 1-hour re-entry time limit
D. No time limits at all
E. 0.4.11 style re-entry (retracement-based targets, trailing, breakeven)
"""

import importlib
import sys
import os

# Load config module
_ver_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "versions")
_parent_dir = os.path.dirname(os.path.abspath(__file__))
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

from backtest import run_backtest

def run_variant(label, config_overrides):
    """Run a backtest variant with overridden config values."""
    import config
    # Save originals
    originals = {}
    for key, val in config_overrides.items():
        originals[key] = getattr(config, key, None)
        setattr(config, key, val)

    trades = run_backtest(n_days=63)

    # Calculate stats
    total_pnl = sum(t.pnl for t in trades)
    first_trades = [t for t in trades if t.trade_type == "first"]
    reentry_trades = [t for t in trades if t.trade_type == "reentry"]
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]

    # Count exit reasons
    from collections import Counter
    first_reasons = Counter(t.exit_reason for t in first_trades)
    reentry_reasons = Counter(t.exit_reason for t in reentry_trades)

    # Restore originals
    for key, val in originals.items():
        if val is None:
            if hasattr(config, key):
                delattr(config, key)
        else:
            setattr(config, key, val)

    return {
        "label": label,
        "equity": 1000 + total_pnl,
        "total_pnl": total_pnl,
        "total_trades": len(trades),
        "first_trades": len(first_trades),
        "reentry_trades": len(reentry_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "first_reasons": dict(first_reasons),
        "reentry_reasons": dict(reentry_reasons),
    }

def run_variant_011_reentry(label):
    """Run with 0.4.11 style re-entry: retracement target, trailing stop, breakeven.
    This requires patching evaluate_reentry_trade to old behavior.
    """
    import config
    from strategy import evaluate_reentry_trade, TradeResult

    # Save and set config for old re-entry style
    orig_pct1 = getattr(config, "REENTRY_PROFIT_PCT_1", 0.03)
    orig_pct2 = getattr(config, "REENTRY_PROFIT_PCT_2", 0.05)
    orig_ratio = getattr(config, "REENTRY_PROFIT_PCT_1", 0.5)
    orig_tl = getattr(config, "REENTRY_TIME_LIMIT_BARS", 12)

    # Override evaluate_reentry_trade with old logic
    import strategy
    old_func = strategy.evaluate_reentry_trade

    def old_evaluate_reentry(entry_price, prev_high, shares, symbol, open_price,
                              bars_after_entry, force_close_price=None,
                              stop_price=None, **kwargs):
        """0.4.11 style: 5% stop, sell 1/3 at 150% retracement target, 5% trailing."""
        sp = stop_price if stop_price else round(entry_price * (1 - config.REENTRY_STOP_PCT), 2)
        target = round(entry_price + config.REENTRY_PROFIT_RETRACEMENT * (prev_high - entry_price), 2)

        highest = entry_price
        reached_target = False
        sold_partial = False
        partial_sell_price = 0.0
        partial_sell_shares = 0
        remaining_shares = shares

        def _make_result(reason, exit_price, bi):
            pnl_partial = (partial_sell_price - entry_price) * partial_sell_shares if sold_partial else 0
            pnl_rest = (exit_price - entry_price) * remaining_shares
            pnl = pnl_partial + pnl_rest
            pnl_pct = pnl / (entry_price * shares) if entry_price > 0 else 0
            return TradeResult(
                symbol=symbol, date=str(bar.get("timestamp", __import__("pandas").Timestamp.now()).date()) if bi >= 0 else "",
                entry_price=entry_price, exit_price=exit_price, shares=shares,
                pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4), exit_reason=reason,
                open_price=open_price, sell_target=target, stop_price=sp,
                partial_sell_price=partial_sell_price, partial_sell_shares=partial_sell_shares,
                trailing_high=highest, trailing_exit_price=exit_price,
                exit_bar_idx=bi, position_size=entry_price * shares,
                trade_type="reentry",
            )

        import pandas as pd
        for bi, bar in enumerate(bars_after_entry):
            bh, bl = bar["high"], bar["low"]
            if bh > highest:
                highest = bh
            if bl <= sp:
                return _make_result("reentry_stop", sp, bi)
            if not reached_target and bh >= target:
                reached_target = True
                if not sold_partial:
                    sold_partial = True
                    partial_sell_price = target
                    partial_sell_shares = remaining_shares // 3
                    remaining_shares -= partial_sell_shares
            if reached_target and remaining_shares > 0:
                tsp = round(highest * (1 - config.REENTRY_TRAILING_PCT), 2)
                tsp = max(tsp, entry_price)
                if bl <= tsp:
                    return _make_result("reentry_trailing", tsp, bi)

        if force_close_price is not None:
            exit_price = force_close_price
        else:
            exit_price = bars_after_entry[-1]["close"] if bars_after_entry else entry_price
        if not reached_target:
            exit_price = entry_price
        return _make_result("reentry_force_close", exit_price, len(bars_after_entry) - 1)

    # Patch
    strategy.evaluate_reentry_trade = old_evaluate_reentry

    # Also need old re-entry params in config
    config.REENTRY_PROFIT_RETRACEMENT = 1.50
    config.REENTRY_TRAILING_PCT = 0.05
    config.REENTRY_TIME_LIMIT_BARS = 0  # No time limit in old version

    trades = run_backtest(n_days=63)

    total_pnl = sum(t.pnl for t in trades)
    first_trades = [t for t in trades if t.trade_type == "first"]
    reentry_trades = [t for t in trades if t.trade_type == "reentry"]
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    from collections import Counter
    first_reasons = Counter(t.exit_reason for t in first_trades)
    reentry_reasons = Counter(t.exit_reason for t in reentry_trades)

    # Restore
    strategy.evaluate_reentry_trade = old_func
    config.REENTRY_PROFIT_PCT_1 = orig_pct1
    config.REENTRY_PROFIT_PCT_2 = orig_pct2
    config.REENTRY_TIME_LIMIT_BARS = orig_tl

    return {
        "label": label,
        "equity": 1000 + total_pnl,
        "total_pnl": total_pnl,
        "total_trades": len(trades),
        "first_trades": len(first_trades),
        "reentry_trades": len(reentry_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "first_reasons": dict(first_reasons),
        "reentry_reasons": dict(reentry_reasons),
    }


if __name__ == "__main__":
    import config

    results = []

    # ── Test A: 0.4.12 baseline ──
    print("\n" + "="*60)
    print("TEST A: 0.4.12 baseline (all changes)")
    print("="*60)
    r = run_variant("0.4.12 基线", {})
    results.append(r)

    # ── Test B: No 40-min first trade time limit ──
    print("\n" + "="*60)
    print("TEST B: 关闭40分钟首次交易时间限制")
    print("="*60)
    r = run_variant("无40分钟限制", {"FIRST_TRADE_TIME_LIMIT_BARS": 0})
    results.append(r)

    # ── Test C: No 1-hour re-entry time limit ──
    print("\n" + "="*60)
    print("TEST C: 关闭1小时再入场时间限制")
    print("="*60)
    r = run_variant("无再入场1小时限制", {"REENTRY_TIME_LIMIT_BARS": 0})
    results.append(r)

    # ── Test D: No time limits at all ──
    print("\n" + "="*60)
    print("TEST D: 关闭所有时间限制")
    print("="*60)
    r = run_variant("无任何时间限制", {"FIRST_TRADE_TIME_LIMIT_BARS": 0, "REENTRY_TIME_LIMIT_BARS": 0})
    results.append(r)

    # ── Test E: 0.4.11 style re-entry (retracement + trailing + breakeven) ──
    print("\n" + "="*60)
    print("TEST E: 0.4.11风格再入场 (retracement目标+trailing)")
    print("="*60)
    r = run_variant_011_reentry("0.4.11再入场风格")
    results.append(r)

    # ── Summary ──
    print("\n" + "="*70)
    print("对比总结")
    print("="*70)
    print(f"{'测试':<20} {'最终权益':>12} {'总P&L':>12} {'交易数':>8} {'胜率':>8} {'vs基线':>10}")
    print("-"*70)
    baseline = results[0]["equity"]
    for r in results:
        diff = r["equity"] - baseline
        sign = "+" if diff >= 0 else ""
        print(f"{r['label']:<20} ${r['equity']:>10,.0f} ${r['total_pnl']:>10,.0f} {r['total_trades']:>6} {r['win_rate']:>6.1f}% {sign}{diff:>+8,.0f}")

    print("\n── 首次交易退出原因对比 ──")
    all_first_reasons = set()
    for r in results:
        all_first_reasons.update(r["first_reasons"].keys())
    print(f"{'退出原因':<25}", end="")
    for r in results:
        print(f" {r['label'][:8]:>8}", end="")
    print()
    for reason in sorted(all_first_reasons):
        print(f"{reason:<25}", end="")
        for r in results:
            print(f" {r['first_reasons'].get(reason, 0):>8}", end="")
        print()

    print("\n── 再入场退出原因对比 ──")
    all_reentry_reasons = set()
    for r in results:
        all_reentry_reasons.update(r["reentry_reasons"].keys())
    print(f"{'退出原因':<25}", end="")
    for r in results:
        print(f" {r['label'][:8]:>8}", end="")
    print()
    for reason in sorted(all_reentry_reasons):
        print(f"{reason:<25}", end="")
        for r in results:
            print(f" {r['reentry_reasons'].get(reason, 0):>8}", end="")
        print()
