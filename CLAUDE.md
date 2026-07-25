# Gap Scanner — Stone 1.1 量化交易系统

## 项目概述

美股日内gap交易系统，基于Alpaca API实盘运行。检测开盘跳空低开的股票，等待1分钟K线折返点确认后市价入场，阶梯挂单逐档卖出获利。

## 核心架构

### 入场系统
- **1分钟K线检测折返点** (`check_entry_1min`) — 价格跌破open_price后，5根1分钟bar确认底部
- **市价单入场** (`place_buy_market`) — 确认后立即市价买入，确保成交
- 入场窗口：9:31-10:00 EST，最多5支股票/天

### 阶梯挂单系统 (Ladder Sell)
- 买入成交后立即挂 **止损单 + T1限价卖单**
- T1成交 → 挂T2限价卖 + 移动trailing stop(2%)
- T2成交 → 挂T3 + trailing stop(2.5%)，依次到T6
- T6成交后只剩25%持仓，trailing stop(5%)保护
- **天然跳档**: 限价卖单"以设定价或更高价卖出"，价格跳档时自动在高价成交

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

## Stone 1.1 修复清单

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
| P1-9 | 重要 | 跳档处理同一轮处理所有触达tier |
| P1-10 | 重要 | scanner加GAP_MAX+MIN_DOLLAR_VOLUME+ETF过滤 |
| P1-11 | 重要 | ATR优先使用历史14日ATR |
| P1-12 | 重要 | calc_stop_price边界改为可配置 |
| P1-13 | 重要 | find_reentry_point加REENTRY_MIN_PULLBACK参数 |
| P1-14 | 重要 | _make_result修复bar引用（date_str参数） |
| P1-15 | 重要 | 拉回止损用20根1分钟滚动窗口 |
| P1-16 | 重要 | re-entry bar_count用accumulator计数 |
| P1-17 | 重要 | 止损退出后禁止re-entry |
| P1-18 | 重要 | BarAccumulator不完整桶检查(count>=5) |
| P1-19 | 重要 | WebSocket 60秒无数据自动重连 |
| P1-20 | 重要 | BarAccumulator线程安全(threading.Lock) |
| P2 | 次要 | 15项小修复（注释、显示、轮询间隔、回退值等） |

## 关键文件

| 文件 | 作用 |
|------|------|
| `versions/live_trade_stone_1.1.py` | 实盘交易脚本（主程序） |
| `versions/config_stone_1.1.py` | 配置文件（参数、API key） |
| `backtest.py` | 回测引擎 |
| `strategy.py` | 策略评估函数 |
| `scanner.py` | 股票扫描和筛选 |
| `versions/stock_simulator.py` | 5场景模拟器 |

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
  /Users/stonewang2014/gap-scanner/versions/live_trade_stone_1.1.py

# 回测 (N天)
/Users/stonewang2014/gap-scanner/.venv/bin/python3 -c \
  "from backtest import run_backtest; run_backtest(n_days=30)"

# 5场景模拟器
/Users/stonewang2014/gap-scanner/.venv/bin/python3 versions/stock_simulator.py
```
