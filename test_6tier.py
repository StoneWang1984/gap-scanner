"""6-tier algorithm verification test — simulated trading.

Tests all code paths without placing real orders:
1. Target calculation (entry < open and entry >= open)
2. Skip-gap partial sells
3. Trailing stop logic
4. Recovery from restart
5. Edge cases (all targets reached, no targets reached, etc.)
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "versions"))

from strategy import calc_price_at_retracement, build_trade_plan
import importlib.util
spec = importlib.util.spec_from_file_location("config_stone_0_4_17", os.path.join(os.path.dirname(__file__), "versions", "config_stone_1.0.py"))
_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_mod)
globals().update({k: v for k, v in vars(_mod).items() if not k.startswith('_')})

# Also need these from the live trading script
TARGET_LIMIT_BUFFER = 0.003
ENTRY_LIMIT_BUFFER = 0.005
STOP_LIMIT_BUFFER = 0.01

# Simulate the live trading calc_targets function
def calc_targets_live(entry_price, open_price):
    retracements = PROFIT_RETRACEMENT_TIERS
    caps = TARGET_CAP_TIERS
    sell_ratios = PARTIAL_SELL_RATIOS
    trail_pcts = TRAILING_STOP_PCTS

    targets = []
    any_capped = False

    if entry_price >= open_price:
        for i in range(len(caps)):
            targets.append(round(entry_price * (1 + caps[i]), 2))
        target_mode = "capped"
    else:
        for i in range(len(retracements)):
            ret_price = calc_price_at_retracement(entry_price, open_price, retracements[i])
            cap_price = round(entry_price * (1 + caps[i]), 2)
            t = min(ret_price, cap_price)
            if t < ret_price:
                any_capped = True
            targets.append(t)
        target_mode = "capped" if any_capped else "retracement"

    return targets, sell_ratios, trail_pcts, target_mode


def get_trailing_pct(reached_list, trail_pcts):
    for ti in range(len(reached_list) - 1, -1, -1):
        if reached_list[ti]:
            return trail_pcts[ti] if ti < len(trail_pcts) else trail_pcts[-1]
    return trail_pcts[0]


class FakePosition:
    def __init__(self, symbol, entry_price, shares, open_price, stop_price, highest=None):
        self.symbol = symbol
        self.entry_price = entry_price
        self.shares = shares
        self.remaining_shares = shares
        self.open_price = open_price
        self.stop_price = stop_price
        self.highest = highest or entry_price
        self.targets, self.sell_ratios, self.trail_pcts, self.target_mode = calc_targets_live(entry_price, open_price)
        self.reached_list = [False] * len(self.targets)
        self.sold_shares_list = [0] * len(self.targets)

    def process_price(self, price):
        """Simulate what the 6-tier system would do at this price."""
        actions = []
        self.highest = max(self.highest, price)

        # Check targets from highest to lowest (skip-gap)
        n_tiers = len(self.targets)
        for ti in range(n_tiers - 1, -1, -1):
            if not self.reached_list[ti] and self.highest >= self.targets[ti]:
                # Mark all lower tiers as reached
                for tj in range(ti + 1):
                    self.reached_list[tj] = True

                # Sell this tier
                if self.sold_shares_list[ti] == 0 and self.remaining_shares > 0:
                    sell_n = int(self.shares * self.sell_ratios[ti])
                    sell_n = min(sell_n, self.remaining_shares)
                    if sell_n > 0:
                        sell_price = round(self.targets[ti] * (1 - TARGET_LIMIT_BUFFER), 2)
                        self.sold_shares_list[ti] = sell_n
                        self.remaining_shares -= sell_n
                        actions.append(f"SELL T{ti+1}({self.target_mode}) {sell_n}sh @ ${sell_price:.2f} (target=${self.targets[ti]:.2f})")

                # Skip-gap: sell lower unsold tiers
                for tj in range(ti):
                    if self.sold_shares_list[tj] == 0 and self.remaining_shares > 0:
                        self.reached_list[tj] = True
                        sell_n = int(self.shares * self.sell_ratios[tj])
                        sell_n = min(sell_n, self.remaining_shares)
                        if sell_n > 0:
                            sell_price = round(self.targets[ti] * (1 - TARGET_LIMIT_BUFFER), 2)
                            self.sold_shares_list[tj] = sell_n
                            self.remaining_shares -= sell_n
                            actions.append(f"SKIP-GAP T{tj+1} {sell_n}sh @ ${sell_price:.2f} (hit T{ti+1}=${self.targets[ti]:.2f})")
                break  # Only process one target hit per check

        # Check trailing stop
        if self.reached_list and self.reached_list[0] and self.remaining_shares > 0:
            pct = get_trailing_pct(self.reached_list, self.trail_pcts)
            tsp = round(self.highest * (1 - pct), 2)
            tsp = max(tsp, self.entry_price)
            if price <= tsp:
                actions.append(f"TRAILING STOP {self.remaining_shares}sh @ ${tsp:.2f} (trail={pct*100:.1f}%, high=${self.highest:.2f})")
                self.remaining_shares = 0

        # Check stop loss
        if price <= self.stop_price and self.remaining_shares > 0:
            actions.append(f"STOP LOSS {self.remaining_shares}sh @ ${self.stop_price:.2f}")
            self.remaining_shares = 0

        return actions


def test_case(name, entry, open_price, shares, price_path, stop_price=None):
    """Run a simulated trade through a price path."""
    if stop_price is None:
        stop_price = round(entry * 0.95, 2)

    pos = FakePosition(name, entry, shares, open_price, stop_price)
    print(f"\n{'='*70}")
    print(f"TEST: {name} | entry=${entry:.2f} open=${open_price:.2f} shares={shares}")
    print(f"  Targets: {pos.targets}")
    print(f"  Mode: {pos.target_mode}")
    print(f"  Stop: ${stop_price:.2f}")
    print(f"{'='*70}")

    total_pnl = 0
    for i, price in enumerate(price_path):
        actions = pos.process_price(price)
        if actions:
            for a in actions:
                print(f"  ${price:.2f}: {a}")

    # Final P&L
    sold_total = pos.shares - pos.remaining_shares
    avg_sell = 0
    if sold_total > 0:
        sold_value = sum(pos.sold_shares_list[i] * pos.targets[i] for i in range(len(pos.targets)) if pos.sold_shares_list[i] > 0)
        avg_sell = sold_value / sold_total if sold_total > 0 else 0
        total_pnl = (avg_sell - entry) * sold_total

    print(f"\n  RESULT: sold={sold_total}/{pos.shares}, remaining={pos.remaining_shares}")
    print(f"  Sold tiers: {pos.sold_shares_list}")
    print(f"  Reached: {pos.reached_list}")
    if sold_total > 0:
        print(f"  Avg sell: ${avg_sell:.2f}, P&L: ${total_pnl:.2f}")
    return total_pnl


# ── Test Cases ──

print("=" * 70)
print("6-TIER ALGORITHM VERIFICATION")
print(f"Tiers: {PROFIT_RETRACEMENT_TIERS}")
print(f"Caps: {TARGET_CAP_TIERS}")
print(f"Sell ratios: {[f'{r:.3f}' for r in PARTIAL_SELL_RATIOS]}")
print(f"Trail pcts: {TRAILING_STOP_PCTS}")
print("=" * 70)

# Test 1: Normal pullback entry (entry < open) — like gap scanner
# Stock gaps up, pulls back, we buy on pullback
test_case("Normal gap pullback",
    entry=6.50, open_price=7.00, shares=30,
    price_path=[6.60, 6.80, 7.00, 7.20, 7.50, 7.80, 8.00, 7.90, 7.70],
    stop_price=6.18)

# Test 2: Momentum entry (entry > open) — like our manual buy
test_case("Momentum entry (entry > open)",
    entry=7.87, open_price=6.71, shares=30,
    price_path=[8.00, 8.26, 8.50, 8.66, 8.80, 9.05, 9.20, 8.90, 8.50, 8.10],
    stop_price=7.48)

# Test 3: Skip-gap — price jumps past multiple tiers at once
test_case("Skip-gap (price jumps 3 tiers)",
    entry=6.50, open_price=7.00, shares=30,
    price_path=[6.60, 7.50, 7.80, 8.20],  # jumps to T3 target level
    stop_price=6.18)

# Test 4: Stop loss hit before any target
test_case("Stop loss hit",
    entry=6.50, open_price=7.00, shares=30,
    price_path=[6.40, 6.30, 6.18],
    stop_price=6.18)

# Test 5: Trailing stop triggers after partial sells
test_case("Trailing stop after T1",
    entry=6.50, open_price=7.00, shares=30,
    price_path=[6.80, 7.00, 6.95, 6.85],  # hits T1, then drops
    stop_price=6.18)

# Test 6: All 6 tiers hit gradually
test_case("All 6 tiers hit",
    entry=6.50, open_price=7.00, shares=30,
    price_path=[6.80, 7.00, 7.20, 7.50, 7.80, 8.10, 8.50, 8.80, 9.05, 9.50, 9.84, 10.50, 10.80],
    stop_price=6.18)

# Test 7: BIYA-like (very high gap, capped mode)
test_case("BIYA-like high gap",
    entry=6.79, open_price=7.78, shares=14,
    price_path=[7.02, 7.27, 7.52, 7.78, 8.03, 8.28, 8.50, 8.20, 7.90],
    stop_price=6.45)

# Test 8: Recovery simulation — position has existing highest
print(f"\n{'='*70}")
print("RECOVERY TEST: position with highest above entry")
print(f"{'='*70}")
pos = FakePosition("TEST", entry=7.87, open_price=6.71, shares=30, stop_price=7.48, highest=8.36)
# Simulate recovery: mark reached tiers based on highest
reached = [t <= pos.highest for t in pos.targets]
pos.reached_list = reached
print(f"  Entry=${pos.entry_price}, highest=${pos.highest}")
print(f"  Targets: {pos.targets}")
print(f"  Reached: {pos.reached_list}")
trail_pct = get_trailing_pct(pos.reached_list, pos.trail_pcts)
tsp = round(pos.highest * (1 - trail_pct), 2)
print(f"  Trailing stop pct: {trail_pct*100:.1f}%, stop=${tsp:.2f}")
# Now simulate price dropping
actions = pos.process_price(8.00)
print(f"  At $8.00: {actions if actions else 'no action (above trailing stop)'}")
actions = pos.process_price(7.90)
print(f"  At $7.90: {actions if actions else 'no action'}")
actions = pos.process_price(7.80)
print(f"  At $7.80: {actions if actions else 'no action'}")

# Test 9: Verify __post_init__ doesn't overwrite highest
print(f"\n{'='*70}")
print("__post_init__ VERIFICATION")
print(f"{'='*70}")
from dataclasses import dataclass, field

@dataclass
class TestLivePosition:
    symbol: str
    entry_price: float
    shares: int
    stop_price: float = 0.0
    targets: list = field(default_factory=list)
    sell_ratios: list = field(default_factory=list)
    trail_pcts: list = field(default_factory=list)
    open_price: float = 0.0
    trade_type: str = "first"
    target_mode: str = "retracement"
    reached_list: list = None
    sold_shares_list: list = None
    remaining_shares: int = 0
    highest: float = 0.0

    def __post_init__(self):
        self.remaining_shares = self.shares
        if self.highest == 0.0:
            self.highest = self.entry_price
        if self.reached_list is None:
            self.reached_list = [False] * len(self.targets)
        if self.sold_shares_list is None:
            self.sold_shares_list = [0] * len(self.targets)

# Case 1: highest not provided (default 0.0) → should set to entry
p1 = TestLivePosition(symbol="A", entry_price=10.0, shares=30, stop_price=9.5)
print(f"  No highest provided: highest=${p1.highest:.2f} (expected $10.00) {'PASS' if p1.highest == 10.0 else 'FAIL'}")

# Case 2: highest provided from recovery → should NOT overwrite
p2 = TestLivePosition(symbol="A", entry_price=10.0, shares=30, stop_price=9.5, highest=12.5)
print(f"  Recovery highest=$12.50: highest=${p2.highest:.2f} (expected $12.50) {'PASS' if p2.highest == 12.5 else 'FAIL'}")

# Case 3: highest is 0.0 explicitly (should set to entry)
p3 = TestLivePosition(symbol="A", entry_price=10.0, shares=30, stop_price=9.5, highest=0.0)
print(f"  Explicit highest=0.0: highest=${p3.highest:.2f} (expected $10.00) {'PASS' if p3.highest == 10.0 else 'FAIL'}")

# Test 10: Verify backtest alignment
print(f"\n{'='*70}")
print("BACKTEST vs LIVE TARGET ALIGNMENT CHECK")
print(f"{'='*70}")
# Backtest uses strategy.build_trade_plan()
# Live uses calc_targets_live()
# They should produce the same targets
for entry, open_p in [(6.50, 7.00), (7.87, 6.71), (6.79, 7.78), (5.50, 6.00)]:
    live_targets = calc_targets_live(entry, open_p)[0]
    bt_plan = build_trade_plan(entry, open_p)
    bt_targets = bt_plan.targets
    match = live_targets == bt_targets
    print(f"  entry=${entry}, open=${open_p}: live={live_targets} bt={bt_targets} {'MATCH' if match else 'MISMATCH!'}")

print(f"\n{'='*70}")
print("ALL TESTS COMPLETE")
print(f"{'='*70}")
