"""Monte Carlo simulation for testing the Stone 1.0 trading system.

Generates random continuous price curves and tests:
1. Backtest engine (evaluate_trade_stone) — 6-tier targets, skip-gap, trailing stop
2. Ladder sell order simulation — sequential tier placement, natural skip-gap
3. Edge cases — stop loss, time limit, partial fills, flash jumps
"""

import random
import math
import sys
import os
from dataclasses import dataclass, field
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
import config
from strategy import (
    calc_price_at_retracement, calc_stop_price, calc_atr,
    build_trade_plan, evaluate_trade_stone, TradePlan, TradeResult,
)


# ── Price path generator ─────────────────────────────────────────────

SCENARIOS = [
    "gradual_rise", "fast_jump_t3", "fast_jump_t6",
    "stop_loss", "t1_then_trail", "t3_then_trail",
    "volatile_oscillate", "time_limit", "flat_then_rise",
    "gap_fail", "slow_grind", "all_tiers_then_rise",
]


def _gbm_step(price, drift, vol, dt=1/390):
    """Geometric Brownian Motion step for 1-minute bar."""
    shock = random.gauss(0, 1)
    return price * math.exp((drift - 0.5 * vol**2) * dt + vol * math.sqrt(dt) * shock)


def generate_price_path(prev_close, gap_pct, scenario, volatility=0.02, n_bars=390):
    """Generate 1-min bar sequence simulating a gap-down trading day.

    Returns list of dicts: [{"open", "high", "low", "close", "volume"}, ...]
    """
    open_price = round(prev_close * (1 - gap_pct), 4)
    bars = []

    # Phase 1: Pullback — price drops below open, then confirms (5 bars)
    # Simulate the initial drop and recovery for entry detection
    pullback_depth = open_price * random.uniform(0.02, 0.08)  # 2-8% below open
    pullback_price = round(open_price - pullback_depth, 4)

    # Generate pullback bars (price drops then recovers slightly)
    price = open_price
    pb_len = random.randint(3, 15)  # How many bars the pullback takes
    for i in range(pb_len):
        # Drop phase (first half) or recovery phase (second half)
        if i < pb_len // 2:
            drift = -0.3 * volatility  # Downward
        elif i < pb_len // 2 + 5:
            drift = 0.1 * volatility   # Small recovery (confirmation)
        else:
            drift = 0.05 * volatility  # Slow recovery
        price = _gbm_step(price, drift, volatility)
        # Force minimum to be near pullback price during drop phase
        if i < pb_len // 2:
            price = min(price, open_price - pullback_depth * (1 - i / (pb_len // 2)))
        bar = {
            "open": round(price * (1 + random.uniform(-0.001, 0.001)), 4),
            "high": round(price * (1 + random.uniform(0, 0.003)), 4),
            "low": round(price * (1 - random.uniform(0, 0.003)), 4),
            "close": round(price, 4),
            "volume": random.randint(1000, 50000),
        }
        bars.append(bar)

    # Entry point = pullback_price (lowest point below open)
    entry_price = pullback_price

    # Phase 2: Scenario-specific path after entry
    remaining = n_bars - len(bars)
    phase2_bars = _generate_scenario_path(entry_price, open_price, scenario, volatility, remaining)
    bars.extend(phase2_bars)

    return bars, open_price, entry_price


def _generate_scenario_path(entry_price, open_price, scenario, volatility, n_bars):
    """Generate bars after entry for a specific scenario."""
    # Calculate targets for scenario planning
    retracements = config.PROFIT_RETRACEMENT_TIERS
    caps = config.TARGET_CAP_TIERS
    targets = []
    for i in range(6):
        ret_price = calc_price_at_retracement(entry_price, open_price, retracements[i])
        cap_price = round(entry_price * (1 + caps[i]), 2)
        targets.append(min(ret_price, cap_price))

    stop_price = calc_stop_price(entry_price, entry_price * 0.03)  # Approximate ATR
    trail_pcts = config.TRAILING_STOP_PCTS

    bars = []
    price = entry_price

    if scenario == "gradual_rise":
        # Price rises steadily through all 6 tiers over ~200 bars
        target_final = targets[5] * 1.05
        drift = math.log(target_final / entry_price) / n_bars
        for i in range(n_bars):
            price = _gbm_step(price, drift + 0.5 * volatility**2, volatility * 0.3)
            bar = _make_bar(price, volatility * 0.3)
            bars.append(bar)

    elif scenario == "fast_jump_t3":
        # Price jumps to T3 quickly (within ~30 bars), then slow rise
        for i in range(n_bars):
            if i < 30:
                drift = math.log(targets[2] / entry_price) / 30 + 0.5 * volatility**2
                price = _gbm_step(price, drift, volatility)
            else:
                price = _gbm_step(price, 0.001, volatility * 0.5)
            bar = _make_bar(price, volatility)
            bars.append(bar)

    elif scenario == "fast_jump_t6":
        # Price jumps to T6 very quickly (within ~50 bars), then stabilize
        for i in range(n_bars):
            if i < 50:
                drift = math.log(targets[5] / entry_price) / 50 + 0.5 * volatility**2
                price = _gbm_step(price, drift, volatility * 0.5)
            else:
                price = _gbm_step(price, 0, volatility * 0.3)
            bar = _make_bar(price, volatility * 0.5)
            bars.append(bar)

    elif scenario == "stop_loss":
        # Price drops to stop loss after entry
        for i in range(n_bars):
            drift = math.log(stop_price / entry_price) / 20 - 0.5 * volatility**2
            if i < 20:
                price = _gbm_step(price, drift, volatility)
            else:
                price = _gbm_step(price, -0.001, volatility)
            bar = _make_bar(price, volatility)
            # Force price to reach stop within 20 bars
            if i == 15:
                bar["low"] = stop_price * 0.98
            bars.append(bar)

    elif scenario == "t1_then_trail":
        # Rise to T1, then drop to trailing stop level
        tsp = round(targets[0] * (1 - trail_pcts[0]), 4)
        for i in range(n_bars):
            if i < 30:
                drift = math.log(targets[0] / entry_price) / 30 + 0.5 * volatility**2
                price = _gbm_step(price, drift, volatility)
            elif i < 60:
                price = _gbm_step(price, -0.002, volatility * 1.5)
            else:
                price = _gbm_step(price, 0, volatility)
            bar = _make_bar(price, volatility)
            bars.append(bar)

    elif scenario == "t3_then_trail":
        # Rise through T1-T3, then drop to trailing stop
        for i in range(n_bars):
            if i < 60:
                drift = math.log(targets[2] / entry_price) / 60 + 0.5 * volatility**2
                price = _gbm_step(price, drift, volatility * 0.5)
            elif i < 120:
                price = _gbm_step(price, -0.003, volatility * 1.5)
            else:
                price = _gbm_step(price, 0, volatility)
            bar = _make_bar(price, volatility)
            bars.append(bar)

    elif scenario == "volatile_oscillate":
        # Price oscillates around entry, hitting T1 then dropping back
        for i in range(n_bars):
            cycle = math.sin(i * 0.05) * 0.01
            price = _gbm_step(price, cycle, volatility * 2)
            bar = _make_bar(price, volatility * 2)
            bars.append(bar)

    elif scenario == "time_limit":
        # Price stays near entry for 40+ minutes (8 5-min bars), slight rise
        for i in range(n_bars):
            if i < 200:  # First 200 minutes (~40 5-min bars)
                price = _gbm_step(price, 0.0001, volatility * 0.2)
            else:
                price = _gbm_step(price, 0.001, volatility)
            bar = _make_bar(price, volatility * 0.2)
            bars.append(bar)

    elif scenario == "flat_then_rise":
        # Flat for 40 min then sharp rise
        for i in range(n_bars):
            if i < 200:
                price = _gbm_step(price, 0, volatility * 0.1)
            else:
                drift = math.log(targets[3] / price) / 100 + 0.5 * volatility**2
                price = _gbm_step(price, drift, volatility * 0.5)
            bar = _make_bar(price, volatility * 0.3)
            bars.append(bar)

    elif scenario == "gap_fail":
        # Price drops continuously after pullback
        for i in range(n_bars):
            price = _gbm_step(price, -0.005, volatility * 1.5)
            bar = _make_bar(price, volatility * 1.5)
            if i == 10:
                bar["low"] = stop_price * 0.95
            bars.append(bar)

    elif scenario == "slow_grind":
        # Price slowly grinds up to T1, barely profitable
        for i in range(n_bars):
            drift = math.log(targets[0] / entry_price) / n_bars + 0.5 * volatility**2
            price = _gbm_step(price, drift, volatility * 0.15)
            bar = _make_bar(price, volatility * 0.15)
            bars.append(bar)

    elif scenario == "all_tiers_then_rise":
        # Rise through all 6 tiers, then continue rising (trailing stop exits later)
        target_high = targets[5] * 1.2
        for i in range(n_bars):
            if i < 100:
                drift = math.log(target_high / entry_price) / 100 + 0.5 * volatility**2
                price = _gbm_step(price, drift, volatility * 0.3)
            else:
                price = _gbm_step(price, 0.001, volatility * 0.5)
            bar = _make_bar(price, volatility * 0.3)
            bars.append(bar)

    else:
        # Default: moderate rise with random volatility
        for i in range(n_bars):
            price = _gbm_step(price, 0.002, volatility)
            bar = _make_bar(price, volatility)
            bars.append(bar)

    return bars


def _make_bar(price, volatility):
    """Create a bar dict from current price."""
    spread = abs(price * volatility * 0.1)  # Intra-bar spread
    return {
        "open": round(price + random.uniform(-spread, spread), 4),
        "high": round(price + abs(random.uniform(0, spread * 2)), 4),
        "low": round(price - abs(random.uniform(0, spread * 2)), 4),
        "close": round(price + random.uniform(-spread, spread), 4),
        "volume": random.randint(1000, 50000),
    }


def aggregate_to_5min(bars_1m):
    """Aggregate 1-minute bars into 5-minute bars."""
    bars_5m = []
    for i in range(0, len(bars_1m), 5):
        chunk = bars_1m[i:i+5]
        if not chunk:
            break
        bar = {
            "open": chunk[0]["open"],
            "high": max(b["high"] for b in chunk),
            "low": min(b["low"] for b in chunk),
            "close": chunk[-1]["close"],
            "volume": sum(b["volume"] for b in chunk),
        }
        bars_5m.append(bar)
    return bars_5m


# ── Simulated broker (mocks Alpaca order management) ─────────────────

@dataclass
class SimOrder:
    id: str
    symbol: str
    side: str           # "buy" / "sell"
    order_type: str     # "limit" / "stop_limit" / "trailing_stop" / "market"
    qty: int
    limit_price: float | None
    stop_price: float | None
    trail_pct: float | None   # For trailing stop (as percentage, e.g., 2.0)
    status: str = "new"
    filled_qty: int = 0
    filled_price: float = 0.0


class SimBroker:
    """Simulated broker for testing ladder sell order logic."""

    def __init__(self):
        self.orders = {}        # {order_id: SimOrder}
        self._next_id = 1
        self._highest = {}      # {symbol: float} for trailing stop calculation

    def _gen_id(self):
        self._next_id += 1
        return f"SIM-{self._next_id}"

    def place_sell_limit(self, symbol, shares, price):
        oid = self._gen_id()
        self.orders[oid] = SimOrder(
            id=oid, symbol=symbol, side="sell", order_type="limit",
            qty=shares, limit_price=price, stop_price=None, trail_pct=None,
        )
        return oid

    def place_stop_limit_sell(self, symbol, shares, stop_price, limit_price):
        oid = self._gen_id()
        self.orders[oid] = SimOrder(
            id=oid, symbol=symbol, side="sell", order_type="stop_limit",
            qty=shares, limit_price=limit_price, stop_price=stop_price, trail_pct=None,
        )
        return oid

    def place_trailing_stop(self, symbol, shares, trail_pct):
        oid = self._gen_id()
        self.orders[oid] = SimOrder(
            id=oid, symbol=symbol, side="sell", order_type="trailing_stop",
            qty=shares, limit_price=None, stop_price=None, trail_pct=trail_pct,
        )
        return oid

    def cancel_order(self, order_id):
        if order_id in self.orders:
            self.orders[order_id].status = "canceled"

    def cancel_all_for_symbol(self, symbol, exclude_ids=None):
        exclude = exclude_ids or set()
        for oid, order in self.orders.items():
            if order.symbol == symbol and order.side == "sell" and oid not in exclude:
                order.status = "canceled"

    def check_fills(self, current_price, symbol):
        """Check all open orders for this symbol; fill if conditions met."""
        fills = []  # [(order_id, filled_qty, filled_price)]
        for oid, order in list(self.orders.items()):
            if order.status != "new" or order.symbol != symbol:
                continue

            filled = False
            fill_price = 0.0

            if order.order_type == "limit" and order.side == "sell":
                # Limit sell fills when market >= limit_price
                if current_price >= order.limit_price:
                    fill_price = min(current_price, order.limit_price * 1.001)  # Slight slippage
                    filled = True

            elif order.order_type == "stop_limit" and order.side == "sell":
                # Stop-limit fills when price drops to stop, then limit
                if current_price <= order.stop_price:
                    fill_price = min(order.limit_price, current_price)
                    filled = True

            elif order.order_type == "trailing_stop" and order.side == "sell":
                # Trailing stop fills when price drops trail_pct below highest
                highest = self._highest.get(symbol, current_price)
                if highest > 0:
                    tsp = round(highest * (1 - order.trail_pct / 100), 2)
                    if current_price <= tsp:
                        fill_price = current_price
                        filled = True

            elif order.order_type == "market" and order.side == "sell":
                fill_price = current_price
                filled = True

            if filled:
                order.status = "filled"
                order.filled_qty = order.qty
                order.filled_price = fill_price
                fills.append((oid, order.qty, fill_price))

        return fills

    def update_highest(self, symbol, price):
        if symbol not in self._highest or price > self._highest[symbol]:
            self._highest[symbol] = price

    def get_open_sell_orders(self, symbol, is_ladder_tier_only=False, pending_sells=None):
        """Get open sell order IDs for a symbol from pending_sells dict."""
        if pending_sells is None:
            return []
        result = []
        for oid, info in pending_sells.items():
            if info["symbol"] == symbol:
                order = self.orders.get(oid)
                if order and order.status == "new":
                    if is_ladder_tier_only and not info.get("is_ladder_tier"):
                        continue
                    result.append(oid)
        return result


# ── Ladder simulation ────────────────────────────────────────────────

@dataclass
class LadderPosition:
    symbol: str
    entry_price: float
    shares: int
    open_price: float
    stop_price: float
    targets: list
    sell_ratios: list
    trail_pcts: list
    target_mode: str = "retracement"
    remaining_shares: int = 0
    highest: float = 0.0
    reached_list: list = None
    sold_shares_list: list = None
    next_tier_idx: int = 0
    bar_count: int = 0
    time_limit_active: bool = False
    exit_reason: str = ""
    exit_price: float = 0.0
    partial_sells: list = field(default_factory=list)  # [(price, shares)]
    pnl: float = 0.0

    def __post_init__(self):
        self.remaining_shares = self.shares
        self.highest = self.entry_price
        if self.reached_list is None:
            self.reached_list = [False] * len(self.targets)
        if self.sold_shares_list is None:
            self.sold_shares_list = [0] * len(self.targets)


TARGET_LIMIT_BUFFER = 0.003
STOP_LIMIT_BUFFER = 0.03
TIME_LIMIT_BARS = getattr(config, "FIRST_TRADE_TIME_LIMIT_BARS", 8)


def run_ladder_simulation(bars_1m, open_price, entry_price, prev_close):
    """Simulate the ladder sell system processing 1-min bars sequentially."""
    broker = SimBroker()

    # Calculate targets, stop, etc.
    bars_5m_sample = aggregate_to_5min(bars_1m[:20])
    atr = calc_atr(bars_5m_sample, period=14) if len(bars_5m_sample) >= 2 else entry_price * 0.03
    stop = calc_stop_price(entry_price, atr)
    targets, sell_ratios, trail_pcts, target_mode = calc_targets(entry_price, open_price)
    shares = 8  # Fixed 8 shares for testing

    pos = LadderPosition(
        symbol="TEST", entry_price=entry_price, shares=shares,
        open_price=open_price, stop_price=stop,
        targets=targets, sell_ratios=sell_ratios, trail_pcts=trail_pcts,
        target_mode=target_mode,
    )

    pending_sells = {}  # {order_id: {"symbol", "shares", "tier_idx", "affected_tiers", "is_ladder_tier"}}
    protective_oid = None
    position_active = True

    # Place initial protective stop + T1 ladder sell (same as live script)
    protective_oid = broker.place_stop_limit_sell(
        pos.symbol, pos.remaining_shares, stop,
        round(stop * (1 - STOP_LIMIT_BUFFER), 2),
    )

    if pos.targets:
        t1_shares = max(1, int(pos.shares * pos.sell_ratios[0]))
        t1_price = round(pos.targets[0] * (1 - TARGET_LIMIT_BUFFER), 2)
        t1_oid = broker.place_sell_limit(pos.symbol, t1_shares, t1_price)
        pending_sells[t1_oid] = {
            "symbol": pos.symbol, "shares": t1_shares, "tier_idx": 0,
            "affected_tiers": [0], "is_ladder_tier": True,
        }
        pos.sold_shares_list[0] = t1_shares
        pos.remaining_shares -= t1_shares
        pos.next_tier_idx = 1

    # Process each 1-min bar
    for bi, bar in enumerate(bars_1m):
        if not position_active:
            break

        cur_price = bar["close"]
        bar_low = bar["low"]
        bar_high = bar["high"]

        # Update highest
        if bar_high > pos.highest:
            pos.highest = bar_high
        broker.update_highest(pos.symbol, pos.highest)

        # Count 5-min bars (for time limit)
        if bi % 5 == 0:
            pos.bar_count += 1

        # Check order fills
        fills = broker.check_fills(cur_price, pos.symbol)

        # Process fills
        for fill_oid, fill_qty, fill_price in fills:
            if fill_oid in pending_sells:
                info = pending_sells[fill_oid]
                tier_idx = info.get("tier_idx")
                if info.get("is_ladder_tier") and tier_idx is not None:
                    # Ladder tier fill
                    pos.reached_list[tier_idx] = True
                    pos.sold_shares_list[tier_idx] = fill_qty
                    pos.next_tier_idx = tier_idx + 1
                    pos.partial_sells.append((fill_price, fill_qty))
                    # Place trailing stop for remaining
                    if pos.remaining_shares > 0 and pos.trail_pcts:
                        # Cancel old protective
                        if protective_oid:
                            broker.cancel_order(protective_oid)
                        trail_pct = pos.trail_pcts[min(tier_idx, len(pos.trail_pcts) - 1)]
                        protective_oid = broker.place_trailing_stop(
                            pos.symbol, pos.remaining_shares, trail_pct * 100,
                        )
                elif fill_oid == protective_oid:
                    # Protective stop triggered — exit all remaining
                    pos.exit_reason = "protective_stop"
                    pos.exit_price = fill_price
                    pos.pnl = _calc_pnl(pos, fill_price)
                    position_active = False
                    # Cancel pending ladder sells
                    for pid in list(pending_sells.keys()):
                        broker.cancel_order(pid)
                        # Roll back state
                        pinfo = pending_sells[pid]
                        for t in pinfo.get("affected_tiers", []):
                            if t < len(pos.sold_shares_list):
                                pos.sold_shares_list[t] = 0
                            if t < len(pos.reached_list):
                                pos.reached_list[t] = False
                        pos.remaining_shares += pinfo["shares"]
                    break

                del pending_sells[fill_oid]

        if not position_active:
            break

        # ── Main loop: ladder placement logic (mirrors live script) ──

        # Stop loss polled fallback
        if cur_price <= pos.stop_price:
            pos.exit_reason = "stop_loss"
            pos.exit_price = pos.stop_price
            pos.pnl = _calc_pnl(pos, pos.stop_price)
            position_active = False
            for pid in list(pending_sells.keys()):
                broker.cancel_order(pid)
                pinfo = pending_sells[pid]
                for t in pinfo.get("affected_tiers", []):
                    pos.sold_shares_list[t] = 0
                    pos.reached_list[t] = False
                pos.remaining_shares += pinfo["shares"]
            if protective_oid:
                broker.cancel_order(protective_oid)
            break

        # Time limit
        has_any_filled = any(pos.reached_list[:pos.next_tier_idx]) if pos.reached_list else False
        if TIME_LIMIT_BARS > 0 and not has_any_filled and pos.bar_count >= TIME_LIMIT_BARS:
            pos.time_limit_active = True
        if pos.time_limit_active and cur_price >= pos.entry_price:
            pos.exit_reason = "time_limit_exit"
            pos.exit_price = cur_price
            pos.pnl = _calc_pnl(pos, cur_price)
            position_active = False
            for pid in list(pending_sells.keys()):
                broker.cancel_order(pid)
                pinfo = pending_sells[pid]
                for t in pinfo.get("affected_tiers", []):
                    pos.sold_shares_list[t] = 0
                    pos.reached_list[t] = False
                pos.remaining_shares += pinfo["shares"]
                pos.next_tier_idx = pinfo.get("tier_idx", pos.next_tier_idx)
            if protective_oid:
                broker.cancel_order(protective_oid)
            break

        # Place next ladder tier sell
        has_pending = any(
            info["symbol"] == pos.symbol and info.get("is_ladder_tier")
            for info in pending_sells.values()
        )
        if pos.next_tier_idx < len(pos.targets) and not has_pending and position_active:
            ti = pos.next_tier_idx
            tier_shares = max(1, int(pos.shares * pos.sell_ratios[ti]))
            tier_shares = min(tier_shares, pos.remaining_shares)
            if tier_shares > 0:
                sell_price = round(pos.targets[ti] * (1 - TARGET_LIMIT_BUFFER), 2)
                oid = broker.place_sell_limit(pos.symbol, tier_shares, sell_price)
                if oid:
                    pending_sells[oid] = {
                        "symbol": pos.symbol, "shares": tier_shares, "tier_idx": ti,
                        "affected_tiers": [ti], "is_ladder_tier": True,
                    }
                    pos.sold_shares_list[ti] = tier_shares
                    pos.remaining_shares -= tier_shares

        # Trailing stop polled fallback (same logic as live script)
        if pos.reached_list and any(pos.reached_list) and pos.remaining_shares > 0 and position_active:
            pct = _get_trailing_pct(pos)
            tsp = round(pos.highest * (1 - pct), 2)
            tsp = max(tsp, pos.entry_price)
            if cur_price <= tsp:
                # Cancel pending ladder sells
                for pid in list(pending_sells.keys()):
                    if pending_sells[pid].get("is_ladder_tier"):
                        broker.cancel_order(pid)
                        pinfo = pending_sells[pid]
                        for t in pinfo.get("affected_tiers", []):
                            pos.sold_shares_list[t] = 0
                            pos.reached_list[t] = False
                        pos.remaining_shares += pinfo["shares"]
                        pos.next_tier_idx = pinfo.get("tier_idx", pos.next_tier_idx)
                        del pending_sells[pid]
                pos.exit_reason = "trailing_stop"
                pos.exit_price = tsp
                pos.pnl = _calc_pnl(pos, tsp)
                position_active = False
                if protective_oid:
                    broker.cancel_order(protective_oid)
                break

    # Force close at end if still active
    if position_active and pos.remaining_shares > 0:
        last_price = bars_1m[-1]["close"] if bars_1m else pos.entry_price
        pos.exit_reason = "force_close"
        pos.exit_price = last_price
        pos.pnl = _calc_pnl(pos, last_price)

    return pos


def _calc_pnl(pos, exit_price):
    """Calculate total P&L including partial sells and remaining."""
    pnl = 0.0
    for sell_price, sell_qty in pos.partial_sells:
        pnl += (sell_price - pos.entry_price) * sell_qty
    pnl += (exit_price - pos.entry_price) * pos.remaining_shares
    return round(pnl, 2)


def _get_trailing_pct(pos):
    """Get trailing pct from highest reached tier."""
    trail_pcts = pos.trail_pcts
    for ti in range(len(pos.reached_list) - 1, -1, -1):
        if pos.reached_list[ti]:
            return trail_pcts[ti] if ti < len(trail_pcts) else trail_pcts[-1]
    return trail_pcts[0]


def calc_targets(entry_price, open_price):
    """Calculate 6-tier targets (same logic as live script)."""
    retracements = getattr(config, "PROFIT_RETRACEMENT_TIERS", [0.25, 0.50, 0.75, 1.00, 1.25, 1.50])
    caps = getattr(config, "TARGET_CAP_TIERS", [0.05, 0.10, 0.15, 0.20, 0.25, 0.35])
    sell_ratios = getattr(config, "PARTIAL_SELL_RATIOS", [1/8]*6)
    trail_pcts = getattr(config, "TRAILING_STOP_PCTS", [0.02, 0.025, 0.03, 0.035, 0.04, 0.05])

    if entry_price >= open_price:
        targets = [round(entry_price * (1 + caps[i]), 2) for i in range(6)]
        target_mode = "capped"
    else:
        targets = []
        any_capped = False
        for i in range(6):
            ret_price = calc_price_at_retracement(entry_price, open_price, retracements[i])
            cap_price = round(entry_price * (1 + caps[i]), 2)
            t = min(ret_price, cap_price)
            if t < ret_price:
                any_capped = True
            targets.append(t)
        target_mode = "capped" if any_capped else "retracement"

    return targets, sell_ratios, trail_pcts, target_mode


# ── Backtest engine test ─────────────────────────────────────────────

def run_backtest_engine(bars_5m, open_price, entry_price, prev_close):
    """Run evaluate_trade_stone with 5-min bars."""
    atr = calc_atr(bars_5m[:20], period=14) if len(bars_5m) >= 2 else entry_price * 0.03
    stop = calc_stop_price(entry_price, atr)

    plan = build_trade_plan("TEST", open_price, entry_price, atr,
                            position_size=entry_price * 8)

    # Find entry bar index (first bar where low < open_price)
    entry_bar_idx = -1
    for i in range(len(bars_5m)):
        if bars_5m[i]["low"] < open_price:
            entry_bar_idx = i
            break
    if entry_bar_idx < 0:
        entry_bar_idx = 0

    remaining_list = bars_5m[entry_bar_idx + 1:]
    force_close_price = remaining_list[-1]["close"] if remaining_list else entry_price

    time_limit = getattr(config, "FIRST_TRADE_TIME_LIMIT_BARS", 0)

    result = evaluate_trade_stone(
        plan, remaining_list, force_close_price,
        time_limit_bars=time_limit,
    )

    return result


# ── Monte Carlo runner ───────────────────────────────────────────────

def run_monte_carlo(n_simulations=5000):
    """Run thousands of random scenarios and collect statistics."""
    all_bt_results = []
    all_ladder_results = []
    comparisons = []
    scenario_counts = defaultdict(int)
    scenario_pnl = defaultdict(list)
    exit_reason_counts = defaultdict(int)
    tier_trigger_counts = [0] * 6
    total_tier_triggers = 0

    print(f"Running Monte Carlo simulation: {n_simulations} scenarios")
    print(f"Scenarios: {SCENARIOS}")
    print()

    for i in range(n_simulations):
        scenario = random.choice(SCENARIOS)
        gap_pct = random.uniform(0.03, 0.20)
        volatility = random.uniform(0.005, 0.04)
        prev_close = random.uniform(5, 100)

        bars_1m, open_price, entry_price = generate_price_path(
            prev_close, gap_pct, scenario, volatility,
        )
        bars_5m = aggregate_to_5min(bars_1m)

        # Run backtest engine
        if len(bars_5m) >= 3:
            try:
                result_bt = run_backtest_engine(bars_5m, open_price, entry_price, prev_close)
                all_bt_results.append(result_bt)
            except Exception as e:
                result_bt = None

        # Run ladder simulation
        try:
            result_ladder = run_ladder_simulation(bars_1m, open_price, entry_price, prev_close)
            all_ladder_results.append(result_ladder)
            scenario_counts[scenario] += 1
            scenario_pnl[scenario].append(result_ladder.pnl)
            exit_reason_counts[result_ladder.exit_reason] += 1

            # Count tier triggers
            for ti in range(6):
                if result_ladder.reached_list and ti < len(result_ladder.reached_list) and result_ladder.reached_list[ti]:
                    tier_trigger_counts[ti] += 1
                    total_tier_triggers += 1
        except Exception as e:
            print(f"  ERROR in scenario {scenario}: {e}")
            continue

        # Compare results
        if result_bt and result_ladder and result_ladder.pnl != 0:
            pnl_diff = result_ladder.pnl - result_bt.pnl
            comparisons.append({
                "scenario": scenario,
                "pnl_bt": result_bt.pnl,
                "pnl_ladder": result_ladder.pnl,
                "pnl_diff": pnl_diff,
                "exit_bt": result_bt.exit_reason,
                "exit_ladder": result_ladder.exit_reason,
            })

        if (i + 1) % 500 == 0:
            print(f"  Progress: {i+1}/{n_simulations} ({(i+1)/n_simulations*100:.0f}%)")

    # ── Print results ──
    print(f"\n{'='*80}")
    print(f" MONTE CARLO SIMULATION RESULTS ({n_simulations} scenarios)")
    print(f"{'='*80}")

    # Overall statistics
    pnls = [r.pnl for r in all_ladder_results if r.pnl != 0]
    if pnls:
        print(f"\n  ── Overall Statistics ──")
        print(f"  Total simulations: {n_simulations}")
        print(f"  Trades with P&L: {len(pnls)}")
        print(f"  Avg P&L: ${sum(pnls)/len(pnls):.2f}")
        print(f"  Median P&L: ${sorted(pnls)[len(pnls)//2]:.2f}")
        print(f"  Std P&L: ${(sum((p-sum(pnls)/len(pnls))**2 for p in pnls)/len(pnls))**0.5:.2f}")
        print(f"  Best P&L: ${max(pnls):.2f}")
        print(f"  Worst P&L: ${min(pnls):.2f}")
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p <= 0)
        print(f"  Win rate: {wins/len(pnls)*100:.1f}% ({wins} wins / {losses} losses)")
        if losses > 0:
            pf = sum(p for p in pnls if p > 0) / abs(sum(p for p in pnls if p < 0))
            print(f"  Profit factor: {pf:.2f}")

    # Scenario breakdown
    print(f"\n  ── Scenario Breakdown ──")
    for scenario in SCENARIOS:
        count = scenario_counts.get(scenario, 0)
        spnls = scenario_pnl.get(scenario, [])
        avg_pnl = sum(spnls)/len(spnls) if spnls else 0
        win_rate = sum(1 for p in spnls if p > 0)/len(spnls)*100 if spnls else 0
        print(f"  {scenario:20s}: {count:4d} runs, avg P&L=${avg_pnl:.2f}, win={win_rate:.1f}%")

    # Exit reason breakdown
    print(f"\n  ── Exit Reason Breakdown ──")
    for reason, count in sorted(exit_reason_counts.items(), key=lambda x: -x[1]):
        pct = count / len(all_ladder_results) * 100
        print(f"  {reason:25s}: {count:4d} ({pct:.1f}%)")

    # Tier trigger rates
    print(f"\n  ── Tier Trigger Rates ──")
    retracements = config.PROFIT_RETRACEMENT_TIERS
    for ti in range(6):
        rate = tier_trigger_counts[ti] / len(all_ladder_results) * 100
        label = f"T{ti+1} ({int(retracements[ti]*100)}%)"
        print(f"  {label:15s}: {tier_trigger_counts[ti]:5d} triggers ({rate:.1f}%)")

    # Backtest vs Ladder comparison
    if comparisons:
        print(f"\n  ── Backtest vs Ladder Comparison ──")
        diffs = [c["pnl_diff"] for c in comparisons]
        avg_diff = sum(diffs) / len(diffs)
        same_exit = sum(1 for c in comparisons if c["exit_bt"] == c["exit_ladder"])
        print(f"  Comparisons: {len(comparisons)}")
        print(f"  Avg P&L diff (ladder - backtest): ${avg_diff:.2f}")
        print(f"  Same exit reason: {same_exit}/{len(comparisons)} ({same_exit/len(comparisons)*100:.1f}%)")

        # Breakdown by exit reason mismatch
        mismatch_reasons = defaultdict(int)
        for c in comparisons:
            if c["exit_bt"] != c["exit_ladder"]:
                mismatch_reasons[f"{c['exit_bt']}→{c['exit_ladder']}"] += 1
        if mismatch_reasons:
            print(f"  Exit reason mismatches:")
            for key, count in sorted(mismatch_reasons.items(), key=lambda x: -x[1]):
                print(f"    {key}: {count}")

    print(f"\n{'='*80}")
    print(f" SIMULATION COMPLETE")
    print(f"{'='*80}")


# ── Edge case tests ──────────────────────────────────────────────────

def run_edge_case_tests():
    """Test specific edge cases that random simulation may miss."""
    print(f"\n{'='*80}")
    print(f" EDGE CASE TESTS")
    print(f"{'='*80}")

    test_cases = []

    # 1. Exact target price (price exactly at T1, not above)
    bars, open_price, entry_price = _make_exact_target_test()
    test_cases.append(("exact_target", bars, open_price, entry_price))

    # 2. Flash jump to T3 then immediate drop
    bars, open_price, entry_price = _make_flash_jump_test()
    test_cases.append(("flash_jump_then_drop", bars, open_price, entry_price))

    # 3. T1 fill then immediate drop below entry
    bars, open_price, entry_price = _make_t1_then_drop_test()
    test_cases.append(("t1_then_immediate_drop", bars, open_price, entry_price))

    # 4. All 6 tiers trigger then price keeps rising
    bars, open_price, entry_price = _make_all_tiers_then_rise_test()
    test_cases.append(("all_tiers_then_continued_rise", bars, open_price, entry_price))

    # 5. Stop loss then recovery (stop already triggered, irreversible)
    bars, open_price, entry_price = _make_stop_then_recovery_test()
    test_cases.append(("stop_then_recovery", bars, open_price, entry_price))

    # 6. Time limit with exact breakeven price
    bars, open_price, entry_price = _make_time_limit_breakeven_test()
    test_cases.append(("time_limit_exact_breakeven", bars, open_price, entry_price))

    # 7. Instant T1 (entry price already near T1)
    bars, open_price, entry_price = _make_instant_t1_test()
    test_cases.append(("instant_t1", bars, open_price, entry_price))

    passed = 0
    failed = 0

    for name, bars_1m, open_price, entry_price in test_cases:
        bars_5m = aggregate_to_5min(bars_1m)

        # Run ladder simulation
        result = run_ladder_simulation(bars_1m, open_price, entry_price, 100.0)

        # Verify basic sanity
        issues = []
        if result.remaining_shares < 0:
            issues.append(f"remaining_shares={result.remaining_shares} < 0")
        if result.pnl == 0 and result.exit_reason == "":
            issues.append("no exit recorded")
        # total_sold = partial_sells (confirmed fills) + exit shares (if position closed)
        partial_total = sum(q for _, q in result.partial_sells)
        exit_sold = result.shares - result.remaining_shares if result.exit_reason else 0
        total_sold = partial_total + max(0, result.remaining_shares if result.exit_reason else 0)
        # The correct check: confirmed fills + exit close should <= shares
        # partial_sells + (remaining_shares on exit) <= shares
        if partial_total + result.remaining_shares > result.shares:
            issues.append(f"partial={partial_total} + remaining={result.remaining_shares} > shares={result.shares}")

        status = "PASS" if not issues else "FAIL"
        if issues:
            failed += 1
        else:
            passed += 1

        print(f"  {name:30s}: {status} | exit={result.exit_reason}, P&L=${result.pnl:.2f}, "
              f"remaining={result.remaining_shares}, tiers_reached={sum(result.reached_list)}, "
              f"partial_sells={len(result.partial_sells)}")
        if issues:
            for issue in issues:
                print(f"    ISSUE: {issue}")

    print(f"\n  Edge cases: {passed} passed, {failed} failed out of {len(test_cases)}")


def _make_exact_target_test():
    """Price exactly equals T1 target, no overshoot."""
    prev_close = 20.0
    open_price = 18.0  # 10% gap down
    entry_price = 17.0  # Pullback
    targets, _, _, _ = calc_targets(entry_price, open_price)
    t1 = targets[0]

    bars = []
    price = entry_price
    # 5 bars of slow rise to exactly T1
    for i in range(5):
        step = (t1 - entry_price) / 5
        price += step
        bar = {"open": round(price - 0.01, 4), "high": round(price, 4),
               "low": round(price - 0.02, 4), "close": round(price, 4),
               "volume": 1000}
        bars.append(bar)
    # Then flat at T1 level
    for i in range(385):
        bar = {"open": round(t1 - 0.01, 4), "high": round(t1 + 0.01, 4),
               "low": round(t1 - 0.02, 4), "close": round(t1, 4),
               "volume": 1000}
        bars.append(bar)
    return bars, open_price, entry_price


def _make_flash_jump_test():
    """Price jumps to T3 in one bar, then drops to trailing stop."""
    prev_close = 20.0
    open_price = 18.0
    entry_price = 17.0
    targets, _, trail_pcts, _ = calc_targets(entry_price, open_price)
    t3 = targets[2]

    bars = []
    price = entry_price
    # 10 bars of flat
    for i in range(10):
        bar = _make_bar(price, 0.01)
        bars.append(bar)
    # Flash jump bar — high reaches T3 but close drops
    bar = {"open": round(entry_price + 0.1, 4), "high": round(t3 + 0.5, 4),
           "low": round(entry_price, 4), "close": round(entry_price + 0.2, 4),
           "volume": 10000}
    bars.append(bar)
    # Then drop to trailing stop
    tsp = round(t3 * (1 - trail_pcts[0]), 2)  # Use T3's trailing pct... actually use highest reached tier
    price = entry_price + 0.2
    for i in range(375):
        price *= 0.997
        bar = _make_bar(price, 0.01)
        bars.append(bar)
    return bars, open_price, entry_price


def _make_t1_then_drop_test():
    """T1 fills, then price drops below entry (trailing stop 2% triggers)."""
    prev_close = 20.0
    open_price = 18.0
    entry_price = 17.0
    targets, _, _, _ = calc_targets(entry_price, open_price)
    t1 = targets[0]
    tsp_t1 = round(t1 * (1 - 0.02), 2)  # 2% trailing after T1

    bars = []
    price = entry_price
    # Rise to T1
    for i in range(20):
        step = (t1 - entry_price) / 20
        price += step
        bar = _make_bar(price, 0.005)
        bars.append(bar)
    # Drop to trailing stop level
    for i in range(100):
        price *= 0.995
        bar = _make_bar(price, 0.01)
        bars.append(bar)
    # Continue flat
    for i in range(270):
        bar = _make_bar(price, 0.005)
        bars.append(bar)
    return bars, open_price, entry_price


def _make_all_tiers_then_rise_test():
    """All 6 tiers trigger, then price keeps rising (25% trailing stop exits)."""
    prev_close = 20.0
    open_price = 16.0  # Big gap
    entry_price = 15.0
    targets, _, trail_pcts, _ = calc_targets(entry_price, open_price)

    bars = []
    price = entry_price
    # Rise through all tiers
    target_high = targets[5] * 1.15
    n_rise = 100
    step = (target_high - entry_price) / n_rise
    for i in range(n_rise):
        price += step * (1 + random.uniform(-0.2, 0.2))
        bar = {"open": round(price - 0.05, 4), "high": round(price + 0.1, 4),
               "low": round(price - 0.1, 4), "close": round(price, 4),
               "volume": 1000}
        bars.append(bar)
    # Then slight rise continues (trailing stop at 5% won't trigger for a while)
    for i in range(290):
        price *= 1.001
        bar = _make_bar(price, 0.01)
        bars.append(bar)
    return bars, open_price, entry_price


def _make_stop_then_recovery_test():
    """Price hits stop loss, then recovers — stop is irreversible."""
    prev_close = 20.0
    open_price = 18.0
    entry_price = 17.0
    stop = calc_stop_price(entry_price, entry_price * 0.03)

    bars = []
    price = entry_price
    # Drop to stop
    for i in range(10):
        price = entry_price - (entry_price - stop) * (i + 1) / 10
        bar = {"open": round(price + 0.01, 4), "high": round(price + 0.05, 4),
               "low": round(price - 0.02, 4), "close": round(price, 4),
               "volume": 1000}
        if i == 9:
            bar["low"] = stop * 0.98  # Hit stop
        bars.append(bar)
    # Recovery (irrelevant — stop already triggered)
    for i in range(380):
        price = entry_price * 1.05
        bar = _make_bar(price, 0.01)
        bars.append(bar)
    return bars, open_price, entry_price


def _make_time_limit_breakeven_test():
    """Price stays at entry for 40 min, then slight rise."""
    prev_close = 20.0
    open_price = 18.0
    entry_price = 17.0

    bars = []
    price = entry_price
    # Flat at entry for 40 bars (8 5-min = 40 min)
    for i in range(200):
        price = entry_price * (1 + random.uniform(-0.001, 0.001))
        bar = _make_bar(price, 0.001)
        bars.append(bar)
    # Then rise slightly above entry
    for i in range(190):
        price = entry_price * (1 + 0.001 * (i + 1))
        bar = _make_bar(price, 0.005)
        bars.append(bar)
    return bars, open_price, entry_price


def _make_instant_t1_test():
    """Entry price is already very close to T1."""
    prev_close = 20.0
    open_price = 19.5  # Small gap
    entry_price = 19.3
    targets, _, _, _ = calc_targets(entry_price, open_price)

    bars = []
    price = entry_price
    # Price quickly reaches T1 (within a few bars)
    for i in range(3):
        price = targets[0] * (1 + random.uniform(0, 0.01))
        bar = {"open": round(price - 0.01, 4), "high": round(price + 0.05, 4),
               "low": round(price - 0.03, 4), "close": round(price, 4),
               "volume": 1000}
        bars.append(bar)
    # Then slow rise
    for i in range(387):
        price *= 1.0005
        bar = _make_bar(price, 0.005)
        bars.append(bar)
    return bars, open_price, entry_price


# ── Main ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5000

    print("=" * 80)
    print(" Stone 1.0 Monte Carlo Simulation")
    print(f" Testing: backtest engine + ladder sell system + edge cases")
    print("=" * 80)

    # Run edge case tests first
    run_edge_case_tests()

    # Run Monte Carlo
    run_monte_carlo(n_simulations=n)
