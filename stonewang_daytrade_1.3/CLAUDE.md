# Gap Scanner — stonewang_daytrade_1.3 量化交易系统

## 项目概述

美股日内gap交易系统，基于Alpaca API实盘运行。检测开盘跳空低开的股票，等待1分钟K线折返点确认后市价入场，阶梯挂单逐档卖出获利。

## 1.3 变更清单（相对于 1.2）

| 编号 | 严重度 | 变更内容 |
|------|--------|----------|
| 1.3-1 | 架构 | entry_checked不再永久排除未确认候选股: buying power不足或仓位太小→continue(不add entry_checked), 释放资金可重新分配 |
| 1.3-2 | 配置 | REENTRY_POSITION_RATIO: 0.5→1.0, re-entry使用full slot而非half slot, 最大化资金效率 |
| 1.3-3 | 架构 | 中午re-scan: 10:30/11:30重新扫描新gap候选股, 加入监控列表, 补充1分钟K线数据 |
| 1.3-4 | 配置 | RESCAN_TIMES=["10:30","11:30"] 可配置re-scan时间 |
| 1.3-5 | 架构 | 6档→8档止盈: 新增T1(1%上限/1%trailing)和T2(2.5%上限/1.5%trailing), 捕获早期小涨幅 |
| 1.3-6 | 架构 | 8×1/8=100%全部通过阶梯卖出(原6×1/8=75%+25%trailing) |
| 1.3-7 | 架构 | T8(最高档)成交后卖出最后1/8, 无trailing stop |
| 1.3-8 | 架构 | T3回撤从25%改为35% |
| 1.3-9 | 架构 | Re-entry使用8档阶梯系统(与首笔交易共用calc_targets) |

## 1.2 变更清单（相对于 1.1）

| 编号 | 严重度 | 变更内容 |
|------|--------|----------|
| 1.2-1 | 架构 | OCO预挂单阶梯卖出系统: T1成交后同时挂T2 OCO(上限=T2目标价, 下限=T1成交价×0.98) + trailing stop保护剩余。OCO limit成交→进下一档+挂新OCO+新trailing。OCO stop成交→不进下一档, trailing继续保护 |
| 1.2-2 | 架构 | 消除5秒轮询延迟: OCO订单在Alpaca端实时触发, 不再需要每5秒检查价格是否触达目标 |
| 1.2-3 | 配置 | OCO_ENABLED=True 启用OCO模式, False回退到v1.1 polling模式 |
| 1.2-4 | 配置 | OCO_STOP_BUFFER_PCT=0.02(参考值), 实际buffer=trail_pct递增(1%/1.5%/2%/2.5%/3%/3.5%/4%/5%) |
| 1.2-5 | 数据 | tier_fill_prices字段: 存储每档实际成交价, 用于OCO stop计算 |
| 1.2-6 | 数据 | oco_order_ids字段: 跟踪预挂OCO订单状态 |
| 1.2-7 | 函数 | place_oco_sell, check_oco_fill, place_oco_for_next_tier, cancel_all_oco_for_position |
| 1.2-8 | 兜底 | Polling fallback: OCO stop成交后不放新OCO, 价格恢复超过下档目标时polling触发 |
| 1.2-9 | 兜底 | OCO下单失败→回退v1.1 trailing-only模式 |
| 1.2-10 | 修复 | 所有仓位退出路径(protective stop, stop loss, trailing stop, pullback stop, time limit, EOD)均先cancel OCO |
| 1.2-11 | Recovery | 启动时扫描Alpaca open orders检测OCO订单, 匹配target_price到tier, 恢复oco_order_ids |
| 1.2-12 | INV-7 | 不变量检查: OCO锁股≤remaining_shares, 超锁→取消所有OCO+补挂trailing |
| 1.2-13 | P0 | force_sell_position intent参数: partial阶梯卖出NEVER使用close_position(修复7/27误清仓bug) |
| 1.2-14 | P1 | _wait_cancel_confirmed: 6个关键cancel→action节点等待Alpaca确认取消后再执行(防止竞态条件) |
| 1.2-15 | P2 | 批量订单状态缓存(_order_cache): 1次get_orders替代N次get_order_by_id, API调用从~15/cycle降至~3-5/cycle |
| 1.2-16 | P3 | 轮询间隔从5秒降至3秒(POLL_INTERVAL可配置), 依赖批量缓存优化降低API压力 |
| 1.2-17 | 配置 | 取消5股交易限制: MAX_DAILY_TRADES=0(无限制), MAX_POSITIONS_PER_DAY=0, MAX_CANDIDATES=20. 仓位分配基于buying power/剩余候选股数而非固定slot数 |
| 1.2-18 | 配置 | MAX_POSITION_SIZE=200(每仓上限$200), MIN_POSITION_SIZE=100(最低$100). 不使用margin杠杆(cash账户multiplier=1) |

## 1.1 修复清单（相对于 1.0）

| 编号 | 严重度 | 修复内容 |
|------|--------|----------|
| 1.1-P0 | 严重 | 阶梯卖出锁仓bug: protective stop锁住全部股份→市价卖部分股数被拒→Method 3卖出全部仓位而非档位股数。修复: 先取消protective stop再卖档位股数，然后立刻重建trailing stop保护剩余股份 |
| 1.1-P1 | 重要 | force_sell_position Method 3: 部分卖出时卖qty而非total_qty（安全兜底） |
| 1.1-P2 | 重要 | force_sell_position Method 3: 只取消本symbol的卖单而非全账户所有卖单（避免干扰其他仓位） |
| 1.1-P3 | 次要 | RED/GREEN/YELLOW/RESET ANSI颜色常量定义（7/27实盘3次NameError） |

## 核心架构

### 入场系统
- **1分钟K线检测折返点** (`check_entry_1min`) — 价格跌破open_price后，3根1分钟bar确认底部（low > bottom + close > bottom + 至少1根阳线）
- **市价单入场** (`place_buy_market`) — 确认后立即市价买入，确保成交
- 入场窗口：9:31-10:00 EST，最多5支股票/天

### 阶梯挂单系统 (Ladder Sell) — 1.2 OCO版

**订单架构（以8股为例）:**

| 时机 | OCO (1股) | Trailing stop | 总锁仓 | 剩余 |
|------|-----------|--------------|--------|------|
| T1成交后 | T2: limit=T2, stop=T1×0.98(2%) | 6股, 2% | 7股 | 7 ✓ |
| T2 limit成交 | T3: limit=T3, stop=T2×0.975(2.5%) | 取消重建→trail(5,2.5%)=6 | 6股 | 6 ✓ |
| T2 stop成交 | — | 不变(6股,2%) | 6股 | 6 ✓ |
| Trailing先成交 | cancel OCO | 全部卖出 | 0 | 0 ✓ |

**三种成交路径:**
- **路径A (OCO limit成交)**: 价格到达目标→1股在目标价卖出→取消旧trailing→放新OCO+新trailing
- **路径B (OCO stop成交)**: 价格回落到stop价→1股在stop价卖出→不放新OCO→trailing继续保护→价格恢复后polling fallback触发下一档
- **路径C (Trailing stop先成交)**: 剩余股全部卖出→cancel OCO→仓位结束

**关键特性:**
- T1仍然polling-based（protective stop覆盖100%仓位，无法为T1预挂OCO）
- 1-2秒裸仓窗口: 取消旧trailing→放新trailing→放OCO之间（与v1.1相同）
- Alpaca OCO锁仓只锁一次（OCO pair锁qty而非2×qty）
- Skip-gap处理: OCO limit成交后检查cur_price是否超过后续多档→中间档用市价卖出→放最后一档OCO+trailing

### 8档目标系统
- 档位: 10% / 20% / 25% / 50% / 75% / 100% / 125% / 150% 回撤位
- 上限: 1% / 2% / 3.5% / 5% / 8% / 10% / 13% / 18% 涨幅封顶
- 每档卖出: 1/8仓位 (8×1/8=100%全部通过阶梯卖出)
- Trailing stop: 1.0% / 1.5% / 2.0% / 2.5% / 3.0% / 3.5% / 4.0% / 5.0% (T8后无trailing)

### 仓位管理
- 持仓过程用5分钟K线（30天对比回测验证：5分钟优于1分钟）
- 初始止损: ATR×2，封顶10%最大亏损
- 时间限制: 40分钟(8根5分钟bar)无T1成交则breakeven退出
- EOD强制平仓: 15:50 EST

### Re-entry系统
- 首笔退出后可二次入场（半仓）
- 1档目标(75%回撤)，1% trailing stop
- 最小回调4%，无时间限制
- 止损退出后禁止re-entry（防止二次亏损）

## Stone 1.1 修复清单（继承自 Stone 1.0）

| 编号 | 严重度 | 修复内容 |
|------|--------|----------|
| P0-1 | 严重 | 配置参数同步: STOP_LOSS_MAX_PCT=0.10, TRAILING_STOP_PCTS对齐, MIN_POSITION_SIZE=250 |
| P0-2 | 严重 | 杠杆ETF后缀移除单字母L/U（不再误排除AAPL/GOOGL） |
| P0-3 | 严重 | force_sell_position无仓位时返回0而非qty（避免双重记录） |
| P0-4 | 严重 | protective fill后过滤已退出仓位（避免双重卖出竞态） |
| P0-5 | 严重 | 阶梯卖出保护性止损空窗期修复（cancel_existing_orders参数） |
| P0-6 | 严重 | re-entry tier-1 remaining_shares延迟到确认成交后才减少 |
| P0-7 | 严重 | re-entry limit sell取消时恢复remaining_shares |
| P0-8 | 严重 | 日亏损熔断器改为累计PnL(realized+unrealized) |
| P1-9~P2 | 重要/次要 | 见Stone 1.0完整清单 |

## 关键文件

| 文件 | 作用 |
|------|------|
| `live_trade.py` | 实盘交易脚本（主程序，1.2 OCO阶梯卖出） |
| `config.py` | 配置文件（OCO_ENABLED, OCO_STOP_BUFFER_PCT新增） |
| `backtest.py` | 回测引擎 |
| `strategy.py` | 策略评估函数 |
| `scanner.py` | 股票扫描和筛选 |
| `config_base.py` | 回测配置（OCO参数同步） |
| `stock_simulator.py` | 5场景模拟器 |
| `rejection_simulator.py` | 拒绝模拟器 |

## 配置要点

- `OCO_ENABLED = True` — v1.2 OCO模式, False回退v1.1 polling
- `OCO_STOP_BUFFER_PCT = 0.02` — OCO止损buffer参考值, 实际用trail_pct递增
- `DRY_RUN = False` — 实盘模式
- `FORCE_QTY = 0` — 动态仓位计算
- `MAX_DAILY_TRADES = 0` — 无交易数量限制（buying power自动分配仓位大小）
- `MAX_CANDIDATES = 20` — 监控最多20支候选股
- `STOP_LOSS_MAX_PCT = 0.10` — 最大亏损10%（与回测一致）
- `TRAILING_STOP_PCTS = [0.01, 0.015, 0.02, 0.025, 0.03, 0.035, 0.04, 0.05]` — 8档, 与回测一致
- `MAX_POSITION_SIZE = 200` — 每仓上限$200（cash账户，不使用margin杠杆）
- `MIN_POSITION_SIZE = 1` — 最低仓位$1
- `REENTRY_MIN_PULLBACK = 0.04` — 最低4%回调（与回测一致）
- `LEVERAGED_ETF_SUFFIXES = ("BULL", "BEAR")` — 不再误排除正常股票
- `DATA_FEED = DataFeed.SIP` — SIP数据源
- 轮询间隔: 5秒（避免API限频）

## 运行方式

```bash
# 实盘运行 (v1.2 OCO模式)
/Users/stonewang2014/gap-scanner/.venv/bin/python3 -u \
  /Users/stonewang2014/gap-scanner/stonewang_daytrade_1.2/live_trade.py

# v1.1 polling模式 (OCO_ENABLED=False)
/Users/stonewang2014/gap-scanner/.venv/bin/python3 -u \
  /Users/stonewang2014/gap-scanner/stonewang_daytrade_1.1/live_trade.py

# 回测 (N天)
/Users/stonewang2014/gap-scanner/.venv/bin/python3 -c \
  "from backtest import run_backtest; run_backtest(n_days=30)"

# 5场景模拟器
/Users/stonewang2014/gap-scanner/.venv/bin/python3 stonewang_daytrade_1.1/stock_simulator.py
```
