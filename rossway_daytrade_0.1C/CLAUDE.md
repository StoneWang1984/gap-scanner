# rossway_daytrade_0.1C — 量价齐升 + 两段式退出 (OCO半仓止盈 + Trailing Stop)

## 项目概述

美股日内gap交易系统，融合0.1和0.1B的优势：**量价齐升入场 + OCO半仓止盈锁利 + trailing stop 3%捕获大行情**。扫描和选股完全继承stonewang_daytrade_1.3。

## 核心设计

| 参数 | 值 | 说明 |
|------|-----|------|
| 入场确认 | 3-bar + 量价齐升 | bottom bar + 3根确认bar (low>bottom, close>bottom, >=1阳线, >=1放量bar) |
| 放量标准 | 前5bar均量×1.5 | 确认bar成交量≥前5bar平均成交量1.5倍 |
| Phase 1 | OCO | 止损: max($0.15, entry×1.5%), 止盈: stop×2.5, 卖出50%仓位 |
| Phase 2 | Trailing stop 3% | 止盈触发后，剩余50%转trailing stop 3% |
| 止损后 | 禁止再入 | 同一股票当天止损后不再入场 |
| 持仓过夜 | 禁止 | EOD 15:50强制平仓 |
| 最大同时持仓 | 5 | 同一时刻最多5只股票 |

## 0.1C vs 0.1 vs 0.1B 对比

| | rossway 0.1 | rossway 0.1B | rossway 0.1C |
|---|---|---|---|
| 入场 | 3-bar | 3-bar + 量价齐升(1.5x) | 3-bar + 量价齐升(1.5x) |
| 止损 | OCO: max($0.15, entry×1.5%) | 无固定止损 | OCO: max($0.15, entry×1.5%) |
| 止盈 | OCO: stop×2.5, 卖100% | 无固定止盈 | OCO: stop×2.5, 卖50% |
| 止盈后 | 仓位结束 | N/A | 剩余50%转trailing stop 3% |
| Trailing | 无 | 2% | 3% (止盈后激活) |

## 5日回测结果

| | rossway 0.1 | rossway 0.1B | rossway 0.1C |
|---|---|---|---|
| 最终资金 | $1,490 | $663 | $670 |
| 交易次数 | 107 | 26 | 58 |
| 胜率 | 69% | 58% | 53% |
| 总P&L | +$990 | +$163 | +$170 |

**协同效果**: EDBL tp_pnl=$0.72 + trail_pnl=$37.92 = +$38.64（+97%），REPL tp_pnl=$0.63 + trail_pnl=$28.04 = +$28.67（+73%）

## 关键文件

| 文件 | 作用 |
|------|------|
| `live_trade.py` | 实盘交易脚本（两段式退出） |
| `config.py` | 配置文件 |
| `scanner.py` | 复用 stonewang_daytrade_1.3/scanner.py |
| `strategy.py` | 复用 stonewang_daytrade_1.3/strategy.py |
| `backtest.py` | 回测引擎（两段式退出模拟） |

## 配置要点

- `STOP_LOSS_MIN_CENTS = 0.15` — 最低15美分止损
- `STOP_LOSS_PCT = 0.015` — 1.5%止损
- `REWARD_RISK_RATIO = 2.5` — 止盈=止损×2.5
- `TP_SELL_RATIO = 0.5` — 止盈卖出50%仓位
- `TRAILING_STOP_PCT = 0.03` — 3% trailing stop (Phase 2)
- `VOLUME_RATIO_MIN = 1.5` — 确认bar放量≥前5bar均量1.5倍
- `MAX_POSITIONS = 5` — 最多5个同时持仓
- `MIN_POSITION_SIZE = 40` — 最小仓位$40
- `MAX_POSITION_SIZE = 200` — 最大仓位$200
- `DRY_RUN = False` — 实盘模式
- `MAX_DAILY_LOSS_PCT = 0.05` — 日亏5%熔断

## 运行方式

```bash
# 实盘运行
/Users/stonewang2014/gap-scanner/venv/bin/python3 -u \
  /Users/stonewang2014/gap-scanner/rossway_daytrade_0.1C/live_trade.py

# 回测 (5天)
/Users/stonewang2014/gap-scanner/venv/bin/python3 \
  /Users/stonewang2014/gap-scanner/rossway_daytrade_0.1C/backtest.py
```
