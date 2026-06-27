"""Test 6 optimizations individually on the same 60-day period.
Shares bulk scan + 5-min bar data across all variants for speed."""

import copy
import pandas as pd
import config
from scanner import get_data_client, get_tradable_symbols
from backtest import get_trading_days, bulk_scan_gaps, get_5min_bars, find_entry_with_confirmation
from strategy import (
    TradeResult, TradePlan, calc_price_at_retracement, calc_atr,
    calc_stop_price, build_trade_plan, evaluate_trade,
)
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import Adjustment, DataFeed

# Variant parameters
RISK_PER_TRADE = 10000       # ATR dynamic position sizing
DAILY_LOSS_LIMIT = 5000      # Daily max loss limit
ENTRY_TIME_CUTOFF = "10:00"  # Only enter before 10:00 AM


# ── Helpers ──────────────────────────────────────────────────────

def get_spy_returns(client, trading_days):
    start = trading_days[0] - pd.Timedelta(days=7)
    end = trading_days[-1] + pd.Timedelta(days=1)
    request = StockBarsRequest(
        symbol_or_symbols="SPY", timeframe=TimeFrame.Day,
        start=start, end=end, adjustment=Adjustment.RAW, feed=DataFeed.IEX,
    )
    bars = client.get_stock_bars(request)
    if bars.df.empty:
        return {}
    df = bars.df
    sym_df = df[df.index.get_level_values("symbol") == "SPY"].sort_index()
    returns = {}
    for i in range(1, len(sym_df)):
        idx = sym_df.index[i]
        if isinstance(idx, tuple):
            d = idx[1].date() if hasattr(idx[1], 'date') else pd.Timestamp(idx[1]).date()
        else:
            d = idx.date() if hasattr(idx, 'date') else pd.Timestamp(idx).date()
        returns[d] = (sym_df.iloc[i]["close"] / sym_df.iloc[i-1]["close"]) - 1.0
    return returns


def param_evaluate_trade(
    plan, bars_after_entry, force_close_price=None,
    trail_pct_75=None, trail_pct_150=None,
    use_atr_trailing=False, atr_trail=0.0,
    atr_trail_mult_75=1.5, atr_trail_mult_150=2.0,
):
    """Evaluate trade with parameterized trailing stop logic."""
    if trail_pct_75 is None:
        trail_pct_75 = config.TRAILING_STOP_PCT_75
    if trail_pct_150 is None:
        trail_pct_150 = config.TRAILING_STOP_PCT_150

    reached_75 = reached_150 = sold_partial = False
    partial_sell_price = 0.0
    partial_sell_shares = 0
    highest = plan.pullback
    remaining_shares = plan.shares

    for bar in bars_after_entry:
        bh, bl = bar["high"], bar["low"]
        if bh > highest:
            highest = bh

        if bl <= plan.stop_price:
            exit_price = plan.stop_price
            pp = (partial_sell_price - plan.pullback) * partial_sell_shares if sold_partial else 0
            pr = (exit_price - plan.pullback) * remaining_shares
            pnl = pp + pr
            pnl_pct = pnl / (plan.pullback * plan.shares) if plan.pullback > 0 else 0
            return TradeResult(
                symbol=plan.symbol, date=str(bar["timestamp"].date()),
                entry_price=plan.pullback, exit_price=exit_price, shares=plan.shares,
                pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4), exit_reason="stop_loss",
                open_price=plan.open_price, sell_target=plan.target_150,
                stop_price=plan.stop_price, partial_sell_price=partial_sell_price,
                partial_sell_shares=partial_sell_shares, trailing_high=highest,
                trailing_exit_price=exit_price, atr=plan.atr,
            )

        if not reached_150 and bh >= plan.target_150:
            reached_150 = reached_75 = True
            if not sold_partial:
                sold_partial = True
                partial_sell_price = plan.target_150
                partial_sell_shares = plan.shares // 3
                remaining_shares = plan.shares - partial_sell_shares

        if not reached_75 and bh >= plan.target_75:
            reached_75 = True

        if reached_75:
            if use_atr_trailing and atr_trail > 0:
                mult = atr_trail_mult_150 if reached_150 else atr_trail_mult_75
                tsp = round(highest - mult * atr_trail, 4)
            else:
                pct = trail_pct_150 if reached_150 else trail_pct_75
                tsp = round(highest * (1 - pct), 4)
            tsp = max(tsp, plan.pullback)

            if bl <= tsp:
                pp = (partial_sell_price - plan.pullback) * partial_sell_shares if sold_partial else 0
                pr = (tsp - plan.pullback) * remaining_shares
                pnl = pp + pr
                pnl_pct = pnl / (plan.pullback * plan.shares) if plan.pullback > 0 else 0
                return TradeResult(
                    symbol=plan.symbol, date=str(bar["timestamp"].date()),
                    entry_price=plan.pullback, exit_price=tsp, shares=plan.shares,
                    pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4),
                    exit_reason="trailing_stop_150" if reached_150 else "trailing_stop_75",
                    open_price=plan.open_price, sell_target=plan.target_150,
                    stop_price=plan.stop_price, partial_sell_price=partial_sell_price,
                    partial_sell_shares=partial_sell_shares, trailing_high=highest,
                    trailing_exit_price=tsp, atr=plan.atr,
                )

    if force_close_price is not None:
        exit_price = force_close_price
    else:
        exit_price = bars_after_entry[-1]["close"] if bars_after_entry else plan.pullback
    if reached_75 and not sold_partial:
        exit_price = max(exit_price, plan.pullback)
    elif not reached_75:
        exit_price = plan.pullback

    pp = (partial_sell_price - plan.pullback) * partial_sell_shares if sold_partial else 0
    pr = (exit_price - plan.pullback) * remaining_shares
    pnl = pp + pr
    pnl_pct = pnl / (plan.pullback * plan.shares) if plan.pullback > 0 else 0
    return TradeResult(
        symbol=plan.symbol, date="", entry_price=plan.pullback,
        exit_price=exit_price, shares=plan.shares,
        pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4), exit_reason="force_close",
        open_price=plan.open_price, sell_target=plan.target_150,
        stop_price=plan.stop_price, partial_sell_price=partial_sell_price,
        partial_sell_shares=partial_sell_shares, trailing_high=highest,
        trailing_exit_price=exit_price, atr=plan.atr,
    )


def collect_trade_data(client, trading_days, gap_data):
    """Fetch 5-min bars + compute entry/ATR for all candidates once."""
    trade_data = {}
    for date in trading_days:
        dk = date.date()
        if dk not in gap_data or gap_data[dk].empty:
            continue
        candidates = gap_data[dk].head(config.MAX_POSITIONS_PER_DAY)
        day_entries = []
        for _, row in candidates.iterrows():
            symbol, open_price = row["symbol"], row["open_price"]
            bars_5m = get_5min_bars(client, symbol, date)
            if bars_5m.empty or len(bars_5m) < 3:
                continue
            pullback, ebi, confirmed = find_entry_with_confirmation(bars_5m, open_price)
            if not confirmed or pullback <= 0:
                continue

            bars_atr = []
            for j in range(min(ebi + 1, len(bars_5m))):
                b = bars_5m.iloc[j]
                bars_atr.append({"high": b["high"], "low": b["low"], "close": b["close"]})
            atr = calc_atr(bars_atr, period=14)

            plan = build_trade_plan(symbol, open_price, pullback, atr)
            plan.shares = int(config.POSITION_SIZE / pullback)

            remaining_bars = bars_5m.iloc[ebi + 1:]
            rlist = []
            for idx, bar in remaining_bars.iterrows():
                ts = idx if isinstance(idx, pd.Timestamp) else date
                rlist.append({"high": bar["high"], "low": bar["low"], "close": bar["close"], "timestamp": ts})
            fcp = rlist[-1]["close"] if rlist else None

            entry_ts = None
            if ebi < len(bars_5m):
                ei = bars_5m.index[ebi]
                entry_ts = pd.Timestamp(ei[1]) if isinstance(ei, tuple) else (ei if isinstance(ei, pd.Timestamp) else None)

            day_entries.append({
                "symbol": symbol, "open_price": open_price, "pullback": pullback,
                "atr": atr, "gap_pct": row["gap_pct"], "plan": plan,
                "shares": plan.shares, "remaining_list": rlist,
                "force_close_price": fcp, "entry_bar_ts": entry_ts, "date": date,
            })
        trade_data[dk] = day_entries
    return trade_data


# ── Variant runners ──────────────────────────────────────────────

def run_variant(trade_data, trading_days, spy_returns, variant):
    trades = []
    equity = config.INITIAL_CAPITAL

    for date in trading_days:
        dk = date.date()
        if dk not in trade_data:
            continue

        # Market filter: skip if SPY previous day down >1.5%
        if variant == "market_filter" and spy_returns.get(dk, 0) < -0.015:
            continue

        daily_pnl = 0
        pos = 0

        for entry in trade_data[dk]:
            if pos >= config.MAX_POSITIONS_PER_DAY:
                break

            # Daily loss limit
            if variant == "daily_loss_limit" and daily_pnl <= -DAILY_LOSS_LIMIT:
                break

            # Time window: skip if entry after 10:00
            if variant == "time_window" and entry["entry_bar_ts"] is not None:
                cutoff = pd.Timestamp(f"{dk} {ENTRY_TIME_CUTOFF}", tz="America/New_York")
                if entry["entry_bar_ts"] > cutoff:
                    continue

            plan = copy.deepcopy(entry["plan"])
            rl = entry["remaining_list"]
            fcp = entry["force_close_price"]

            if variant == "atr_position":
                sd = plan.pullback - plan.stop_price
                if sd > 0:
                    plan.shares = min(int(RISK_PER_TRADE / sd), int(config.POSITION_SIZE / plan.pullback))
                else:
                    plan.shares = entry["shares"]

            if variant == "gap_tiering":
                gp = entry["gap_pct"]
                atr = entry["atr"]
                pb = entry["pullback"]
                if gp > 1.0:
                    am, tp75, tp150 = 3.0, 0.015, 0.03
                elif gp > 0.5:
                    am, tp75, tp150 = 2.5, 0.025, 0.04
                else:
                    am, tp75, tp150 = config.STOP_LOSS_ATR_MULT, config.TRAILING_STOP_PCT_75, config.TRAILING_STOP_PCT_150
                if atr > 0:
                    s = pb - am * atr
                    s = max(pb * 0.70, min(pb * 0.95, s))
                    plan.stop_price = round(s, 4)
                result = param_evaluate_trade(plan, rl, fcp, trail_pct_75=tp75, trail_pct_150=tp150)

            elif variant == "atr_trailing":
                result = param_evaluate_trade(
                    plan, rl, fcp,
                    use_atr_trailing=True, atr_trail=entry["atr"],
                    atr_trail_mult_75=1.5, atr_trail_mult_150=2.0,
                )
            else:
                result = evaluate_trade(plan, rl, fcp)

            result.date = str(dk)
            result.open_price = entry["open_price"]
            if variant in ("gap_tiering", "atr_trailing"):
                result.sell_target = plan.target_150
                result.stop_price = plan.stop_price

            trades.append(result)
            equity += result.pnl
            daily_pnl += result.pnl
            pos += 1

    return trades, equity


# ── Stats & output ───────────────────────────────────────────────

def stats(trades, equity, n_days):
    if not trades:
        return dict(trades=0, equity=equity, ret=0, daily=0, wr=0, dd=0, avg=0, exits={})
    ec = [config.INITIAL_CAPITAL]
    for t in trades:
        ec.append(ec[-1] + t.pnl)
    peak, max_dd = config.INITIAL_CAPITAL, 0
    for e in ec:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak)
    w = sum(1 for t in trades if t.pnl >= 0)
    ret = (equity - config.INITIAL_CAPITAL) / config.INITIAL_CAPITAL * 100
    ex = {}
    for t in trades:
        ex[t.exit_reason] = ex.get(t.exit_reason, 0) + 1
    return dict(
        trades=len(trades), equity=equity, ret=ret, daily=ret/n_days,
        wins=w, losses=len(trades)-w, wr=w/len(trades)*100, dd=max_dd*100,
        avg=sum(t.pnl for t in trades)/len(trades), exits=ex,
    )


def main():
    client = get_data_client()
    end_date = pd.Timestamp.now(tz="America/New_York")
    n_days = 60

    trading_days = get_trading_days(client, end_date, n_days)
    if not trading_days:
        print("No trading days found."); return

    print(f"回测期间: {trading_days[0].date()} ~ {trading_days[-1].date()} ({len(trading_days)} 天)")

    symbols = get_tradable_symbols()
    print(f"扫描 {len(symbols)} 只股票...")
    gap_data = bulk_scan_gaps(client, trading_days, symbols)
    for d in gap_data:
        gap_data[d] = gap_data[d][gap_data[d]["dollar_volume"] >= config.MIN_DOLLAR_VOLUME]

    print("获取5分钟K线数据（共用，仅获取一次）...")
    trade_data = collect_trade_data(client, trading_days, gap_data)

    print("获取SPY数据...")
    spy_returns = get_spy_returns(client, trading_days)

    total_entries = sum(len(v) for v in trade_data.values())
    print(f"共 {total_entries} 笔交易数据待评估")

    variants = [
        ("baseline",         "基线（当前策略）"),
        ("atr_position",     "① ATR动态仓位"),
        ("market_filter",    "② 大盘环境过滤"),
        ("gap_tiering",      "③ 跳空幅度分级"),
        ("time_window",      "④ 入场时间窗口"),
        ("daily_loss_limit", "⑤ 每日亏损限额"),
        ("atr_trailing",     "⑥ ATR移动止盈"),
    ]

    results = {}
    for vid, vname in variants:
        trades, equity = run_variant(trade_data, trading_days, spy_returns, vid)
        results[vid] = (trades, equity)
        s = stats(trades, equity, len(trading_days))
        print(f"  {vname}: {s['trades']}笔, 收益{s['ret']:.1f}%, 日均{s['daily']:.2f}%, 回撤-{s['dd']:.2f}%")

    # ── Print table ──
    nd = len(trading_days)
    bl = stats(*results["baseline"], nd)

    print(f"\n{'='*110}")
    print(f"{'6项优化对比结果（同一60天时间段）':^110}")
    print(f"{'='*110}")
    print(f"{'优化项':<22} {'交易':>5} {'总收益率':>10} {'日均收益率':>11} {'胜率':>8} {'最大回撤':>10} {'收益/回撤比':>11} {'每笔均盈':>11}")
    print(f"{'-'*110}")

    for vid, vname in variants:
        s = stats(*results[vid], nd)
        rr = s['ret']/s['dd'] if s['dd'] > 0 else 0
        mark = " ★" if vid == "baseline" else ""
        print(f"{vname:<22} {s['trades']:>5} {s['ret']:>9.1f}% {s['daily']:>10.2f}% "
              f"{s['wr']:>7.1f}% {'-'+format(s['dd'],'.2f')+'%':>9} {rr:>10.2f} "
              f"${s['avg']:>10,.0f}{mark}")

    # ── Improvement table ──
    print(f"\n{'='*110}")
    print(f"{'相对基线的变化':^110}")
    print(f"{'='*110}")
    print(f"{'优化项':<22} {'总收益':>10} {'日均收益':>11} {'最大回撤':>11} {'收益/回撤比':>12}")
    print(f"{'-'*110}")

    for vid, vname in variants:
        if vid == "baseline":
            continue
        s = stats(*results[vid], nd)
        rd = s['ret'] - bl['ret']
        dd = s['daily'] - bl['daily']
        ddd = s['dd'] - bl['dd']  # negative = improvement
        rr_b = bl['ret']/bl['dd'] if bl['dd'] > 0 else 0
        rr_v = s['ret']/s['dd'] if s['dd'] > 0 else 0
        rrd = rr_v - rr_b
        print(f"{vname:<22} {rd:>+9.1f}% {dd:>+10.2f}% {ddd:>+10.2f}% {rrd:>+11.2f}")

    # ── Conclusion ──
    print(f"\n{'='*110}")
    print(f"{'结论':^110}")
    print(f"{'='*110}")

    # Rank by return/drawdown ratio improvement
    improvements = []
    for vid, vname in variants:
        if vid == "baseline":
            continue
        s = stats(*results[vid], nd)
        rr_b = bl['ret']/bl['dd'] if bl['dd'] > 0 else 0
        rr_v = s['ret']/s['dd'] if s['dd'] > 0 else 0
        improvements.append((vid, vname, rr_v - rr_b, s['ret'] - bl['ret'], s['dd'] - bl['dd']))
    improvements.sort(key=lambda x: x[2], reverse=True)

    for i, (vid, vname, rr_imp, ret_imp, dd_imp) in enumerate(improvements, 1):
        verdict = "✓ 推荐" if rr_imp > 0 and ret_imp >= 0 else ("△ 收益下降但风险改善" if rr_imp > 0 else "✗ 不推荐")
        print(f"  {i}. {vname}: {verdict} (收益{ret_imp:+.1f}%, 回撤{dd_imp:+.2f}%, 收益回撤比{rr_imp:+.2f})")

    print(f"\n{'='*110}")


if __name__ == "__main__":
    main()
