# rossway_daytrade_0.1wp — 分档止损止盈版

## 项目概述

美股日内gap交易系统，基于rossway 0.1，核心改进：**按价格分档设置止损止盈百分比**，经6个月回测验证最优。

## 与 rossway 0.1 的区别

| | rossway 0.1 | rossway 0.1wp |
|---|---|---|
| 止损 | max($0.15, entry×1.5%) | 按价格分档 (6%→1.25%) |
| 止盈 | 止损×2.5 | 按价格分档 (12%→2%) |
| R:R | 固定2.5:1 | 分档1.5-2.3:1 |
| 回测(6月) | 未验证 | 1,401笔, +151.6% |

## 止损止盈分档 (STOP_TIERS)

| 价格档 | 止损 | 止盈 | R:R | 说明 |
|--------|------|------|-----|------|
| $1-2 | 6% | 12% | 2.0 | 低价股波动大，宽止损宽止盈 |
| $2-3 | 5% | 10% | 2.0 | 同上 |
| $3-4 | 4% | 6% | 1.5 | 波动减小，收窄 |
| $4-5 | 3% | 4.5% | 1.5 | 中价股 |
| $5-10 | 2% | 4% | 2.0 | 止盈经回测验证最优 |
| $10-15 | 1.5% | 3.5% | 2.3 | 微调验证 |
| $15-20 | 1.25% | 2% | 1.6 | 高价股波动小 |

## 6个月回测结果

- 总交易: 1,401笔 | 胜率: 44.9%
- 平均盈利: $2.35 | 平均亏损: -$0.93 | 盈亏比: 2.53:1
- 最终资金: $1,258 (初始$500, +151.6%)
- $1-2和$2-3两档贡献77%利润

## 核心设计

| 参数 | 值 | 说明 |
|------|-----|------|
| 入场确认 | 3-bar | bottom bar + 3根确认bar (low>bottom, close>bottom, >=1阳线) |
| 退出方式 | OCO单 | take_profit + stop_loss 一次性挂好 |
| 多次入场 | 允许 | OCO成交后slot释放，可再次入场 |
| 止损后 | 禁止再入 | 同一股票当天止损后不再入场 |
| 持仓过夜 | 禁止 | EOD 15:50强制平仓 |
| 最大同时持仓 | 5 | 同一时刻最多5只股票 |

## 关键文件

| 文件 | 作用 |
|------|------|
| `live_trade.py` | 实盘交易脚本 |
| `config.py` | 配置文件 (含STOP_TIERS) |
| `scanner.py` | 复用 stonewang_daytrade_1.3/scanner.py |
| `strategy.py` | 复用 stonewang_daytrade_1.3/strategy.py |
| `backtest.py` | 回测引擎 (RTH过滤 + 分档止盈止损) |

## 运行方式

```bash
# 实盘运行
/Users/stonewang2014/gap-scanner/venv/bin/python3 -u \
  /Users/stonewang2014/gap-scanner/versions/rossway_daytrade_0.1wp/live_trade.py

# 回测 (6个月)
/Users/stonewang2014/gap-scanner/venv/bin/python3 \
  /Users/stonewang2014/gap-scanner/versions/rossway_daytrade_0.1wp/backtest.py
```
