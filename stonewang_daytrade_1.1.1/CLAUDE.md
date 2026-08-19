# Gap Scanner — stonewang_daytrade_1.1.1 量化交易系统

## 项目概述

美股日内gap交易系统，基于Alpaca API实盘运行。检测开盘跳空低开的股票，等待1分钟K线折返点确认后市价入场，阶梯挂单逐档卖出获利。

## 1.1.1 修复清单（相对于 1.1）

| 编号 | 严重度 | 修复内容 |
|------|--------|----------|
| 1.1.1-P0 | 严重 | ANSI颜色常量未定义: live_trade_stone_1.1.py中RED/GREEN/YELLOW/RESET未声明，导致place_protective_stop兜底逻辑NameError崩溃（2026-08-18 ENRD trailing stop失败时触发） |

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

### 阶梯挂单系统 (Ladder Sell) — 1.1修复版
- 买入成交后挂 **protective stop (100%仓位)**
- 价格触达T1目标 → **取消protective stop → 市价卖1/8仓位 → 重建trailing stop保护剩余**
- T1成交后 → trailing stop(2%)，触达T2 → 同样流程（取消trailing → 卖1/8 → 重建新trailing）
- 依次到T6，T6后只剩25%持仓，trailing stop(5%)保护
- **1.1关键变化**: 每次阶梯卖出前先取消protective/trailing stop，解锁股份后再卖，避免锁仓导致全仓退出
- **裸仓窗口**: 取消止损到卖出成交约1-2秒，风险可接受

### 6档目标系统
- 档位: 25% / 50% / 75% / 100% / 125% / 150% 回撤位
- 上限: 5% / 10% / 15% / 20% / 25% / 35% 涨幅封顶
- 每档卖出: 1/8仓位 (6×1/8=75%卖出，25%靠trailing stop)
- Trailing stop: 2.0% / 2.5% / 3.0% / 3.5% / 4.0% / 5.0%

### 仓位管理
- 持仓过程用5分钟K线（30天对比回测验证：5分钟优于1分钟）
- 初始止损: ATR×2，封顶10%最大亏损
- 时间限制: 40分钟(8根5分钟bar)无T1成交则breakeven退出
- EOD强制平仓: 15:50 EST

### Re-entry系统
- 首笔退出后可二次入场（半仓）
- 1档目标(75%回撤)，3% trailing stop
- 最小回调4%，无时间限制
- 止损退出后禁止re-entry（防止二次亏损）

## 关键文件

| 文件 | 作用 |
|------|------|
| `live_trade.py` | 实盘交易脚本（主程序，1.1.1 ANSI hotfix） |
| `config.py` | 配置文件（参数、API key，无变化） |
| `backtest.py` | 回测引擎 |
| `strategy.py` | 策略评估函数 |
| `scanner.py` | 股票扫描和筛选 |
| `stock_simulator.py` | 5场景模拟器 |
| `rejection_simulator.py` | 拒绝模拟器 |

## 配置要点

- `DRY_RUN = False` — 实盘模式
- `FORCE_QTY = 0` — 动态仓位计算
- `STOP_LOSS_MAX_PCT = 0.10` — 最大亏损10%（与回测一致）
- `TRAILING_STOP_PCTS = [0.02, 0.025, 0.03, 0.035, 0.04, 0.05]` — 与回测一致
- `MIN_POSITION_SIZE = 250` — 最小仓位$250（与回测一致）
- `REENTRY_MIN_PULLBACK = 0.04` — 最低4%回调（与回测一致）
- `LEVERAGED_ETF_SUFFIXES = ("BULL", "BEAR")` — 不再误排除正常股票
- `DATA_FEED = DataFeed.SIP` — SIP数据源
- 轮询间隔: 5秒（避免API限频）

## 运行方式

```bash
# 实盘运行
/Users/stonewang2014/gap-scanner/.venv/bin/python3 -u \
  /Users/stonewang2014/gap-scanner/versions/live_trade_stone_1.1.1.py

# 回测 (N天)
/Users/stonewang2014/gap-scanner/.venv/bin/python3 -c \
  "from backtest import run_backtest; run_backtest(n_days=30)"

# 5场景模拟器
/Users/stonewang2014/gap-scanner/.venv/bin/python3 stonewang_daytrade_1.1.1/stock_simulator.py
```
