"""Detailed A/B comparison: isolate TARGET_150 full exit vs trailing, and re-entry 3%/5% vs retracement."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from backtest import run_backtest
from strategy import evaluate_trade_stone, evaluate_reentry_trade, TradeResult, TradePlan
from collections import Counter
import pandas as pd


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
        "first_n": len(first),
        "reentry_n": len(reentry),
        "win_rate": len(wins)/len(trades)*100 if trades else 0,
        "first_reasons": dict(Counter(t.exit_reason for t in first)),
        "reentry_reasons": dict(Counter(t.exit_reason for t in reentry)),
        "first_pnl": sum(t.pnl for t in first),
        "reentry_pnl": sum(t.pnl for t in reentry),
    }


# ── Old TARGET_150 logic: sell 1/3, then trailing stop ──
def make_old_evaluate_trade():
    """Return evaluate_trade_stone with old 0.4.11 TARGET_150: sell 1/3, trail rest."""
    import strategy

    def old_evaluate_trade(
        plan, bars_after_entry, force_close_price=None,
        time_limit_bars=0, trail_pct_75=0.03, trail_pct_1125=0.03, trail_pct_150=0.04,
    ):
        highest = plan.pullback
        reached_75 = reached_1125 = reached_150 = False
        sold_partial_75 = sold_partial_1125 = sold_partial_150 = False
        partial_sell_price_75 = partial_sell_price_1125 = partial_sell_price_150 = 0.0
        partial_sell_shares_75 = partial_sell_shares_1125 = partial_sell_shares_150 = 0
        remaining_shares = plan.shares
        time_limit_active = False

        def _make_result(reason, exit_price, bi):
            pnl_parts = []
            if sold_partial_75:
                pnl_parts.append((partial_sell_price_75 - plan.pullback) * partial_sell_shares_75)
            if sold_partial_1125:
                pnl_parts.append((partial_sell_price_1125 - plan.pullback) * partial_sell_shares_1125)
            if sold_partial_150:
                pnl_parts.append((partial_sell_price_150 - plan.pullback) * partial_sell_shares_150)
            pnl_parts.append((exit_price - plan.pullback) * remaining_shares)
            pnl = sum(pnl_parts)
            pnl_pct = pnl / (plan.pullback * plan.shares) if plan.pullback > 0 else 0
            return TradeResult(
                symbol=plan.symbol,
                date=str(bar.get("timestamp", pd.Timestamp.now()).date()) if bi >= 0 else "",
                entry_price=plan.pullback, exit_price=exit_price, shares=plan.shares,
                pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4), exit_reason=reason,
                open_price=plan.open_price, sell_target=plan.target_150, stop_price=plan.stop_price,
                partial_sell_price=partial_sell_price_75, partial_sell_shares=partial_sell_shares_75,
                trailing_high=highest, trailing_exit_price=exit_price,
                exit_bar_idx=bi, position_size=plan.pullback * plan.shares,
                trade_type="first",
            )

        for bi, bar in enumerate(bars_after_entry):
            bh, bl = bar["high"], bar["low"]
            if bh > highest:
                highest = bh

            if bl <= plan.stop_price:
                return _make_result("stop_loss", plan.stop_price, bi)

            if time_limit_bars > 0 and not reached_75 and bi >= time_limit_bars:
                time_limit_active = True
            if time_limit_active and bh >= plan.pullback:
                exit_price = max(bh, plan.pullback)
                return _make_result("time_limit_exit", exit_price, bi)

            # OLD: TARGET_150 sells 1/3, NOT all remaining
            if not reached_150 and bh >= plan.target_150:
                reached_150 = reached_1125 = reached_75 = True
                if not sold_partial_150:
                    sold_partial_150 = True
                    partial_sell_price_150 = plan.target_150
                    partial_sell_shares_150 = remaining_shares // 3  # OLD: only 1/3
                    remaining_shares -= partial_sell_shares_150
                if not sold_partial_1125:
                    sold_partial_1125 = True
                    partial_sell_price_1125 = plan.target_150
                    partial_sell_shares_1125 = 0
                if not sold_partial_75:
                    sold_partial_75 = True
                    partial_sell_price_75 = plan.target_150
                    partial_sell_shares_75 = 0
                # DO NOT return — continue to trailing stop

            if not reached_1125 and bh >= plan.target_1125:
                reached_1125 = reached_75 = True
                if not sold_partial_1125:
                    sold_partial_1125 = True
                    partial_sell_price_1125 = plan.target_1125
                    partial_sell_shares_1125 = remaining_shares // 3
                    remaining_shares -= partial_sell_shares_1125
                if not sold_partial_75:
                    sold_partial_75 = True
                    partial_sell_price_75 = plan.target_1125
                    partial_sell_shares_75 = plan.shares // 4
                    remaining_shares -= partial_sell_shares_75

            if not reached_75 and bh >= plan.target_75:
                reached_75 = True
                if not sold_partial_75:
                    sold_partial_75 = True
                    partial_sell_price_75 = plan.target_75
                    partial_sell_shares_75 = plan.shares // 4
                    remaining_shares -= partial_sell_shares_75

            # Trailing stop (continues after TARGET_150 in old version)
            if reached_75:
                if reached_150:
                    pct = trail_pct_150
                elif reached_1125:
                    pct = trail_pct_1125
                else:
                    pct = trail_pct_75
                tsp = round(highest * (1 - pct), 2)
                tsp = max(tsp, plan.pullback)
                if bl <= tsp:
                    suffix = "_150" if reached_150 else "_1125" if reached_1125 else "_75"
                    return _make_result(f"trailing_stop{suffix}", tsp, bi)

        if force_close_price is not None:
            exit_price = force_close_price
        else:
            exit_price = bars_after_entry[-1]["close"] if bars_after_entry else plan.pullback
        if not reached_75 and not sold_partial_75 and not sold_partial_1125 and not sold_partial_150:
            exit_price = plan.pullback

        return _make_result("force_close", exit_price, len(bars_after_entry) - 1)

    return old_evaluate_trade


# ── Old re-entry logic: retracement target + trailing ──
def make_old_evaluate_reentry():
    """Return evaluate_reentry_trade with old 0.4.11 style: retracement target, trailing, breakeven."""
    def old_evaluate_reentry(
        entry_price, prev_high, shares, symbol, open_price,
        bars_after_entry, force_close_price=None,
        stop_price=None, **kwargs,
    ):
        sp = stop_price if stop_price else round(entry_price * (1 - config.REENTRY_STOP_PCT), 2)
        target = round(entry_price + 1.50 * (prev_high - entry_price), 2)

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
                symbol=symbol,
                date=str(bar.get("timestamp", pd.Timestamp.now()).date()) if bi >= 0 else "",
                entry_price=entry_price, exit_price=exit_price, shares=shares,
                pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4), exit_reason=reason,
                open_price=open_price, sell_target=target, stop_price=sp,
                partial_sell_price=partial_sell_price, partial_sell_shares=partial_sell_shares,
                trailing_high=highest, trailing_exit_price=exit_price,
                exit_bar_idx=bi, position_size=entry_price * shares,
                trade_type="reentry",
            )

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
                tsp = round(highest * (1 - 0.05), 2)
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

    return old_evaluate_reentry


if __name__ == "__main__":
    import strategy

    results = []

    # ── Test 1: 0.4.12 baseline ──
    print("=" * 60)
    print("1: 0.4.12 基线 (TARGET_150全清 + 再入3%/5% + 再入1hr限制)")
    r = run_and_stats("0.4.12基线")
    results.append(r)

    # ── Test 2: 0.4.12 + TARGET_150 trailing (old 0.4.11 style) ──
    print("\n" + "=" * 60)
    print("2: TARGET_150 trailing (0.4.11风格: 卖1/3+trailing, 保留再入3%/5%)")
    import backtest as bt
    old_first_strat = strategy.evaluate_trade_stone
    old_first_bt = bt.evaluate_trade_stone
    strategy.evaluate_trade_stone = make_old_evaluate_trade()
    bt.evaluate_trade_stone = strategy.evaluate_trade_stone
    r = run_and_stats("150trailing")
    results.append(r)
    strategy.evaluate_trade_stone = old_first_strat
    bt.evaluate_trade_stone = old_first_strat

    # ── Test 3: 0.4.12 + old re-entry (retracement + trailing) ──
    print("\n" + "=" * 60)
    print("3: 0.4.11再入场 (retracement目标+trailing, 保留TARGET_150全清)")
    old_reentry_strat = strategy.evaluate_reentry_trade
    old_reentry_bt = bt.evaluate_reentry_trade
    strategy.evaluate_reentry_trade = make_old_evaluate_reentry()
    bt.evaluate_reentry_trade = strategy.evaluate_reentry_trade
    config.REENTRY_TIME_LIMIT_BARS = 0  # old re-entry had no time limit
    r = run_and_stats("旧再入场")
    results.append(r)
    strategy.evaluate_reentry_trade = old_reentry_strat
    bt.evaluate_reentry_trade = old_reentry_strat
    config.REENTRY_TIME_LIMIT_BARS = 12  # restore

    # ── Test 4: Full 0.4.11 (TARGET_150 trailing + old re-entry) ──
    print("\n" + "=" * 60)
    print("4: 完整0.4.11风格 (150 trailing + retracement再入场)")
    strategy.evaluate_trade_stone = make_old_evaluate_trade()
    bt.evaluate_trade_stone = strategy.evaluate_trade_stone
    strategy.evaluate_reentry_trade = make_old_evaluate_reentry()
    bt.evaluate_reentry_trade = strategy.evaluate_reentry_trade
    config.REENTRY_TIME_LIMIT_BARS = 0
    r = run_and_stats("0.4.11全")
    results.append(r)
    strategy.evaluate_trade_stone = old_first_strat
    bt.evaluate_trade_stone = old_first_strat
    strategy.evaluate_reentry_trade = old_reentry_strat
    bt.evaluate_reentry_trade = old_reentry_strat
    config.REENTRY_TIME_LIMIT_BARS = 12

    # ── Test 5: 0.4.12 without re-entry time limit ──
    print("\n" + "=" * 60)
    print("5: 0.4.12无再入场时间限制")
    config.REENTRY_TIME_LIMIT_BARS = 0
    r = run_and_stats("无再入场限")
    results.append(r)
    config.REENTRY_TIME_LIMIT_BARS = 12

    # ── Summary ──
    print("\n" + "=" * 90)
    print("对比总结")
    print("=" * 90)
    print(f"{'测试':<16} {'最终权益':>12} {'总P&L':>12} {'交易':>5} {'首次':>5} {'再入':>5} {'胜率':>7} {'首次P&L':>10} {'再入P&L':>10} {'vs基线':>10}")
    print("-" * 90)
    base = results[0]["equity"]
    for r in results:
        diff = r["equity"] - base
        sign = "+" if diff >= 0 else ""
        print(f"{r['label']:<16} ${r['equity']:>10,.0f} ${r['pnl']:>10,.0f} {r['n']:>4} {r['first_n']:>4} {r['reentry_n']:>4} {r['win_rate']:>5.1f}% ${r['first_pnl']:>8,.0f} ${r['reentry_pnl']:>8,.0f} {sign}{diff:>+9,.0f}")

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

    print("\n── 各修改效果分析 ──")
    base_eq = results[0]["equity"]
    t150_trail = results[1]["equity"]
    old_re = results[2]["equity"]
    full_011 = results[3]["equity"]
    no_re_tl = results[4]["equity"]

    print(f"TARGET_150全清 vs trailing: {t150_trail - base_eq:+,.0f} (trailing {'更好' if t150_trail > base_eq else '更差'})")
    print(f"再入场3%/5% vs retracement: {old_re - base_eq:+,.0f} (retracement {'更好' if old_re > base_eq else '更差'})")
    print(f"两者组合(0.4.11全): {full_011 - base_eq:+,.0f}")
    print(f"1hr再入场时间限制: {no_re_tl - base_eq:+,.0f} (去掉限制 {'更好' if no_re_tl > base_eq else '更差'})")

    # Decomposition
    effect_150 = t150_trail - base_eq
    effect_reentry = old_re - base_eq
    effect_combo = full_011 - base_eq
    interaction = effect_combo - effect_150 - effect_reentry

    print(f"\n── 效果分解 ──")
    print(f"TARGET_150 trailing效果: ${effect_150:+,.0f}")
    print(f"再入场retracement效果: ${effect_reentry:+,.0f}")
    print(f"交互效果: ${interaction:+,.0f}")
    print(f"1hr再入场限制效果: ${no_re_tl - base_eq:+,.0f}")
