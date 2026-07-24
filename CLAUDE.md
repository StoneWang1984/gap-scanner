# Gap Scanner — Stone 1.0 量化交易系统

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
- 最小回调3%，无时间限制

## 关键文件

| 文件 | 作用 |
|------|------|
| `versions/live_trade_stone_1.0.py` | 实盘交易脚本（主程序） |
| `versions/config_stone_1.0.py` | 配置文件（参数、API key） |
| `backtest.py` | 回测引擎 |
| `strategy.py` | 策略评估函数（evaluate_trade_stone, calc_targets等） |
| `scanner.py` | 股票扫描和筛选 |
| `monte_carlo_test.py` | Monte Carlo随机价格模拟测试 |
| `compare_bar_resolution.py` | 1分钟 vs 5分钟持仓管理对比 |

## 配置要点

- `DRY_RUN = False` — 实盘模式
- `ALPACA_PAPER = False` — 真实账户（非模拟）
- `FORCE_QTY = 8` — 测试模式固定8股
- `FIRST_TRADE_TIME_LIMIT_BARS = 8` — 40分钟时间限制
- `TARGET_LIMIT_BUFFER = 0.003` — 限价卖单buffer (0.3%)
- `DATA_FEED = DataFeed.SIP` — SIP数据源（实盘）

## 运行方式

```bash
# 实盘运行
/Users/stonewang2014/gap-scanner/.venv/bin/python3 -u \
  /Users/stonewang2014/gap-scanner/versions/live_trade_stone_1.0.py

# 回测 (N天)
/Users/stonewang2014/gap-scanner/.venv/bin/python3 -c \
  "from backtest import run_backtest; run_backtest(n_days=30)"

# Monte Carlo模拟测试
/Users/stonewang2014/gap-scanner/.venv/bin/python3 monte_carlo_test.py 5000
```

## 已验证的关键结论

1. **5分钟K线更适合持仓管理** — 30天对比：5分钟在75.8%的交易中表现更好，1分钟trailing stop触发太早截断利润
2. **市价单比限价单更可靠** — 限价单在pullback点无法预测价格，成交率低
3. **阶梯挂单零延迟成交** — 买入时立刻挂T1，价格到自动成交，无轮询延迟
4. **Monte Carlo验证系统稳定** — 5000次随机模拟：93.9%胜率，PF=1.46，7个边缘测试全通过

## 注意事项

- 实盘脚本重启后需扫描Alpaca开放订单，重建pending阶梯卖单（recovery逻辑）
- `LivePosition.next_tier_idx` 跟踪下一个要挂单的档位，是阶梯系统的核心状态
- WebSocket断线后自动重连
- 强制平仓流程：取消所有卖单 → close_position → market sell fallback
