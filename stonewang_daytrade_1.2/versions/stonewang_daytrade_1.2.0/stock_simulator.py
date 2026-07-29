"""Stock Data Simulator — 5 scenarios to deeply test the trading system.

Generates deterministic 1-minute bar data for 5 different price paths,
each designed to exercise a specific set of trading system functions.
Runs each scenario through evaluate_trade_stone / evaluate_reentry_trade
and reports results.

Scenarios:
1. Perfect Ladder: T1-T6 gradual triggers + trailing stop exit
2. Skip-Gap Jump: Price jumps past multiple tiers simultaneously
3. Stop Loss: Entry then continuous decline to stop
4. Time Limit: 40-min flat then breakeven exit
5. Re-entry: First trade exit, re-entry, tier-1, trailing stop
"""

import sys
import os
import math
import datetime as dt
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from strategy import (
    build_trade_plan, evaluate_trade_stone, evaluate_reentry_trade,
    find_reentry_point, calc_stop_price, calc_atr,
)

EST = ZoneInfo("US/Eastern")

SYMBOL = "TESTSIM"
PREV_CLOSE = 10.00
OPEN_PRICE = 11.50  # gap +15%
ENTRY_PRICE = 10.50


def make_bar(timestamp, open_, high, low, close, volume=50000):
    return {
        "timestamp": timestamp,
        "open": round(open_, 4),
        "high": round(high, 4),
        "low": round(low, 4),
        "close": round(close, 4),
        "volume": volume,
    }


def make_ts(hour, minute, date="2026-07-25"):
    return dt.datetime.strptime(f"{date} {hour}:{minute}", "%Y-%m-%d %H:%M").replace(tzinfo=EST)


def bars_range(start_h, start_m, end_h, end_m, price_fn, date="2026-07-25"):
    bars = []
    t = dt.datetime.strptime(f"{date} {start_h}:{start_m}", "%Y-%m-%d %H:%M").replace(tzinfo=EST)
    end = dt.datetime.strptime(f"{date} {end_h}:{end_m}", "%Y-%m-%d %H:%M").replace(tzinfo=EST)
    idx = 0
    while t <= end:
        result = price_fn(idx)
        if len(result) == 5:
            o, h, l, c, vol = result
        else:
            o, h, l, c = result[:4]
            vol = 50000
        bars.append(make_bar(t, o, h, l, c, vol))
        t += dt.timedelta(minutes=1)
        idx += 1
    return bars


def build_plan(entry=ENTRY_PRICE, open=OPEN_PRICE):
    return build_trade_plan(SYMBOL, open, entry, atr=0.8)


def print_tier_sells(result):
    for i, ps in enumerate(result.partial_sells):
        if ps[1] > 0:
            print("    T%d: %dsh @ $%.2f" % (i+1, ps[1], ps[0]))


# ── Scenario 1: Perfect Ladder ───────────────────────────────────────────

def scenario1_perfect_ladder():
    plan = build_plan()
    print("\n" + "=" * 65)
    print("Scenario 1: Perfect Ladder — %s" % SYMBOL)
    print("=" * 65)
    print("Entry: $%.2f | Stop: $%.2f" % (plan.pullback, plan.stop_price))
    for i, t in enumerate(plan.targets):
        print("  T%d = $%.2f (sell ceil(sh/8), trail %.1f%%)" % (i+1, t, plan.trail_pcts[i]*100))

    targets = plan.targets
    entry = plan.pullback
    # Each phase: price rises to next target, then small pullback
    def price_fn(idx):
        phases = [(15, entry, targets[0]),
                  (15, targets[0], targets[1]),
                  (15, targets[1], targets[2]),
                  (15, targets[2], targets[3]),
                  (15, targets[3], targets[4]),
                  (15, targets[4], targets[5])]
        cumulative = 0
        for dur, start_p, end_p in phases:
            if idx < cumulative + dur:
                progress = (idx - cumulative) / dur
                p = start_p + progress * (end_p - start_p)
                return (p - 0.01, p + 0.02, p - 0.01, p)
            cumulative += dur
        # After all tiers: peak then trailing stop
        peak = targets[5] + 0.15
        trail_pct = plan.trail_pcts[5]
        tsp = round(peak * (1 - trail_pct), 2)
        tsp = max(tsp, entry)
        if idx < cumulative + 10:
            p = peak - 0.01 * (idx - cumulative)
            return (p, peak, p - 0.02, p)
        else:
            p = peak - 0.05 * (idx - cumulative - 10)
            l = max(p - 0.02, tsp)
            return (p, peak - 0.03, l, p)

    bars = bars_range(10, 1, 12, 0, price_fn)
    result = evaluate_trade_stone(plan, bars, force_close_price=None, time_limit_bars=0)

    print("\nResult:")
    print("  Exit reason: %s" % result.exit_reason)
    print("  Exit price: $%.2f" % result.exit_price)
    print("  P&L: $%.2f (%.2f%%)" % (result.pnl, result.pnl_pct * 100))
    print("  Trailing high: $%.2f" % result.trailing_high)
    print_tier_sells(result)
    return result


# ── Scenario 2: Skip-Gap Jump ────────────────────────────────────────────

def scenario2_skip_gap():
    plan = build_plan()
    print("\n" + "=" * 65)
    print("Scenario 2: Skip-Gap Jump — %s" % SYMBOL)
    print("=" * 65)
    print("Entry: $%.2f | Targets: %s" % (plan.pullback,
          ", ".join("$%.2f" % t for t in plan.targets)))

    targets = plan.targets
    entry = plan.pullback

    def price_fn(idx):
        if idx < 5:      # Small initial rise
            p = entry + 0.01 * idx
            return (entry, entry + 0.05, entry - 0.01, p)
        elif idx < 8:    # Jump directly to T4
            progress = (idx - 5) / 3
            p = entry + progress * (targets[3] - entry)
            return (p - 0.02, p + 0.05, p - 0.01, p)
        elif idx < 12:   # Rise to T5
            progress = (idx - 8) / 4
            p = targets[3] + progress * (targets[4] - targets[3])
            return (p - 0.01, p + 0.02, p - 0.01, p)
        elif idx < 16:   # Rise to T6
            progress = (idx - 12) / 4
            p = targets[4] + progress * (targets[5] - targets[4])
            return (p - 0.01, p + 0.02, p - 0.01, p)
        else:            # Peak then trailing
            peak = targets[5] + 0.25
            tsp = round(peak * (1 - plan.trail_pcts[5]), 2)
            tsp = max(tsp, entry)
            if idx < 25:
                p = peak - 0.01 * idx
                return (p, peak, p - 0.01, p)
            else:
                p = peak - 0.08 * (idx - 25)
                return (p, peak - 0.03, min(p - 0.02, tsp), p)

    bars = bars_range(10, 1, 11, 0, price_fn)
    result = evaluate_trade_stone(plan, bars, force_close_price=None, time_limit_bars=0)

    print("\nResult:")
    print("  Exit reason: %s" % result.exit_reason)
    print("  Exit price: $%.2f" % result.exit_price)
    print("  P&L: $%.2f (%.2f%%)" % (result.pnl, result.pnl_pct * 100))
    print_tier_sells(result)
    return result


# ── Scenario 3: Stop Loss ─────────────────────────────────────────────────

def scenario3_stop_loss():
    plan = build_plan()
    print("\n" + "=" * 65)
    print("Scenario 3: Stop Loss — %s" % SYMBOL)
    print("=" * 65)
    print("Entry: $%.2f | Stop: $%.2f" % (plan.pullback, plan.stop_price))

    entry = plan.pullback
    stop = plan.stop_price

    def price_fn(idx):
        if idx < 10:     # Brief rise, not reaching T1
            p = entry + 0.01 * idx
            return (p, p + 0.02, p - 0.01, p)
        elif idx < 15:   # Start declining
            p = entry + 0.10 - 0.02 * (idx - 10)
            return (p, p + 0.01, p - 0.02, p)
        elif idx < 30:   # Steady decline toward stop
            progress = (idx - 15) / 15
            p = entry - progress * (entry - stop)
            p = max(p, stop)
            l = min(p - 0.03, stop) if progress > 0.8 else p - 0.02
            return (p, p + 0.01, l, p)
        else:            # Below stop
            p = stop - 0.01 * (idx - 30)
            return (p, p + 0.01, stop, p)

    bars = bars_range(10, 1, 11, 0, price_fn)
    result = evaluate_trade_stone(plan, bars, force_close_price=None, time_limit_bars=0)

    print("\nResult:")
    print("  Exit reason: %s" % result.exit_reason)
    print("  Exit price: $%.2f" % result.exit_price)
    print("  P&L: $%.2f (%.2f%%)" % (result.pnl, result.pnl_pct * 100))
    print_tier_sells(result)
    return result


# ── Scenario 4: Time Limit ────────────────────────────────────────────────

def scenario4_time_limit():
    plan = build_plan()
    print("\n" + "=" * 65)
    print("Scenario 4: Time Limit — %s" % SYMBOL)
    print("=" * 65)
    print("Entry: $%.2f | Time limit: %d bars" % (plan.pullback,
          getattr(config, "FIRST_TRADE_TIME_LIMIT_BARS", 8)))

    entry = plan.pullback
    t1 = plan.targets[0]

    def price_fn(idx):
        # Flat price between entry and just below T1
        base = entry + 0.05
        p = base + 0.02 * math.sin(idx * 0.628)
        h = min(p + 0.02, t1 - 0.01)
        l = max(p - 0.02, entry)
        if idx > 45:  # After time limit, rise to entry for breakeven
            p = entry + 0.01 * min(idx - 45, 5)
            return (p, p + 0.01, p - 0.01, p)
        return (p, h, l, p)

    bars = bars_range(10, 1, 11, 30, price_fn)
    time_limit = getattr(config, "FIRST_TRADE_TIME_LIMIT_BARS", 8)
    result = evaluate_trade_stone(plan, bars, force_close_price=None,
                                  time_limit_bars=time_limit)

    print("\nResult:")
    print("  Exit reason: %s" % result.exit_reason)
    print("  Exit price: $%.2f" % result.exit_price)
    print("  P&L: $%.2f (%.2f%%)" % (result.pnl, result.pnl_pct * 100))
    print_tier_sells(result)
    return result


# ── Scenario 5: Re-entry ──────────────────────────────────────────────────

def scenario5_reentry():
    plan = build_plan()
    print("\n" + "=" * 65)
    print("Scenario 5: Re-entry — %s" % SYMBOL)
    print("=" * 65)
    print("Entry: $%.2f | T1: $%.2f" % (plan.pullback, plan.targets[0]))

    entry = plan.pullback
    t1 = plan.targets[0]
    trail_pct_1 = plan.trail_pcts[0]

    # Phase A: First trade — reach T1, then trailing stop
    def phase_a_fn(idx):
        if idx < 10:
            progress = idx / 10
            p = entry + progress * (t1 - entry)
            return (p - 0.01, p + 0.02, p - 0.01, p)
        elif idx < 20:
            peak = t1 + 0.30
            tsp = round(peak * (1 - trail_pct_1), 2)
            tsp = max(tsp, entry)
            if idx < 15:
                p = t1 + 0.06 * (idx - 10)
                return (p, peak, p - 0.01, p)
            else:
                p = peak - 0.04 * (idx - 15)
                l = max(p - 0.02, tsp if idx > 17 else p - 0.01)
                return (p, peak, l, p)

    bars_a = bars_range(10, 1, 10, 20, phase_a_fn)
    result_a = evaluate_trade_stone(plan, bars_a, force_close_price=None, time_limit_bars=0)

    print("\nPhase A (first trade):")
    print("  Exit: %s @ $%.2f" % (result_a.exit_reason, result_a.exit_price))
    print("  P&L: $%.2f" % result_a.pnl)
    print_tier_sells(result_a)

    # Phase B: Re-entry detection and execution
    # After first trade exits, stock continues rising then pulls back.
    # find_reentry_point requires: peak > open*1.03, lower low (pullback),
    # then confirmation bar (bullish + high volume).
    initial_highest = max(result_a.trailing_high, OPEN_PRICE)
    peak = 12.10  # new peak after first trade exit

    def phase_b_fn(idx):
        # Phase 1 (idx 0-9): Rise from exit price toward peak
        if idx < 10:
            progress = idx / 10
            p = result_a.exit_price + progress * (peak - result_a.exit_price)
            h = p + 0.05 + 0.01 * idx
            l = p - 0.03
            return (p, h, l, p)
        # Phase 2 (idx 10): Peak bar
        elif idx == 10:
            return (peak - 0.02, peak + 0.02, peak - 0.08, peak)
        # Phase 3 (idx 11-13): Decline with progressively lower lows
        elif idx == 11:
            p = peak - 0.20
            return (p, p + 0.05, p - 0.15, p)
        elif idx == 12:
            p = peak - 0.40
            return (p, p + 0.04, peak - 0.55, p)
        elif idx == 13:
            # Pullback bottom — lowest low bar
            p = peak - 0.55
            return (p, p + 0.03, peak - 0.75, p)
        elif idx == 14:
            # Confirmation bar: bullish (close > open), high volume
            # low >= previous bar's low, entry_price = bars[13].low
            bottom = peak - 0.75
            o = bottom + 0.05
            c = bottom + 0.15
            h = c + 0.03
            l = bottom + 0.01
            return (o, h, l, c, 70000)
        # Phase 4 (idx 15+): Re-entry trade execution
        else:
            reentry_entry = peak - 0.75
            reentry_target = round(reentry_entry + 0.75 * (peak + 0.02 - reentry_entry), 2)
            trail_pct = getattr(config, "REENTRY_TRAILING_PCT_2", 0.03)
            reentry_peak = reentry_target + 0.15
            tsp = round(reentry_peak * (1 - trail_pct), 2)
            tsp = max(tsp, reentry_entry)
            start_p = reentry_entry + 0.15  # price right after confirmation
            if idx < 20:
                progress = (idx - 15) / 5
                p = start_p + progress * (reentry_target - start_p)
                h = p + 0.03
                l = p - 0.02
                return (p, h, l, p)
            elif idx < 23:
                progress = (idx - 20) / 3
                p = reentry_target + progress * (reentry_peak - reentry_target)
                h = p + 0.03
                l = p - 0.02
                return (p, h, l, p)
            else:
                p = reentry_peak - 0.04 * (idx - 23)
                l = p - 0.02
                if idx > 25:
                    l = min(l, tsp)
                return (p, p + 0.01, l, p)

    bars_b = bars_range(10, 21, 10, 50, phase_b_fn)

    reentry_price, prev_high, reentry_bar, confirmed = find_reentry_point(
        bars_b, OPEN_PRICE, initial_highest=initial_highest
    )

    print("\nPhase B (re-entry detection):")
    print("  Re-entry price: $%.2f" % reentry_price)
    print("  Prev high: $%.2f" % prev_high)
    print("  Confirmed: %s (bar %d)" % (confirmed, reentry_bar))

    if confirmed and reentry_price > 0:
        bars_after_reentry = bars_b[reentry_bar + 1:]
        reentry_stop = round(reentry_price * (1 - getattr(config, "REENTRY_STOP_PCT", 0.08)), 2)
        reentry_shares = math.ceil(plan.shares * getattr(config, "REENTRY_POSITION_RATIO", 0.5))
        result_b = evaluate_reentry_trade(
            reentry_price, prev_high, reentry_shares, SYMBOL, OPEN_PRICE,
            bars_after_reentry, force_close_price=None, stop_price=reentry_stop
        )
        print("\nPhase B (re-entry trade):")
        print("  Exit: %s @ $%.2f" % (result_b.exit_reason, result_b.exit_price))
        print("  P&L: $%.2f" % result_b.pnl)
        for ps_price, ps_qty in result_b.partial_sells:
            if ps_qty > 0:
                print("    Partial: %dsh @ $%.2f" % (ps_qty, ps_price))
        return result_a, result_b
    else:
        print("\nRe-entry NOT confirmed — diagnosing bars:")
        print("  initial_highest: $%.2f (need > $%.2f for peak)" % (initial_highest, OPEN_PRICE * 1.03))
        for i in range(min(len(bars_b), 20)):
            print("    bar %d: o=%.2f h=%.2f l=%.2f c=%.2f vol=%d" % (
                i, bars_b[i]["open"], bars_b[i]["high"], bars_b[i]["low"],
                bars_b[i]["close"], bars_b[i]["volume"]))
        return result_a, None


# ── Main ──────────────────────────────────────────────────────────────────

def run_all():
    print("=" * 65)
    print("Stock Data Simulator — 5-Scenario Deep Test")
    print("=" * 65)
    print("Symbol: %s | Prev close: $%.2f | Open: $%.2f" % (SYMBOL, PREV_CLOSE, OPEN_PRICE))
    print("Gap: +%.1f%% | Entry: $%.2f" % ((OPEN_PRICE/PREV_CLOSE - 1)*100, ENTRY_PRICE))
    plan = build_plan()
    print("Shares: %d | Stop: $%.2f" % (plan.shares, plan.stop_price))
    print("Targets: %s" % ", ".join("$%.2f" % t for t in plan.targets))
    print("Trail pcts: %s" % ", ".join("%.1f%%" % (p*100) for p in plan.trail_pcts))
    per_tier = math.ceil(plan.shares / 8) if plan.shares >= 8 else 1
    print("Per-tier sell: %dsh (ceil(%d/8))" % (per_tier, plan.shares))

    results = []
    results.append(("Perfect Ladder", scenario1_perfect_ladder()))
    results.append(("Skip-Gap Jump", scenario2_skip_gap()))
    results.append(("Stop Loss", scenario3_stop_loss()))
    results.append(("Time Limit", scenario4_time_limit()))
    r5 = scenario5_reentry()
    results.append(("Re-entry", r5))

    print("\n" + "=" * 65)
    print("SUMMARY")
    print("=" * 65)
    total_pnl = 0
    wins = 0
    for name, r in results:
        if isinstance(r, tuple):
            r = r[0]
        pnl = r.pnl
        total_pnl += pnl
        if pnl > 0:
            wins += 1
        status = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "NEUTRAL"
        print("  %s: P&L=$%.2f %s" % (name, pnl, status))
    print("  Total P&L: $%.2f | Wins: %d/%d" % (total_pnl, wins, len(results)))


if __name__ == "__main__":
    run_all()
