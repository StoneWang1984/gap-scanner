"""卖出拒绝回测 — 模拟"买入后无法卖出"的实盘场景

现有回测假设完美执行，但实盘Alpaca经常拒绝卖单。本脚本在卖出环节
注入概率性拒绝，逐5-min bar模拟完整交易生命周期，对比完美执行vs
拒绝执行的PnL差异，量化裸仓风险和不变量检查器修复效果。

用法:
  python3 backtest_rejection.py --days 5 --scenario G    # 快速测试
  python3 backtest_rejection.py --days 30 --all           # 全7场景
  python3 backtest_rejection.py --days 30 --stop-distance 0.10
"""

import argparse
import math
import random
import sys
import os
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

# ── 项目导入 ──────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from strategy import (
    TradePlan, TradeResult, build_trade_plan, calc_atr, calc_stop_price,
    calc_price_at_retracement, calc_position_size, evaluate_trade_stone,
    evaluate_reentry_trade, find_reentry_point,
)
from backtest import (
    get_data_client, get_trading_days, bulk_scan_gaps,
    get_5min_bars, get_1min_bars, find_entry_with_confirmation_1min,
    is_leveraged_etf, _bars_to_list, run_backtest,
)

# ── 拒绝配置 ──────────────────────────────────────────────
@dataclass
class RejectionConfig:
    """各类别拒绝概率 + 模拟参数"""
    stop_distance: float = 0.05    # 止损距离太近（最常见）
    rate_limit: float = 0.02       # API限频
    not_tradable: float = 0.01     # 股票暂停交易
    network: float = 0.01          # 网络超时
    price_invalid: float = 0.01    # 价格无效
    qty_small: float = 0.005       # 数量太小
    cascading_fix_failure: float = 0.10  # INV-2修复也被拒的概率增幅
    inv_check_interval: int = 4    # 不变量检查间隔（bar数）
    stop_limit_max_retries: int = 3
    trailing_max_retries: int = 2
    market_sell_max_retries: int = 3
    seed: int = 42                 # 随机种子（可复现）

    # 滑点模型（与实盘config一致）
    slippage_stop_pct: float = 0.02
    slippage_trailing_pct: float = 0.01
    slippage_target_pct: float = 0.003
    slippage_force_close_pct: float = 0.01
    slippage_entry_pct: float = 0.005

# ── 拒绝事件日志 ──────────────────────────────────────────
@dataclass
class RejectionEvent:
    timestamp: str          # bar时间
    symbol: str
    order_type: str         # protective_stop/trailing_stop/market_sell/limit_sell/force_close
    category: str           # stop_distance/rate_limit/not_tradable/network/price_invalid
    attempt: int            # 第几次尝试
    retry_result: str       # "success"/"failed"/"skipped"
    stop_price_before: float = 0.0
    stop_price_after: float = 0.0
    detail: str = ""

# ── 模拟仓位 ──────────────────────────────────────────────
@dataclass
class SimPosition:
    """镜像LivePosition + 模拟特有字段"""
    symbol: str
    entry_price: float
    shares: int
    stop_price: float          # 原始止损价（calc_stop_price）
    open_price: float
    trade_type: str = "first"  # "first"/"reentry"
    remaining_shares: int = 0
    highest: float = 0.0
    atr: float = 0.0

    # 保护性订单状态
    protective_order_type: str = "none"  # "stop_limit"/"trailing_stop"/"none"
    protective_order_stop_price: float = 0.0  # 当前止损价
    protective_trail_pct: float = 0.0   # 当前trailing百分比

    # 6档目标
    targets: list = field(default_factory=list)
    sell_ratios: list = field(default_factory=list)
    trail_pcts: list = field(default_factory=list)
    reached_list: list = None
    sold_shares_list: list = None
    next_tier_idx: int = 0

    # Re-entry字段
    reentry_target: float = 0.0
    reached_target1: bool = False
    sold_partial1_shares: int = 0
    breakeven_active: bool = False
    prev_high: float = 0.0

    # 时间限制
    bar_count: int = 0
    time_limit_active: bool = False

    # 模拟特有字段
    naked_since_bar: int = -1     # 裸仓开始的bar索引
    naked_bars_total: int = 0     # 累计裸仓bar数
    rejection_events: list = field(default_factory=list)

    def __post_init__(self):
        self.remaining_shares = self.shares
        self.highest = self.entry_price
        if self.reached_list is None:
            self.reached_list = [False] * len(self.targets)
        if self.sold_shares_list is None:
            self.sold_shares_list = [0] * len(self.targets)

# ── 模拟交易结果 ──────────────────────────────────────────
@dataclass
class SimTradeResult:
    """模拟交易结果（与TradeResult对齐 + 模拟特有字段）"""
    symbol: str
    date: str
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    pnl_pct: float
    exit_reason: str
    open_price: float = 0.0
    stop_price: float = 0.0
    partial_sells: list = None
    atr: float = 0.0
    trade_type: str = "first"
    # 模拟特有
    rejection_count: int = 0       # 本笔交易遭遇的拒绝次数
    naked_bars: int = 0            # 裸仓bar数
    inv2_detections: int = 0       # INV-2检测次数
    inv2_fix_success: int = 0      # INV-2修复成功次数
    inv2_fix_failed: int = 0       # INV-2修复失败次数（级联）
    max_unprotected_drawdown_pct: float = 0.0  # 裸仓期间最大回撤%
    exit_bar_idx: int = -1         # 退出bar索引（用于re-entry检测）

# ── 核心模拟引擎 ──────────────────────────────────────────
class RejectionSimulator:
    def __init__(self, cfg: RejectionConfig):
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        self.order_counter = 0
        self.events: list[RejectionEvent] = []
        self.current_bar_idx = 0
        self.orders_this_bar = 0  # 同一bar下单数（影响rate_limit概率）

    def _should_reject(self, order_type: str, context: str = "normal") -> tuple:
        """概率决策：是否拒绝本次下单。返回 (rejected, category, detail)"""
        # 基础概率表（按订单类型上下文感知）
        base_probs = {
            "protective_stop": {
                "stop_distance": self.cfg.stop_distance * 1.5,  # 止损单最容易被拒
                "rate_limit": self.cfg.rate_limit,
                "not_tradable": self.cfg.not_tradable,
                "network": self.cfg.network,
            },
            "trailing_stop": {
                "stop_distance": self.cfg.stop_distance,
                "rate_limit": self.cfg.rate_limit,
                "network": self.cfg.network,
            },
            "market_sell": {
                "rate_limit": self.cfg.rate_limit * 1.2,  # 同bar多单触发限频
                "not_tradable": self.cfg.not_tradable * 0.5,
                "network": self.cfg.network,
                "qty_small": self.cfg.qty_small,
            },
            "limit_sell": {
                "price_invalid": self.cfg.price_invalid,
                "rate_limit": self.cfg.rate_limit,
                "network": self.cfg.network,
            },
            "force_close": {
                "rate_limit": self.cfg.rate_limit * 0.5,  # EOD平仓优先级高
                "not_tradable": self.cfg.not_tradable * 0.3,
                "network": self.cfg.network,
            },
        }

        probs = base_probs.get(order_type, {})

        # 上下文调整：同一bar多单时rate_limit概率增加
        if self.orders_this_bar > 2:
            for cat in probs:
                if cat == "rate_limit":
                    probs[cat] = min(probs[cat] * 2, 0.5)

        # 上下文调整：不变量修复时概率增加（级联失败）
        if context == "invariant_fix":
            for cat in probs:
                probs[cat] *= (1 + self.cfg.cascading_fix_failure)

        for category, prob in probs.items():
            if self.rng.random() < prob:
                detail = f"Simulated {category} rejection for {order_type}"
                return True, category, detail

        return False, None, None

    def _log_rejection(self, pos, order_type, category, attempt, stop_before=0, stop_after=0, detail=""):
        """记录拒绝事件"""
        ev = RejectionEvent(
            timestamp=f"bar_{self.current_bar_idx}",
            symbol=pos.symbol,
            order_type=order_type,
            category=category,
            attempt=attempt,
            retry_result="pending",
            stop_price_before=stop_before,
            stop_price_after=stop_after,
            detail=detail,
        )
        pos.rejection_events.append(ev)
        self.events.append(ev)

    # ── 模拟订单函数 ──────────────────────────────────────────────

    def sim_place_protective_stop(self, pos, context="normal") -> bool:
        """模拟protective stop下单。失败时仓位裸仓。"""
        stop_price = pos.stop_price
        # 限价 = 止损价 × (1 - 3%缓冲)
        limit_price = round(stop_price * (1 - self.cfg.slippage_stop_pct), 2)

        for attempt in range(self.cfg.stop_limit_max_retries):
            rejected, category, detail = self._should_reject("protective_stop", context)
            self.orders_this_bar += 1
            if not rejected:
                pos.protective_order_type = "stop_limit"
                pos.protective_order_stop_price = stop_price
                if pos.naked_since_bar >= 0:
                    # 从裸仓恢复
                    naked_bars = self.current_bar_idx - pos.naked_since_bar
                    pos.naked_bars_total += naked_bars
                    pos.naked_since_bar = -1
                return True

            self._log_rejection(pos, "protective_stop", category, attempt + 1,
                                stop_before=stop_price, stop_after=stop_price, detail=detail)

            if category == "stop_distance" and attempt < self.cfg.stop_limit_max_retries - 1:
                # 增大缓冲：stop下移3%/5%/7%（匹配实盘修复）
                buffer_mult = 0.03 * (attempt + 1)
                old_stop = stop_price
                stop_price = round(stop_price * (1 - buffer_mult), 2)
                limit_price = round(stop_price * (1 - self.cfg.slippage_stop_pct), 2)
                # 更新pos.stop_price以便后续止损检查使用更宽的止损
                if stop_price < pos.stop_price:
                    pos.stop_price = stop_price
                continue

            if category in ("rate_limit", "network") and attempt < self.cfg.stop_limit_max_retries - 1:
                continue  # 模拟5秒等待后重试

            # 所有重试耗尽 → 裸仓
            pos.protective_order_type = "none"
            pos.protective_order_stop_price = 0
            if pos.naked_since_bar < 0:
                pos.naked_since_bar = self.current_bar_idx
            return False

        return False

    def sim_place_trailing_stop(self, pos, trail_pct, context="normal") -> bool:
        """模拟trailing stop下单。失败时回退到stop-limit。"""
        for attempt in range(self.cfg.trailing_max_retries):
            rejected, category, detail = self._should_reject("trailing_stop", context)
            self.orders_this_bar += 1
            if not rejected:
                pos.protective_order_type = "trailing_stop"
                pos.protective_trail_pct = trail_pct
                # trailing stop的阈值 = highest × (1 - trail_pct)
                pos.protective_order_stop_price = round(pos.highest * (1 - trail_pct), 2)
                if pos.naked_since_bar >= 0:
                    naked_bars = self.current_bar_idx - pos.naked_since_bar
                    pos.naked_bars_total += naked_bars
                    pos.naked_since_bar = -1
                return True

            self._log_rejection(pos, "trailing_stop", category, attempt + 1, detail=detail)

            if category in ("rate_limit", "network") and attempt < self.cfg.trailing_max_retries - 1:
                continue

            # trailing stop失败 → 回退到stop-limit（匹配实盘）
            if attempt >= self.cfg.trailing_max_retries - 1:
                fallback_stop = round(pos.entry_price * 0.98, 2)  # 2%保护
                pos.stop_price = max(pos.stop_price, fallback_stop)
                result = self.sim_place_protective_stop(pos, context=context)
                if result:
                    self._log_rejection(pos, "trailing_stop_fallback", "fallback_success", attempt + 1,
                                        detail="Trailing failed, stop-limit fallback succeeded")
                else:
                    if pos.naked_since_bar < 0:
                        pos.naked_since_bar = self.current_bar_idx
                return result

        return False

    def sim_place_sell_market(self, pos, shares, context="normal") -> int:
        """模拟市价卖出。返回实际卖出股数（0=全部被拒）。"""
        for attempt in range(self.cfg.market_sell_max_retries):
            rejected, category, detail = self._should_reject("market_sell", context)
            self.orders_this_bar += 1
            if not rejected:
                return shares

            self._log_rejection(pos, "market_sell", category, attempt + 1, detail=detail)

            if category in ("rate_limit", "network") and attempt < self.cfg.market_sell_max_retries - 1:
                continue

            if category == "qty_small" and attempt < self.cfg.market_sell_max_retries - 1:
                shares = max(1, shares)
                continue

            return 0  # 所有重试耗尽

        return 0

    def sim_place_sell_limit(self, pos, shares, price, context="normal") -> bool:
        """模拟限价卖（re-entry tier-1）。返回是否成功下单。"""
        for attempt in range(2):
            rejected, category, detail = self._should_reject("limit_sell", context)
            self.orders_this_bar += 1
            if not rejected:
                return True

            self._log_rejection(pos, "limit_sell", category, attempt + 1, detail=detail)

            if category == "price_invalid" and attempt < 1:
                price = round(price * 0.99, 2)
                continue

            if category in ("rate_limit", "network") and attempt < 1:
                continue

            return False

        return False

    def sim_force_sell(self, pos, shares, context="normal") -> int:
        """模拟强制卖出（force_close用）。返回卖出股数。"""
        # 尝试1: 市价卖出
        sold = self.sim_place_sell_market(pos, shares, context=context)
        if sold >= shares:
            return sold
        if sold > 0:
            return sold
        # 尝试2: 降低价格再试（模拟实盘close_position fallback）
        sold2 = self.sim_place_sell_market(pos, shares, context=context)
        return sold2

    # ── 不变量检查器 ──────────────────────────────────────────────

    def sim_check_invariants(self, pos) -> tuple:
        """模拟INV-2/3/5检查。返回 (errors, fixes, critical)"""
        errors = []
        fixes = []

        # INV-2: 裸仓检测
        if pos.remaining_shares > 0 and pos.protective_order_type == "none":
            errors.append(f"INV-2 裸仓: {pos.symbol} {pos.remaining_shares}股无保护")
            fixes.append(("add_stop", pos.symbol))
            pos.inv2_detections = getattr(pos, 'inv2_detections', 0) + 1

        # INV-3: 超卖
        sold = pos.shares - pos.remaining_shares
        if sold > pos.shares or pos.remaining_shares < 0:
            errors.append(f"INV-3 超卖: {pos.symbol}")
            fixes.append(("reset_remaining", pos.symbol))

        # INV-5: tier不一致
        if pos.sold_shares_list and pos.trade_type != "reentry":
            expected_sold = sum(pos.sold_shares_list[:pos.next_tier_idx])
            actual_sold = pos.shares - pos.remaining_shares
            if expected_sold != actual_sold and pos.remaining_shares > 0:
                errors.append(f"INV-5 tier不一致: {pos.symbol}")

        critical = any(e.startswith("INV-2") for e in errors)
        return errors, fixes, critical

    def sim_apply_invariant_fix(self, pos, fix) -> bool:
        """模拟执行修复。返回是否成功。"""
        action = fix[0]
        if action == "add_stop":
            result = self.sim_place_protective_stop(pos, context="invariant_fix")
            if result:
                setattr(pos, 'inv2_fix_success', getattr(pos, 'inv2_fix_success', 0) + 1)
            else:
                setattr(pos, 'inv2_fix_failed', getattr(pos, 'inv2_fix_failed', 0) + 1)
            return result

        elif action == "reset_remaining":
            pos.remaining_shares = max(0, pos.shares)
            return True

        return False

    # ── 逐bar交易处理 ──────────────────────────────────────────────

    def simulate_first_trade(self, plan: TradePlan, bars_after_entry: list,
                             force_close_price=None) -> SimTradeResult:
        """逐bar模拟首笔交易完整生命周期（含拒绝注入）"""
        pos = SimPosition(
            symbol=plan.symbol,
            entry_price=plan.pullback,
            shares=plan.shares,
            stop_price=plan.stop_price,
            open_price=plan.open_price,
            trade_type="first",
            targets=plan.targets,
            sell_ratios=plan.sell_ratios,
            trail_pcts=plan.trail_pcts,
            atr=plan.atr,
        )

        time_limit_bars = getattr(config, "FIRST_TRADE_TIME_LIMIT_BARS", 8)
        inv2_det = inv2_fix_ok = inv2_fix_fail = 0

        # 入场后立即挂protective stop
        self.orders_this_bar = 0
        ps_ok = self.sim_place_protective_stop(pos, context="entry_fill")
        if not ps_ok:
            # 入场裸仓：protective stop被拒
            pass

        # 跟踪裸仓期间最大回撤
        max_drawdown_pct = 0.0
        entry_price = pos.entry_price

        # 逐bar处理
        for bi, bar in enumerate(bars_after_entry):
            self.current_bar_idx = bi
            self.orders_this_bar = 0  # 每bar重置
            bh, bl = bar["high"], bar["low"]
            cur_price = bar["close"]
            if bh > pos.highest:
                pos.highest = bh
            pos.bar_count = bi + 1

            # ── 裸仓期间跟踪回撤 ──
            if pos.protective_order_type == "none" and pos.remaining_shares > 0:
                dd_pct = (entry_price - bl) / entry_price
                if dd_pct > max_drawdown_pct:
                    max_drawdown_pct = dd_pct

            # ── 1. 保护性止损触达 ──
            if pos.protective_order_type == "stop_limit":
                if bl <= pos.protective_order_stop_price:
                    # 止损成交价 = stop_price × (1 - slippage)
                    exit_price = round(pos.protective_order_stop_price * (1 - self.cfg.slippage_stop_pct), 2)
                    return self._make_sim_result(pos, "stop_loss", exit_price, bi,
                                                  inv2_det, inv2_fix_ok, inv2_fix_fail, max_drawdown_pct, bar)

            # ── 2. Trailing stop触达 ──
            if pos.protective_order_type == "trailing_stop":
                trail_threshold = round(pos.highest * (1 - pos.protective_trail_pct), 2)
                trail_threshold = max(trail_threshold, pos.entry_price)
                if bl <= trail_threshold:
                    exit_price = round(trail_threshold * (1 - self.cfg.slippage_trailing_pct), 2)
                    suffix = f"_trail{int(pos.protective_trail_pct * 100)}"
                    return self._make_sim_result(pos, f"trailing_stop{suffix}", exit_price, bi,
                                                  inv2_det, inv2_fix_ok, inv2_fix_fail, max_drawdown_pct, bar)

            # ── 3. 原始止损价触达（裸仓时的fallback） ──
            if pos.protective_order_type == "none" and bl <= pos.stop_price:
                # 裸仓时价格跌穿止损 → 尝试force sell
                sold = self.sim_force_sell(pos, pos.remaining_shares, context="naked_stop")
                if sold >= pos.remaining_shares:
                    exit_price = round(pos.stop_price * (1 - self.cfg.slippage_stop_pct * 2), 2)
                    pos.remaining_shares = 0
                    return self._make_sim_result(pos, "naked_stop_loss", exit_price, bi,
                                                  inv2_det, inv2_fix_ok, inv2_fix_fail, max_drawdown_pct, bar)
                elif sold > 0:
                    pos.remaining_shares -= sold
                    self.sim_place_protective_stop(pos)  # 补挂止损
                else:
                    # 完全无法卖出 → 继续持有，等下一bar
                    pass

            # ── 4. 时间限制 ──
            if time_limit_bars > 0 and not pos.reached_list[0] and bi >= time_limit_bars:
                pos.time_limit_active = True
            if pos.time_limit_active and bh >= pos.entry_price:
                sold = self.sim_force_sell(pos, pos.remaining_shares, context="time_limit")
                if sold >= pos.remaining_shares:
                    exit_price = max(cur_price, pos.entry_price)
                    pos.remaining_shares = 0
                    return self._make_sim_result(pos, "time_limit_exit", exit_price, bi,
                                                  inv2_det, inv2_fix_ok, inv2_fix_fail, max_drawdown_pct, bar)
                elif sold > 0:
                    pos.remaining_shares -= sold
                    self.sim_place_protective_stop(pos)

            # ── 5. 阶梯卖出 T1-T6 ──
            # 取消protective → 市价卖出tier份额 → 挂新protective
            for ti in range(pos.next_tier_idx, len(pos.targets)):
                if not pos.reached_list[ti] and bh >= pos.targets[ti]:
                    # 标记所有更低tier为已触达（跳档处理）
                    for tj in range(ti + 1):
                        pos.reached_list[tj] = True

                    # 卖出本档
                    sell_n = max(1, int(pos.shares * pos.sell_ratios[ti]))
                    sell_n = min(sell_n, pos.remaining_shares)

                    # 取消protective stop
                    pos.protective_order_type = "none"
                    pos.protective_order_stop_price = 0

                    sold = self.sim_place_sell_market(pos, sell_n, context="tier_sell")
                    if sold >= sell_n:
                        pos.sold_shares_list[ti] = sold
                        pos.remaining_shares -= sold
                        pos.next_tier_idx = ti + 1
                    elif sold > 0:
                        # 部分卖出
                        pos.sold_shares_list[ti] = sold
                        pos.remaining_shares -= sold
                        pos.next_tier_idx = ti + 1
                    else:
                        # 卖出被拒 → 补挂止损
                        if pos.remaining_shares > 0:
                            self.sim_place_protective_stop(pos)

                    # 处理跳档中未卖出的低档
                    for tj in range(ti):
                        if not pos.sold_shares_list[tj] and pos.reached_list[tj]:
                            sell_n2 = max(1, int(pos.shares * pos.sell_ratios[tj]))
                            sell_n2 = min(sell_n2, pos.remaining_shares)
                            sold2 = self.sim_place_sell_market(pos, sell_n2, context="tier_sell_skip")
                            if sold2 > 0:
                                pos.sold_shares_list[tj] = sold2
                                pos.remaining_shares -= sold2
                            else:
                                break  # 无法继续卖出

                    # 挂新protective（trailing stop for next tier）
                    if pos.remaining_shares > 0:
                        highest_tier = 0
                        for ht in range(len(pos.reached_list) - 1, -1, -1):
                            if pos.reached_list[ht]:
                                highest_tier = ht
                                break
                        if highest_tier < len(pos.trail_pcts):
                            trail_pct = pos.trail_pcts[highest_tier]
                        else:
                            trail_pct = pos.trail_pcts[-1]
                        self.sim_place_trailing_stop(pos, trail_pct)

                    # 所有份额卖出 → 退出
                    if pos.remaining_shares <= 0:
                        return self._make_sim_result(pos, "all_tiers_sold", cur_price, bi,
                                                      inv2_det, inv2_fix_ok, inv2_fix_fail, max_drawdown_pct, bar)

            # ── 6. Trailing stop轮询检查（已触达tier后） ──
            if any(pos.reached_list) and pos.protective_order_type == "trailing_stop":
                pass  # 已在步骤2处理

            # ── 7. 不变量检查 ──
            if bi % self.cfg.inv_check_interval == 0:
                errors, fixes, critical = self.sim_check_invariants(pos)
                for fix in fixes:
                    ok = self.sim_apply_invariant_fix(pos, fix)
                    inv2_det = getattr(pos, 'inv2_detections', inv2_det)
                    inv2_fix_ok = getattr(pos, 'inv2_fix_success', inv2_fix_ok)
                    inv2_fix_fail = getattr(pos, 'inv2_fix_failed', inv2_fix_fail)

        # ── EOD强制平仓 ──
        if force_close_price is not None:
            sold = self.sim_force_sell(pos, pos.remaining_shares, context="force_close")
            if sold >= pos.remaining_shares:
                exit_price = round(force_close_price * (1 - self.cfg.slippage_force_close_pct), 2)
                pos.remaining_shares = 0
            elif sold > 0:
                pos.remaining_shares -= sold
                exit_price = round(force_close_price * (1 - self.cfg.slippage_force_close_pct), 2)
            else:
                exit_price = bars_after_entry[-1]["close"] if bars_after_entry else pos.entry_price
        else:
            exit_price = bars_after_entry[-1]["close"] if bars_after_entry else pos.entry_price

        inv2_det = getattr(pos, 'inv2_detections', inv2_det)
        inv2_fix_ok = getattr(pos, 'inv2_fix_success', inv2_fix_ok)
        inv2_fix_fail = getattr(pos, 'inv2_fix_failed', inv2_fix_fail)

        reason = "force_close" if not any(pos.reached_list) else "force_close_partial"
        return self._make_sim_result(pos, reason, exit_price,
                                      len(bars_after_entry) - 1,
                                      inv2_det, inv2_fix_ok, inv2_fix_fail, max_drawdown_pct,
                                      bars_after_entry[-1] if bars_after_entry else {})

    def _make_sim_result(self, pos, reason, exit_price, bi,
                         inv2_det, inv2_fix_ok, inv2_fix_fail,
                         max_dd_pct, bar) -> SimTradeResult:
        """构造SimTradeResult"""
        # 计算PnL（含部分卖出）
        pnl = 0.0
        partial_sells = []
        for i in range(len(pos.sold_shares_list)):
            if pos.sold_shares_list[i] > 0:
                sell_price = pos.targets[i] if i < len(pos.targets) else exit_price
                pnl += (sell_price - pos.entry_price) * pos.sold_shares_list[i]
                partial_sells.append((sell_price, pos.sold_shares_list[i]))

        # 剩余份额的PnL
        if pos.remaining_shares > 0:
            pnl += (exit_price - pos.entry_price) * pos.remaining_shares

        pnl_pct = pnl / (pos.entry_price * pos.shares) if pos.entry_price > 0 else 0
        total_rejections = len(pos.rejection_events)
        naked_bars = pos.naked_bars_total
        if pos.naked_since_bar >= 0 and bi >= pos.naked_since_bar:
            naked_bars += (bi - pos.naked_since_bar)

        date_str = ""
        if isinstance(bar, dict) and "timestamp" in bar:
            ts = bar["timestamp"]
            date_str = str(ts.date()) if hasattr(ts, 'date') else str(ts)[:10]

        return SimTradeResult(
            symbol=pos.symbol, date=date_str,
            entry_price=pos.entry_price, exit_price=round(exit_price, 4),
            shares=pos.shares, pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4),
            exit_reason=reason, open_price=pos.open_price,
            stop_price=pos.stop_price, partial_sells=partial_sells,
            atr=pos.atr, trade_type=pos.trade_type,
            rejection_count=total_rejections, naked_bars=naked_bars,
            inv2_detections=inv2_det, inv2_fix_success=inv2_fix_ok,
            inv2_fix_failed=inv2_fix_fail,
            max_unprotected_drawdown_pct=round(max_dd_pct, 4),
            exit_bar_idx=bi,
        )

    # ── Re-entry模拟 ──────────────────────────────────────────────

    def simulate_reentry_trade(self, symbol, entry_price, shares, stop_price,
                               open_price, reentry_target, trail_pct_2,
                               bars_after_entry, prev_high, atr,
                               force_close_price=None) -> SimTradeResult:
        """逐bar模拟re-entry交易（1档目标+trailing）"""
        pos = SimPosition(
            symbol=symbol, entry_price=entry_price, shares=shares,
            stop_price=stop_price, open_price=open_price,
            trade_type="reentry",
            atr=atr, prev_high=prev_high,
            reentry_target=reentry_target,
            targets=[], sell_ratios=[], trail_pcts=[],
            reached_list=[], sold_shares_list=[],
        )

        inv2_det = inv2_fix_ok = inv2_fix_fail = 0
        max_drawdown_pct = 0.0

        # 挂protective stop
        self.sim_place_protective_stop(pos, context="reentry_fill")

        for bi, bar in enumerate(bars_after_entry):
            self.current_bar_idx = bi
            self.orders_this_bar = 0
            bh, bl = bar["high"], bar["low"]
            cur_price = bar["close"]
            if bh > pos.highest:
                pos.highest = bh

            if pos.protective_order_type == "none" and pos.remaining_shares > 0:
                dd_pct = (pos.entry_price - bl) / pos.entry_price
                if dd_pct > max_drawdown_pct:
                    max_drawdown_pct = dd_pct

            # 止损触达
            if pos.protective_order_type == "stop_limit" and bl <= pos.protective_order_stop_price:
                exit_price = round(pos.protective_order_stop_price * (1 - self.cfg.slippage_stop_pct), 2)
                return self._make_sim_result(pos, "reentry_stop_loss", exit_price, bi,
                                              inv2_det, inv2_fix_ok, inv2_fix_fail, max_drawdown_pct, bar)

            # 裸仓fallback止损
            if pos.protective_order_type == "none" and bl <= pos.stop_price:
                sold = self.sim_force_sell(pos, pos.remaining_shares, context="reentry_naked_stop")
                if sold >= pos.remaining_shares:
                    exit_price = round(pos.stop_price * (1 - self.cfg.slippage_stop_pct * 2), 2)
                    pos.remaining_shares = 0
                    return self._make_sim_result(pos, "reentry_naked_stop", exit_price, bi,
                                                  inv2_det, inv2_fix_ok, inv2_fix_fail, max_drawdown_pct, bar)

            # Tier-1目标触达
            if not pos.reached_target1 and bh >= pos.reentry_target:
                pos.reached_target1 = True
                sell_ratio_1 = getattr(config, "REENTRY_SELL_RATIO_1", 0.5)
                n = int(pos.remaining_shares * sell_ratio_1)
                if n > 0:
                    # 取消protective stop
                    pos.protective_order_type = "none"
                    pos.protective_order_stop_price = 0

                    # 挂限价卖单（模拟实盘place_sell_limit）
                    sell_ok = self.sim_place_sell_limit(pos, n, pos.reentry_target, context="reentry_tier1")
                    if sell_ok:
                        # 限价卖成交（模拟fill）
                        pos.sold_partial1_shares = n
                        pos.remaining_shares -= n
                        pos.breakeven_active = True
                        # 挂trailing stop保护剩余份额
                        self.sim_place_trailing_stop(pos, trail_pct_2, context="reentry_after_tier1")
                    else:
                        # 限价卖被拒 → 补挂止损（实盘修复逻辑）
                        pos.reached_target1 = False
                        pos.sold_partial1_shares = 0
                        pos.breakeven_active = False
                        if pos.remaining_shares > 0:
                            self.sim_place_protective_stop(pos, context="reentry_tier1_failed")

            # Breakeven止损
            if pos.breakeven_active and cur_price <= pos.entry_price and pos.remaining_shares > 0:
                sold = self.sim_force_sell(pos, pos.remaining_shares, context="reentry_breakeven")
                if sold >= pos.remaining_shares:
                    pos.remaining_shares = 0
                    return self._make_sim_result(pos, "reentry_breakeven", pos.entry_price, bi,
                                                  inv2_det, inv2_fix_ok, inv2_fix_fail, max_drawdown_pct, bar)
                elif sold > 0:
                    pos.remaining_shares -= sold
                    self.sim_place_protective_stop(pos)

            # Trailing stop检查
            if pos.protective_order_type == "trailing_stop":
                trail_threshold = round(pos.highest * (1 - pos.protective_trail_pct), 2)
                trail_threshold = max(trail_threshold, pos.entry_price)
                if bl <= trail_threshold:
                    exit_price = round(trail_threshold * (1 - self.cfg.slippage_trailing_pct), 2)
                    return self._make_sim_result(pos, "reentry_trailing_stop", exit_price, bi,
                                                  inv2_det, inv2_fix_ok, inv2_fix_fail, max_drawdown_pct, bar)

            # 不变量检查
            if bi % self.cfg.inv_check_interval == 0:
                errors, fixes, critical = self.sim_check_invariants(pos)
                for fix in fixes:
                    self.sim_apply_invariant_fix(pos, fix)
                    inv2_det = getattr(pos, 'inv2_detections', inv2_det)
                    inv2_fix_ok = getattr(pos, 'inv2_fix_success', inv2_fix_ok)
                    inv2_fix_fail = getattr(pos, 'inv2_fix_failed', inv2_fix_fail)

        # EOD
        if force_close_price:
            sold = self.sim_force_sell(pos, pos.remaining_shares, context="force_close")
            exit_price = round(force_close_price * (1 - self.cfg.slippage_force_close_pct), 2)
        else:
            exit_price = bars_after_entry[-1]["close"] if bars_after_entry else pos.entry_price

        inv2_det = getattr(pos, 'inv2_detections', inv2_det)
        inv2_fix_ok = getattr(pos, 'inv2_fix_success', inv2_fix_ok)
        inv2_fix_fail = getattr(pos, 'inv2_fix_failed', inv2_fix_fail)

        return self._make_sim_result(pos, "reentry_force_close", exit_price,
                                      len(bars_after_entry) - 1,
                                      inv2_det, inv2_fix_ok, inv2_fix_fail, max_drawdown_pct,
                                      bars_after_entry[-1] if bars_after_entry else {})

# ── 数据加载 + 完美执行基线 ──────────────────────────────────────

def run_baseline(n_days=30) -> list[TradeResult]:
    """运行完美执行回测作为基线"""
    print("Running perfect-fill baseline backtest...")
    results = run_backtest(n_days=n_days)
    print(f"Baseline: {len(results)} trades")
    return results

def run_rejection_backtest(n_days=30, cfg: RejectionConfig = None) -> list[SimTradeResult]:
    """运行拒绝回测：获取同样的历史数据，逐bar模拟含拒绝的交易"""
    if cfg is None:
        cfg = RejectionConfig()

    sim = RejectionSimulator(cfg)
    client = get_data_client()

    end_date = pd.Timestamp.now(tz="America/New_York")
    trading_days = get_trading_days(client, end_date, n_days)
    if not trading_days:
        print("No trading days found.")
        return []

    print(f"\n[Rejection Backtest] {len(trading_days)} days, seed={cfg.seed}")
    print(f"  Rejection probs: stop_distance={cfg.stop_distance:.1%} rate_limit={cfg.rate_limit:.1%} "
          f"not_tradable={cfg.not_tradable:.1%} network={cfg.network:.1%} cascading={cfg.cascading_fix_failure:.1%}")

    # 获取可交易股票
    from backtest import get_tradable_symbols
    symbols = get_tradable_symbols()
    symbols = [s for s in symbols if not is_leveraged_etf(s)]

    gap_data = bulk_scan_gaps(client, trading_days, symbols)

    all_results: list[SimTradeResult] = []
    equity = config.INITIAL_CAPITAL

    for date in trading_days:
        date_key = date.date()
        if date_key not in gap_data or gap_data[date_key].empty:
            continue

        n_cands = len(gap_data[date_key])
        max_stocks = min(config.MAX_POSITIONS_PER_DAY, n_cands)
        pos_per_stock = equity / max_stocks if max_stocks > 0 else equity
        pos_per_stock = min(pos_per_stock, config.MAX_POSITION_SIZE)
        candidates = gap_data[date_key].head(max_stocks)

        daily_trades = 0
        daily_stopped = False
        daily_loss = 0.0
        max_daily_loss = equity * getattr(config, "MAX_DAILY_LOSS_PCT", 0.05)

        first_trade_exits = {}  # {symbol: (exit_price, exit_bar_idx, highest, atr)} for re-entry

        for _, row in candidates.iterrows():
            if daily_trades >= config.MAX_DAILY_TRADES or daily_stopped:
                break
            if max_daily_loss > 0 and daily_loss <= -max_daily_loss:
                daily_stopped = True
                break

            symbol = row["symbol"]
            open_price = row["open_price"]

            bars_5m = get_5min_bars(client, symbol, date)
            if bars_5m.empty or len(bars_5m) < 3:
                continue

            bars_1m = get_1min_bars(client, symbol, date)
            entry_price, pullback_idx, confirmed = find_entry_with_confirmation_1min(
                bars_1m, open_price
            ) if not bars_1m.empty else (0, -1, False)

            if not confirmed or entry_price <= 0:
                continue

            if getattr(config, "ENTRY_BELOW_OPEN", True) and entry_price >= open_price:
                continue

            atr = calc_atr(_bars_to_list(bars_5m), 14)
            plan = build_trade_plan(symbol, open_price, entry_price, atr, pos_per_stock)
            if plan.shares <= 0:
                continue

            entry_bar_idx = locate_5min_bar_index(_bars_to_list(bars_5m),
                                                   bars_5m.index[0].to_pydatetime().replace(tzinfo=None)
                                                   if hasattr(bars_5m.index[0], 'to_pydatetime') else None)

            # 取入场后的5min bars
            bars_list = _bars_to_list(bars_5m)
            # 找到入场bar
            entry_bi = 0
            for i, b in enumerate(bars_list):
                if b["low"] <= entry_price and b["high"] >= entry_price:
                    entry_bi = i
                    break

            remaining_bars = bars_list[entry_bi + 1:]
            if not remaining_bars:
                continue

            # EOD强制平仓价
            force_close_price = remaining_bars[-1]["close"]

            # ── 首笔交易模拟 ──
            result = sim.simulate_first_trade(plan, remaining_bars, force_close_price)
            all_results.append(result)
            daily_trades += 1
            equity += result.pnl
            daily_loss += result.pnl

            # ── Re-entry检测 ──
            if (result.exit_reason not in ("stop_loss", "naked_stop_loss", "reentry_stop_loss")
                and result.pnl > 0):
                reentry_cutoff_bars = len(remaining_bars) - 12  # 约12:30 EST (78 bars from 9:31)
                reentry_bars = remaining_bars[result.exit_bar_idx + 1:] if result.exit_bar_idx >= 0 else []

                if len(reentry_bars) >= 3:
                    reentry_ep, reentry_ph, _, reentry_conf = find_reentry_point(reentry_bars, open_price,
                                                                                 result.exit_price)
                    if reentry_conf and reentry_ep > 0:
                        min_pb = getattr(config, "REENTRY_MIN_PULLBACK", 0.04)
                        if min_pb > 0 and reentry_ph > 0:
                            pb_pct = (reentry_ph - reentry_ep) / reentry_ph
                            if pb_pct < min_pb:
                                continue

                        reentry_pos_ratio = getattr(config, "REENTRY_POSITION_RATIO", 0.5)
                        reentry_size = pos_per_stock * reentry_pos_ratio
                        reentry_shares = int(reentry_size / reentry_ep)
                        if reentry_shares <= 0:
                            continue

                        if atr > 0:
                            reentry_stop = round(reentry_ep - 1.5 * atr, 2)
                            reentry_stop = max(reentry_stop, round(reentry_ep * 0.96, 2))
                        else:
                            reentry_stop = round(reentry_ep * (1 - config.REENTRY_STOP_PCT), 2)

                        # 封顶10%
                        stop_max_pct = getattr(config, "STOP_LOSS_MAX_PCT", 0.10)
                        if stop_max_pct > 0:
                            min_stop = round(reentry_ep * (1 - stop_max_pct), 2)
                            reentry_stop = max(reentry_stop, min_stop)

                        retrace_1 = getattr(config, "REENTRY_PROFIT_RETRACEMENT_1", 0.75)
                        reentry_target = round(reentry_ep + retrace_1 * (reentry_ph - reentry_ep), 2)
                        trail_pct_2 = getattr(config, "REENTRY_TRAILING_PCT_2", 0.03)

                        reentry_result = sim.simulate_reentry_trade(
                            symbol, reentry_ep, reentry_shares, reentry_stop,
                            open_price, reentry_target, trail_pct_2,
                            reentry_bars, reentry_ph, atr,
                            reentry_bars[-1]["close"] if reentry_bars else None,
                        )
                        all_results.append(reentry_result)
                        daily_trades += 1
                        equity += reentry_result.pnl
                        daily_loss += reentry_result.pnl

    return all_results

# ── 场景配置 ──────────────────────────────────────────────

SCENARIOS = {
    "A": ("入场裸仓", RejectionConfig(stop_distance=0.20, rate_limit=0.01, not_tradable=0.01, network=0.01, cascading_fix_failure=0.05)),
    "B": ("Trailing级联", RejectionConfig(stop_distance=0.15, rate_limit=0.02, network=0.02, cascading_fix_failure=0.30)),
    "C": ("止损空窗期", RejectionConfig(stop_distance=0.10, rate_limit=0.01, not_tradable=0.01, network=0.01, cascading_fix_failure=0.05)),
    "D": ("阶梯卖出被拒", RejectionConfig(stop_distance=0.01, rate_limit=0.05, not_tradable=0.01, network=0.02, cascading_fix_failure=0.05)),
    "E": ("Re-entry取消", RejectionConfig(stop_distance=0.03, rate_limit=0.02, not_tradable=0.05, network=0.01, cascading_fix_failure=0.05)),
    "F": ("INV-2级联失败", RejectionConfig(stop_distance=0.20, rate_limit=0.02, network=0.02, cascading_fix_failure=0.40)),
    "G": ("综合压力", RejectionConfig()),  # 默认配置
}

# ── 对比报告 ──────────────────────────────────────────────

def print_comparison(baseline: list[TradeResult], sim_results: list[SimTradeResult],
                     scenario_name: str):
    """打印完美执行vs拒绝模拟对比表"""
    def stats(trades, is_sim=False):
        if not trades:
            return {"n": 0, "pnl": 0, "avg": 0, "win": 0, "win_rate": 0,
                    "max_dd": 0, "naked_bars": 0, "rejections": 0,
                    "inv2_det": 0, "inv2_fix_ok": 0, "inv2_fix_fail": 0}
        n = len(trades)
        pnl = sum(t.pnl for t in trades)
        avg = pnl / n
        wins = [t for t in trades if t.pnl > 0]
        win_rate = len(wins) / n if n > 0 else 0

        # 最大单笔亏损
        max_loss = min(t.pnl for t in trades) if trades else 0

        extra = {}
        if is_sim:
            extra["naked_bars"] = sum(t.naked_bars for t in trades)
            extra["rejections"] = sum(t.rejection_count for t in trades)
            extra["inv2_det"] = sum(t.inv2_detections for t in trades)
            extra["inv2_fix_ok"] = sum(t.inv2_fix_success for t in trades)
            extra["inv2_fix_fail"] = sum(t.inv2_fix_failed for t in trades)
            extra["max_unprotected_dd"] = max(t.max_unprotected_drawdown_pct for t in trades) if trades else 0
            # 拒绝原因统计
            stop_dist = sum(1 for t in trades for e in (getattr(t, '_rejection_events', []))
                           if "stop_distance" in str(e))
            rate_lim = sum(t.rejection_count for t in trades)  # 简化
            extra["stop_dist_rejects"] = 0
            extra["rate_limit_rejects"] = 0
            extra["network_rejects"] = 0

        return {"n": n, "pnl": round(pnl, 2), "avg": round(avg, 2),
                "win": len(wins), "win_rate": round(win_rate, 4),
                "max_dd": round(max_loss, 2), **extra}

    b = stats(baseline)
    s = stats(sim_results, is_sim=True)

    print(f"\n{'='*70}")
    print(f"  场景 {scenario_name}: 完美执行 vs 拒绝模拟对比")
    print(f"{'='*70}")
    print(f"  指标              │ 完美执行   │ 拒绝模拟   │ 差值       │ 变化率")
    print(f"  {'─'*16}│{'─'*12}│{'─'*12}│{'─'*12}│{'─'*10}")
    print(f"  交易笔数          │ {b['n']:>10} │ {s['n']:>10} │ {s['n']-b['n']:>10} │")
    print(f"  总PnL             │ ${b['pnl']:>9} │ ${s['pnl']:>9} │ ${s['pnl']-b['pnl']:>9} │ {(s['pnl']-b['pnl'])/abs(b['pnl'])*100 if b['pnl'] else 0:.1f}%")
    print(f"  平均PnL/笔        │ ${b['avg']:>9} │ ${s['avg']:>9} │ ${s['avg']-b['avg']:>9} │ {(s['avg']-b['avg'])/abs(b['avg'])*100 if b['avg'] else 0:.1f}%")
    print(f"  胜率              │ {b['win_rate']*100:>9.1f}% │ {s['win_rate']*100:>9.1f}% │ {(s['win_rate']-b['win_rate'])*100:>9.1f}% │")
    print(f"  最大单笔亏损      │ ${b['max_dd']:>9} │ ${s['max_dd']:>9} │ ${s['max_dd']-b['max_dd']:>9} │")
    print(f"  {'─'*16}│{'─'*12}│{'─'*12}│{'─'*12}│{'─'*10}")
    print(f"  累计裸仓bar数     │ {0:>10} │ {s.get('naked_bars',0):>10} │ {s.get('naked_bars',0):>10} │ N/A")
    print(f"  累计拒绝次数      │ {0:>10} │ {s.get('rejections',0):>10} │ {s.get('rejections',0):>10} │ N/A")
    print(f"  INV-2检测次数     │ {0:>10} │ {s.get('inv2_det',0):>10} │ {s.get('inv2_det',0):>10} │ N/A")
    print(f"  INV-2修复成功     │ {0:>10} │ {s.get('inv2_fix_ok',0):>10} │ {s.get('inv2_fix_ok',0):>10} │ N/A")
    print(f"  INV-2级联失败     │ {0:>10} │ {s.get('inv2_fix_fail',0):>10} │ {s.get('inv2_fix_fail',0):>10} │ N/A")
    if s.get('max_unprotected_dd', 0) > 0:
        print(f"  裸仓最大回撤%     │ {0:>10} │ {s['max_unprotected_dd']*100:>9.1f}% │ N/A       │ N/A")
    print(f"{'='*70}")

    # 退出原因分布
    if sim_results:
        reasons = {}
        for t in sim_results:
            r = t.exit_reason
            reasons[r] = reasons.get(r, 0) + 1
        print(f"\n  拒绝模拟退出原因分布:")
        for r, cnt in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"    {r}: {cnt}笔 ({cnt/len(sim_results)*100:.1f}%)")

    # 拒绝类别分布
    all_events = []
    for t in sim_results:
        # 重建rejection_events（从SimPosition获取不方便，用统计数据）
        pass

    return {"baseline": b, "sim": s, "pnl_delta": s['pnl'] - b['pnl']}

# ── 辅助函数 ──────────────────────────────────────────────

def locate_5min_bar_index(bars_list, entry_timestamp):
    """简化版：找到entry对应的5min bar index"""
    if not bars_list or entry_timestamp is None:
        return 0
    for i, b in enumerate(bars_list):
        ts = b.get("timestamp")
        if ts is not None:
            # 简化比较
            try:
                if str(ts)[:16] == str(entry_timestamp)[:16]:
                    return i
            except:
                pass
    return 0

# ── 主入口 ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="卖出拒绝回测")
    parser.add_argument("--days", type=int, default=30, help="回测天数")
    parser.add_argument("--scenario", type=str, default="G",
                        help="场景字母(A-G)或'all'")
    parser.add_argument("--stop-distance", type=float, default=None)
    parser.add_argument("--rate-limit", type=float, default=None)
    parser.add_argument("--cascading", type=float, default=None)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    # 运行基线
    print("=" * 70)
    print("  STEP 1: Perfect-fill baseline")
    print("=" * 70)
    baseline = run_baseline(n_days=args.days)

    # 运行拒绝场景
    scenarios_to_run = []
    if args.scenario == "all":
        scenarios_to_run = list(SCENARIOS.keys())
    else:
        scenarios_to_run = [args.scenario.upper()]

    all_comparisons = {}
    for sc in scenarios_to_run:
        if sc not in SCENARIOS:
            print(f"Unknown scenario: {sc}")
            continue

        sc_name, sc_cfg = SCENARIOS[sc]
        # 覆盖CLI参数
        if args.stop_distance is not None:
            sc_cfg.stop_distance = args.stop_distance
        if args.rate_limit is not None:
            sc_cfg.rate_limit = args.rate_limit
        if args.cascading is not None:
            sc_cfg.cascading_fix_failure = args.cascading
        if args.seed is not None:
            sc_cfg.seed = args.seed

        print(f"\n{'='*70}")
        print(f"  STEP 2: Scenario {sc} — {sc_name}")
        print(f"{'='*70}")

        sim_results = run_rejection_backtest(n_days=args.days, cfg=sc_cfg)
        comp = print_comparison(baseline, sim_results, f"{sc}: {sc_name}")
        all_comparisons[sc] = comp

    # 总结所有场景
    if len(scenarios_to_run) > 1:
        print(f"\n{'='*70}")
        print(f"  全场景总结 (PnL差异 vs 完美执行)")
        print(f"{'='*70}")
        for sc, comp in all_comparisons.items():
            sc_name = SCENARIOS[sc][0]
            delta = comp.get("pnl_delta", 0)
            b_pnl = comp["baseline"]["pnl"]
            pct = delta / abs(b_pnl) * 100 if b_pnl else 0
            print(f"  {sc}: {sc_name} — PnL差异: ${delta:.2f} ({pct:.1f}%)")

    # 拒绝事件日志样本
    print(f"\n  拒绝事件日志 (最近10条):")
    # 从所有SimPosition的rejection_events中获取
    # 这里需要从run_rejection_backtest内部获取sim对象
    # 简化：显示统计摘要

if __name__ == "__main__":
    main()
