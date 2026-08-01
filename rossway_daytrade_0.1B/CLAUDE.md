# rossway_daytrade_0.1B — 量价齐升 + 纯trailing stop

## 项目概述

美股日内gap交易系统，基于rossway 0.1改进：**入场增加量价齐升条件，退出改为纯trailing stop 2%**。扫描和选股完全继承stonewang_daytrade_1.3。

## 核心设计

| 参数 | 值 | 说明 |
|------|-----|------|
| 入场确认 | 3-bar + 量价齐升 | bottom bar + 3根确认bar (low>bottom, close>bottom, >=1阳线, >=1放量bar) |
| 放量标准 | 前5bar均量×1.5 | 确认bar成交量≥前5bar平均成交量1.5倍 |
| 退出方式 | trailing stop 2% | 纯追踪止损，无固定止损/止盈 |
| 多次入场 | 允许 | trailing stop成交后slot释放，可再次入场 |
| 止损后 | 禁止再入 | 同一股票当天trailing stop后不再入场 |
| 持仓过夜 | 禁止 | EOD 15:50强制平仓 |
| 最大同时持仓 | 5 | 同一时刻最多5只股票 |
| 扫描选股 | 继承1.3 | scanner.py + strategy.py 完全复用 |

## 0.1B vs 0.1 对比

| | rossway 0.1 | rossway 0.1B |
|---|---|---|
| 入场确认 | 3-bar (价涨+阳线) | 3-bar + 量价齐升 (确认bar放量>前5bar均量1.5倍) |
| 退出 | OCO (stop+target) | 纯trailing stop 2% |
| 止损 | max($0.15, entry×1.5%) | 无固定止损 |
| 止盈 | stop×2.5 | 无固定止盈 |
| Position字段 | stop_price, target_price, oco_order_id | trailing_order_id (仅1个) |

## 5日回测结果

| | rossway 0.1 | rossway 0.1B |
|---|---|---|
| 最终资金 | $1,490 | $663 |
| 交易次数 | 107 | 26 |
| 胜率 | 69% | 58% |
| 总P&L | +$990 | +$163 |

## 关键文件

| 文件 | 作用 |
|------|------|
| `live_trade.py` | 实盘交易脚本 |
| `config.py` | 配置文件 |
| `scanner.py` | 复用 stonewang_daytrade_1.3/scanner.py |
| `strategy.py` | 复用 stonewang_daytrade_1.3/strategy.py |
| `backtest.py` | 回测引擎（trailing stop模拟） |

## 配置要点

- `TRAILING_STOP_PCT = 0.02` — 2% trailing stop
- `VOLUME_RATIO_MIN = 1.5` — 确认bar放量≥前5bar均量1.5倍
- `MAX_POSITIONS = 5` — 最多5个同时持仓
- `MIN_POSITION_SIZE = 40` — 最小仓位$40
- `MAX_POSITION_SIZE = 200` — 最大仓位$200
- `DRY_RUN = False` — 实盘模式
- `MAX_DAILY_LOSS_PCT = 0.05` — 日亏5%熔断
- `POLL_INTERVAL = 3` — 轮询间隔秒

## 运行方式

```bash
# 实盘运行
/Users/stonewang2014/gap-scanner/venv/bin/python3 -u \
  /Users/stonewang2014/gap-scanner/rossway_daytrade_0.1B/live_trade.py

# 回测 (5天)
/Users/stonewang2014/gap-scanner/venv/bin/python3 \
  /Users/stonewang2014/gap-scanner/rossway_daytrade_0.1B/backtest.py
```
