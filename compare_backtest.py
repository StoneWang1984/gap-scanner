"""Compare old vs optimized strategy on the same 60-day period.

Old strategy:  Fixed 20% stop, fixed 75% take profit, no confirmation, no liquidity filter
Optimized:     ATR stops, entry confirmation, liquidity filter, tiered trailing stops
"""

import pandas as pd
import config
import strategy
import backtest as bt_module
from backtest import run_backtest, get_trading_days, bulk_scan_gaps, get_5min_bars
from scanner import get_data_client, get_tradable_symbols
from strategy import TradeResult, TradePlan, calc_price_at_retracement, calc_atr
from report import print_report


# ── Old strategy logic ──────────────────────────────────────────

def old_calc_stop_price(pullback, atr=0.0):
    return round(pullback * 0.80, 4)  # Fixed 20% stop


def old_build_trade_plan(symbol, open_price, pullback, atr=0.0):
    target_75 = calc_price_at_retracement(pullback, open_price, config.PROFIT_RETRACEMENT_75)
    stop_price = old_calc_stop_price(pullback)
    shares = int(config.POSITION_SIZE / pullback)
    return TradePlan(
        symbol=symbol, open_price=open_price, pullback=pullback,
        target_75=target_75, target_150=0, stop_price=stop_price,
        shares=shares, atr=0.0,
    )


def old_evaluate_trade(plan, bars_after_entry, force_close_price=None):
    """Old strategy: fixed 75% take profit, fixed 20% stop, no trailing."""
    for bar in bars_after_entry:
        bar_high = bar["high"]
        bar_low = bar["low"]

        # Stop loss (fixed 20%)
        if bar_low <= plan.stop_price:
            exit_price = plan.stop_price
            pnl = (exit_price - plan.pullback) * plan.shares
            pnl_pct = pnl / (plan.pullback * plan.shares) if plan.pullback > 0 else 0
            return TradeResult(
                symbol=plan.symbol, date=str(bar["timestamp"].date()),
                entry_price=plan.pullback, exit_price=exit_price, shares=plan.shares,
                pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4), exit_reason="stop_loss",
                open_price=plan.open_price, sell_target=plan.target_75,
                stop_price=plan.stop_price,
            )

        # Take profit at 75% retracement
        if bar_high >= plan.target_75:
            exit_price = plan.target_75
            pnl = (exit_price - plan.pullback) * plan.shares
            pnl_pct = pnl / (plan.pullback * plan.shares) if plan.pullback > 0 else 0
            return TradeResult(
                symbol=plan.symbol, date=str(bar["timestamp"].date()),
                entry_price=plan.pullback, exit_price=exit_price, shares=plan.shares,
                pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4), exit_reason="take_profit",
                open_price=plan.open_price, sell_target=plan.target_75,
                stop_price=plan.stop_price,
            )

    # Force close at EOD
    if force_close_price is not None:
        exit_price = force_close_price
    else:
        exit_price = bars_after_entry[-1]["close"] if bars_after_entry else plan.pullback

    pnl = (exit_price - plan.pullback) * plan.shares
    pnl_pct = pnl / (plan.pullback * plan.shares) if plan.pullback > 0 else 0

    return TradeResult(
        symbol=plan.symbol, date="",
        entry_price=plan.pullback, exit_price=exit_price, shares=plan.shares,
        pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4), exit_reason="force_close",
        open_price=plan.open_price, sell_target=plan.target_75,
        stop_price=plan.stop_price,
    )


# ── Shared bulk scan + dual evaluation ──────────────────────────

def run_comparison(n_days=60):
    client = get_data_client()
    end_date = pd.Timestamp.now(tz="America/New_York")

    trading_days = get_trading_days(client, end_date, n_days)
    if not trading_days:
        print("No trading days found.")
        return

    print(f"{'='*60}")
    print(f"对比回测 — {len(trading_days)} 个交易日: {trading_days[0].date()} ~ {trading_days[-1].date()}")
    print(f"{'='*60}")

    symbols = get_tradable_symbols()
    print(f"扫描 {len(symbols)} 只股票...")

    # Bulk scan WITHOUT liquidity filter (old strategy needs all candidates)
    orig_min_dv = config.MIN_DOLLAR_VOLUME
    config.MIN_DOLLAR_VOLUME = 0
    gap_data = bulk_scan_gaps(client, trading_days, symbols)
    config.MIN_DOLLAR_VOLUME = orig_min_dv

    total_candidates = sum(len(v) for v in gap_data.values())
    print(f"共找到 {total_candidates} 个跳空候选，覆盖 {len(gap_data)} 天")

    # Run both strategies on the same data
    old_trades = []
    new_trades = []
    old_equity = config.INITIAL_CAPITAL
    new_equity = config.INITIAL_CAPITAL

    for date in trading_days:
        date_key = date.date()
        if date_key not in gap_data or gap_data[date_key].empty:
            continue

        all_candidates = gap_data[date_key]
        # Old: no liquidity filter, take top N
        old_cands = all_candidates.head(config.MAX_POSITIONS_PER_DAY)
        # New: apply liquidity filter, then take top N
        new_cands = all_candidates[all_candidates["dollar_volume"] >= config.MIN_DOLLAR_VOLUME]
        new_cands = new_cands.head(config.MAX_POSITIONS_PER_DAY)

        print(f"\n--- {date_key} (全部: {len(all_candidates)}, 流动性过滤后: {len(new_cands)}) ---")

        # ── Old strategy ──
        old_pos = 0
        for _, row in old_cands.iterrows():
            if old_pos >= config.MAX_POSITIONS_PER_DAY:
                break
            symbol = row["symbol"]
            open_price = row["open_price"]

            bars_5m = get_5min_bars(client, symbol, date)
            if bars_5m.empty or len(bars_5m) < 3:
                continue

            # No confirmation — first pullback
            pullback = 0.0
            entry_bar_idx = -1
            for i in range(len(bars_5m)):
                if bars_5m.iloc[i]["low"] < open_price:
                    pullback = bars_5m.iloc[i]["low"]
                    entry_bar_idx = i
                    break

            if pullback <= 0:
                continue

            shares = int(config.POSITION_SIZE / pullback)
            if shares <= 0:
                continue

            plan = old_build_trade_plan(symbol, open_price, pullback)
            plan.shares = shares

            remaining_bars = bars_5m.iloc[entry_bar_idx + 1:]
            remaining_list = []
            for idx, bar in remaining_bars.iterrows():
                remaining_list.append({
                    "high": bar["high"], "low": bar["low"], "close": bar["close"],
                    "timestamp": idx if isinstance(idx, pd.Timestamp) else date,
                })

            force_close_price = remaining_list[-1]["close"] if remaining_list else None
            result = old_evaluate_trade(plan, remaining_list, force_close_price)
            result.date = str(date_key)
            result.open_price = open_price

            old_trades.append(result)
            old_equity += result.pnl
            old_pos += 1

        # ── New (optimized) strategy ──
        new_pos = 0
        for _, row in new_cands.iterrows():
            if new_pos >= config.MAX_POSITIONS_PER_DAY:
                break
            symbol = row["symbol"]
            open_price = row["open_price"]

            bars_5m = get_5min_bars(client, symbol, date)
            if bars_5m.empty or len(bars_5m) < 3:
                continue

            # With confirmation
            from backtest import find_entry_with_confirmation
            pullback, entry_bar_idx, confirmed = find_entry_with_confirmation(bars_5m, open_price)
            if not confirmed or pullback <= 0:
                continue

            # ATR
            bars_for_atr = []
            for j in range(min(entry_bar_idx + 1, len(bars_5m))):
                b = bars_5m.iloc[j]
                bars_for_atr.append({"high": b["high"], "low": b["low"], "close": b["close"]})
            atr = calc_atr(bars_for_atr, period=14)

            shares = int(config.POSITION_SIZE / pullback)
            if shares <= 0:
                continue

            plan = strategy.build_trade_plan(symbol, open_price, pullback, atr)
            plan.shares = shares

            remaining_bars = bars_5m.iloc[entry_bar_idx + 1:]
            remaining_list = []
            for idx, bar in remaining_bars.iterrows():
                remaining_list.append({
                    "high": bar["high"], "low": bar["low"], "close": bar["close"],
                    "timestamp": idx if isinstance(idx, pd.Timestamp) else date,
                })

            force_close_price = remaining_list[-1]["close"] if remaining_list else None
            result = strategy.evaluate_trade(plan, remaining_list, force_close_price)
            result.date = str(date_key)
            result.open_price = open_price
            result.sell_target = plan.target_150
            result.stop_price = plan.stop_price

            new_trades.append(result)
            new_equity += result.pnl
            new_pos += 1

    # ── Print comparison ──
    print_comparison(old_trades, new_trades, old_equity, new_equity, len(trading_days))


def print_comparison(old_trades, new_trades, old_equity, new_equity, n_days):
    def stats(trades, equity):
        if not trades:
            return {}
        peak = config.INITIAL_CAPITAL
        max_dd = 0
        wins = sum(1 for t in trades if t.pnl >= 0)
        losses = sum(1 for t in trades if t.pnl < 0)
        total_pnl = sum(t.pnl for t in trades)
        for t in trades:
            peak = max(peak, peak + t.pnl)  # approximate
            # Recalculate properly
        # Proper max drawdown
        equity_curve = [config.INITIAL_CAPITAL]
        for t in trades:
            equity_curve.append(equity_curve[-1] + t.pnl)
        peak = config.INITIAL_CAPITAL
        max_dd = 0
        for eq in equity_curve:
            peak = max(peak, eq)
            dd = (peak - eq) / peak
            max_dd = max(max_dd, dd)

        total_return = (equity - config.INITIAL_CAPITAL) / config.INITIAL_CAPITAL * 100
        trade_days = len(set(t.date for t in trades if t.date))
        daily_avg = total_return / max(trade_days, 1)

        exit_counts = {}
        for t in trades:
            r = t.exit_reason
            exit_counts[r] = exit_counts.get(r, 0) + 1

        return {
            "trades": len(trades),
            "equity": equity,
            "return": total_return,
            "daily_avg": daily_avg,
            "trade_days": trade_days,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / len(trades) * 100,
            "max_dd": max_dd * 100,
            "exits": exit_counts,
        }

    o = stats(old_trades, old_equity)
    n = stats(new_trades, new_equity)

    print(f"\n{'='*70}")
    print(f"{'对比结果':^70}")
    print(f"{'='*70}")
    print(f"{'指标':<20} {'旧策略':>20} {'优化后':>20} {'变化':>10}")
    print(f"{'-'*70}")
    print(f"{'交易天数':<20} {o.get('trade_days',0):>20} {n.get('trade_days',0):>20}")
    print(f"{'总交易次数':<20} {o.get('trades',0):>20} {n.get('trades',0):>20}")
    print(f"{'最终资金':<20} {'${:,.0f}'.format(o.get('equity',0)):>20} {'${:,.0f}'.format(n.get('equity',0)):>20}")
    print(f"{'总收益率':<20} {'{:.2f}%'.format(o.get('return',0)):>20} {'{:.2f}%'.format(n.get('return',0)):>20}")
    print(f"{'日均收益率':<20} {'{:.2f}%'.format(o.get('daily_avg',0)):>20} {'{:.2f}%'.format(n.get('daily_avg',0)):>20}")
    wr_o = o.get('win_rate', 0)
    wr_n = n.get('win_rate', 0)
    print(f"{'胜率':<20} {'{:.1f}%'.format(wr_o):>20} {'{:.1f}%'.format(wr_n):>20} {'{:+.1f}%'.format(wr_n - wr_o):>10}")
    print(f"{'胜/负':<20} {'{}/{}'.format(o.get('wins',0), o.get('losses',0)):>20} {'{}/{}'.format(n.get('wins',0), n.get('losses',0)):>20}")
    dd_o = o.get('max_dd', 0)
    dd_n = n.get('max_dd', 0)
    print(f"{'最大回撤':<20} {'-{:.2f}%'.format(dd_o):>20} {'-{:.2f}%'.format(dd_n):>20} {'{:+.2f}%'.format(dd_n - dd_o):>10}")

    print(f"\n{'出场方式':^70}")
    print(f"{'-'*70}")
    all_exit_reasons = set(list(o.get('exits', {}).keys()) + list(n.get('exits', {}).keys()))
    for reason in sorted(all_exit_reasons):
        oc = o.get('exits', {}).get(reason, 0)
        nc = n.get('exits', {}).get(reason, 0)
        print(f"  {reason:<30} {oc:>15} {nc:>15}")

    # Risk-reward comparison
    print(f"\n{'风险收益分析':^70}")
    print(f"{'-'*70}")
    if o.get('return', 0) > 0 and dd_o > 0:
        rr_o = o['return'] / dd_o
    else:
        rr_o = 0
    if n.get('return', 0) > 0 and dd_n > 0:
        rr_n = n['return'] / dd_n
    else:
        rr_n = 0
    print(f"  {'收益/回撤比':<30} {'{:.2f}'.format(rr_o):>15} {'{:.2f}'.format(rr_n):>15}")
    print(f"  {'每笔平均盈亏':<30} {'${:,.2f}'.format(o.get('equity',0)/max(o.get('trades',1),1) - config.INITIAL_CAPITAL/max(o.get('trades',1),1)):>15} {'${:,.2f}'.format(n.get('equity',0)/max(n.get('trades',1),1) - config.INITIAL_CAPITAL/max(n.get('trades',1),1)):>15}")

    print(f"\n{'='*70}")


if __name__ == "__main__":
    run_comparison(n_days=60)
