# rossway_daytrade_0.1 — 简洁交易系统

## 项目概述

美股日内gap交易系统，极简设计：**一次入场、一个OCO、固定盈亏比2.5:1**。扫描和选股完全继承stonewang_daytrade_1.3。

## 核心设计

| 参数 | 值 | 说明 |
|------|-----|------|
| 入场确认 | 3-bar (from 1.2) | bottom bar + 3根确认bar (low>bottom, close>bottom, >=1阳线) |
| 止损 | max($0.15, entry×1.5%) | 低价股至少15美分，高价股1.5% |
| 止盈 | 止损 × 2.5 | 固定盈亏比2.5:1 |
| 退出方式 | OCO单 | take_profit + stop_loss 一次性挂好 |
| 多次入场 | 允许 | OCO成交后slot释放，可再次入场 |
| 止损后 | 禁止再入 | 同一股票当天止损后不再入场 |
| 持仓过夜 | 禁止 | EOD 15:50强制平仓 |
| 最大同时持仓 | 5 | 同一时刻最多5只股票 |
| 扫描选股 | 继承1.3 | scanner.py + strategy.py 完全复用 |

## 架构对比

| | stonewang 1.3 | rossway 0.1 |
|---|---|---|
| 入场 | 3-tier锤子线(1-3min) | 3-bar确认(3+min) |
| 退出 | 8档阶梯OCO+trailing | 单次OCO（止盈2.5×止损） |
| 止损 | ATR×2, 10%封顶 | max($0.15, entry×1.5%) |
| 止盈 | 8档targets | 止损×2.5 |
| trailing | 8档递增1-5% | 无（OCO覆盖） |
| 再入场 | 有（半仓→全仓） | 有（slot释放后） |
| 仓位管理 | 30+字段 | 9字段 |

## 关键文件

| 文件 | 作用 |
|------|------|
| `live_trade.py` | 实盘交易脚本（~700行 vs 1.3的4000+行） |
| `config.py` | 配置文件 |
| `scanner.py` | 复用 stonewang_daytrade_1.3/scanner.py |
| `strategy.py` | 复用 stonewang_daytrade_1.3/strategy.py |
| `backtest.py` | 回测引擎（简化版） |

## 配置要点

- `STOP_LOSS_MIN_CENTS = 0.15` — 最低15美分止损
- `STOP_LOSS_PCT = 0.015` — 1.5%止损
- `REWARD_RISK_RATIO = 2.5` — 止盈=止损×2.5
- `STOP_LIMIT_BUFFER = 0.03` — stop-limit 3% buffer
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
  /Users/stonewang2014/gap-scanner/rossway_daytrade_0.1/live_trade.py

# 回测 (5天)
/Users/stonewang2014/gap-scanner/venv/bin/python3 \
  /Users/stonewang2014/gap-scanner/rossway_daytrade_0.1/backtest.py
```
