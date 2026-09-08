"""Compare Stone 0.4.10 vs 0.4.11 backtest on same week data."""

import pandas as pd
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import Adjustment, DataFeed

import config
from scanner import get_data_client, get_tradable_symbols
from strategy import (
    evaluate_trade_stone, evaluate_reentry_trade,
    calc_atr, calc_stop_price, calc_price_at_retracement, calc_position_size,
    find_reentry_point, TradePlan,
)
from backtest import (
    get_trading_days, bulk_scan_gaps, get_5min_bars, _bars_to_list,
)


# ── 0.4.10: Old entry confirmation (pre-running-minimum) ──
def find_entry_old(bars_5m, open_price):
    """0.4.10: original entry confirmation with potential skip bug."""
    if bars_5m.empty or len(bars_5m) < 2:
        return 0, -1, False
    for i in range(len(bars_5m)):
        if bars_5m.iloc[i]["low"] < open_price:
            pullback_price = bars_5m.iloc[i]["low"]
            if not config.ENTRY_CONFIRMATION:
                return pullback_price, i, True
            if i + 1 < len(bars_5m) and bars_5m.iloc[i + 1]["low"] >= pullback_price:
                return pullback_price, i, True
            for j in range(i + 2, len(bars_5m)):
                bar = bars_5m.iloc[j]
                prev = bars_5m.iloc[j - 1]
                if bar["low"] < open_price and prev["low"] >= bar["low"]:
                    if j + 1 < len(bars_5m) and bars_5m.iloc[j + 1]["low"] >= bar["low"]:
                        return bar["low"], j, True
            return pullback_price, i, True
    return 0, -1, False


# ── 0.4.11: Running-minimum entry confirmation ──
def find_entry_new(bars_5m, open_price):
    """0.4.11: running-minimum approach finds true pullback bottom."""
    if bars_5m.empty or len(bars_5m) < 2:
        return 0, -1, False
    pullback_idx = -1
    pullback_price = 0.0
    for i in range(len(bars_5m)):
        if bars_5m.iloc[i]["low"] < open_price:
            pullback_idx = i
            pullback_price = bars_5m.iloc[i]["low"]
            break
    if pullback_idx < 0:
        return 0, -1, False
    if not config.ENTRY_CONFIRMATION:
        return pullback_price, pullback_idx, True
    if pullback_idx + 1 >= len(bars_5m):
        return 0, -1, False
    for i in range(pullback_idx + 1, len(bars_5m)):
        bar_low = bars_5m.iloc[i]["low"]
        if bar_low < open_price and bar_low < pullback_price:
            pullback_idx = i
            pullback_price = bar_low
        elif bar_low >= pullback_price:
            return pullback_price, pullback_idx, True
    return pullback_price, pullback_idx, True


def _get_entry_ts(bars_5m, entry_bar_idx):
    idx_val = bars_5m.index[entry_bar_idx]
    ts = idx_val[1] if isinstance(idx_val, tuple) else idx_val
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize('UTC')
    return ts.tz_convert('America/New_York')


def run_version(client, trading_days, gap_data, find_entry_fn, skip_entry_above_open,
                version_label):
    """Run backtest with given entry function and rules."""
    all_trades = []
    equity = config.INITIAL_CAPITAL

    for date in trading_days:
        date_key = date.date()
        if date_key not in gap_data or gap_data[date_key].empty:
            continue

        deployable = calc_position_size(equity)
        pos_per_stock = min(deployable, config.MAX_POSITION_SIZE)
        max_stocks_today = max(config.MAX_POSITIONS_PER_DAY, int(deployable / pos_per_stock))
        candidates = gap_data[date_key].head(max_stocks_today)

        daily_trades = 0
        daily_stopped = False

        for _, row in candidates.iterrows():
            if daily_trades >= config.MAX_DAILY_TRADES or daily_stopped:
                break

            symbol = row["symbol"]
            open_price = row["open_price"]

            bars_5m = get_5min_bars(client, symbol, date)
            if bars_5m.empty or len(bars_5m) < 3:
                continue

            all_bars = _bars_to_list(bars_5m)

            # First trade
            pullback, entry_bar_idx, confirmed = find_entry_fn(bars_5m, open_price)
            if not confirmed or pullback <= 0:
                continue

            # 0.4.11 rule: skip if entry >= open
            if skip_entry_above_open and pullback >= open_price:
                continue

            # Entry time check
            entry_ts = _get_entry_ts(bars_5m, entry_bar_idx)
            cutoff = pd.Timestamp(f"{date_key} 10:00", tz="America/New_York")
            if entry_ts > cutoff:
                continue

            # ATR
            bars_for_atr = []
            for j in range(min(entry_bar_idx + 1, len(bars_5m))):
                b = bars_5m.iloc[j]
                bars_for_atr.append({"high": b["high"], "low": b["low"], "close": b["close"]})
            atr = calc_atr(bars_for_atr, period=14)

            stop_price = calc_stop_price(pullback, atr)
            target_75 = calc_price_at_retracement(pullback, open_price, config.PROFIT_RETRACEMENT_75)
            target_1125 = calc_price_at_retracement(pullback, open_price, config.PROFIT_RETRACEMENT_1125)
            target_150 = calc_price_at_retracement(pullback, open_price, config.PROFIT_RETRACEMENT_150)

            pos_size = min(calc_position_size(equity), config.MAX_POSITION_SIZE)
            shares = int(pos_size / pullback)
            if shares <= 0:
                continue

            plan = TradePlan(
                symbol=symbol, open_price=open_price, pullback=pullback,
                target_75=target_75, target_1125=target_1125, target_150=target_150,
                stop_price=stop_price, shares=shares, atr=atr,
            )

            remaining_list = all_bars[entry_bar_idx + 1:]
            force_close_price = remaining_list[-1]["close"] if remaining_list else None

            result = evaluate_trade_stone(
                plan, remaining_list, force_close_price,
                trail_pct_75=config.TRAILING_STOP_PCT_75,
                trail_pct_1125=config.TRAILING_STOP_PCT_1125,
                trail_pct_150=config.TRAILING_STOP_PCT_150,
            )
            result.date = str(date_key)
            result.open_price = open_price

            all_trades.append(result)
            equity += result.pnl
            daily_trades += 1

            # Re-entry
            exit_bar_in_all = entry_bar_idx + 1 + result.exit_bar_idx
            bars_after_exit = all_bars[exit_bar_in_all + 1:]

            if result.exit_reason == "force_close" or not bars_after_exit:
                continue

            while daily_trades < config.MAX_DAILY_TRADES and not daily_stopped:
                if not bars_after_exit or len(bars_after_exit) < 3:
                    break

                reentry_price, prev_high, reentry_idx, reentry_confirmed = find_reentry_point(
                    bars_after_exit, open_price, initial_highest=result.trailing_high
                )

                if not reentry_confirmed or reentry_price <= 0:
                    break

                if prev_high > 0 and (prev_high - reentry_price) / prev_high > config.PULLBACK_STOP_THRESHOLD:
                    daily_stopped = True
                    break

                reentry_stop = round(reentry_price * (1 - config.REENTRY_STOP_PCT), 2)
                reentry_target = round(reentry_price + config.REENTRY_PROFIT_RETRACEMENT * (prev_high - reentry_price), 2)

                pos_size_re = min(calc_position_size(equity), config.MAX_POSITION_SIZE)
                reentry_shares = int(pos_size_re / reentry_price)
                if reentry_shares <= 0:
                    break

                reentry_remaining = bars_after_exit[reentry_idx + 1:]
                reentry_force_close = reentry_remaining[-1]["close"] if reentry_remaining else None

                reentry_result = evaluate_reentry_trade(
                    entry_price=reentry_price, prev_high=prev_high,
                    shares=reentry_shares, symbol=symbol, open_price=open_price,
                    bars_after_entry=reentry_remaining, force_close_price=reentry_force_close,
                )
                reentry_result.date = str(date_key)

                all_trades.append(reentry_result)
                equity += reentry_result.pnl
                daily_trades += 1

                reentry_exit_bar = reentry_idx + 1 + reentry_result.exit_bar_idx
                bars_after_exit = bars_after_exit[reentry_exit_bar + 1:]

                if reentry_result.exit_reason == "reentry_force_close":
                    break

    return all_trades, equity


def main():
    client = get_data_client()
    end_date = pd.Timestamp('2026-07-10', tz='America/New_York')
    n_days = 5

    trading_days = get_trading_days(client, end_date, n_days)
    if not trading_days:
        print("No trading days found.")
        return

    symbols = get_tradable_symbols()
    gap_data = bulk_scan_gaps(client, trading_days, symbols)

    # ── Run 0.4.10 ──
    print("=" * 70)
    print("Running 0.4.10 (old entry confirmation, no entry>=open check)...")
    print("=" * 70)
    trades_10, equity_10 = run_version(
        client, trading_days, gap_data,
        find_entry_fn=find_entry_old,
        skip_entry_above_open=False,
        version_label="0.4.10",
    )

    # ── Run 0.4.11 ──
    print("\n" + "=" * 70)
    print("Running 0.4.11 (running-min entry, skip entry>=open)...")
    print("=" * 70)
    trades_11, equity_11 = run_version(
        client, trading_days, gap_data,
        find_entry_fn=find_entry_new,
        skip_entry_above_open=True,
        version_label="0.4.11",
    )

    # ── Comparison ──
    print("\n" + "=" * 70)
    print("COMPARISON: 0.4.10 vs 0.4.11")
    print("=" * 70)

    def summarize(trades, equity, label):
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl < 0]
        total_pnl = sum(t.pnl for t in trades)
        avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.pnl for t in losses) / len(losses) if losses else 0
        return {
            "label": label,
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": f"{len(wins)/len(trades):.0%}" if trades else "—",
            "total_pnl": total_pnl,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "final_equity": equity,
        }

    s10 = summarize(trades_10, equity_10, "0.4.10")
    s11 = summarize(trades_11, equity_11, "0.4.11")

    print(f"\n{'Metric':<20} {'0.4.10':>15} {'0.4.11':>15} {'Delta':>15}")
    print("-" * 65)
    for key in ["trades", "wins", "losses", "win_rate"]:
        v10 = s10[key]
        v11 = s11[key]
        print(f"{key:<20} {str(v10):>15} {str(v11):>15}")
    for key in ["total_pnl", "avg_win", "avg_loss", "final_equity"]:
        v10 = s10[key]
        v11 = s11[key]
        delta = v11 - v10
        print(f"{key:<20} {v10:>+15,.2f} {v11:>+15,.2f} {delta:>+15,.2f}")

    # ── Per-trade comparison ──
    print(f"\n{'─' * 70}")
    print("Per-trade detail:")
    print(f"{'─' * 70}")

    # Build lookup by date+symbol for comparison
    def trade_key(t):
        tag = getattr(t, 'trade_type', '1st')
        return f"{t.date} {t.symbol} {tag}"

    t10_map = {}
    for t in trades_10:
        # Determine trade type from exit reason
        tag = "RE" if "reentry" in t.exit_reason else "1st"
        t10_map[f"{t.date} {t.symbol} {tag}"] = t

    t11_map = {}
    for t in trades_11:
        tag = "RE" if "reentry" in t.exit_reason else "1st"
        t11_map[f"{t.date} {t.symbol} {tag}"] = t

    all_keys = sorted(set(list(t10_map.keys()) + list(t11_map.keys())))

    print(f"\n{'Trade':<25} {'0.4.10 Entry':>14} {'0.4.10 P&L':>12} {'0.4.11 Entry':>14} {'0.4.11 P&L':>12} {'Delta P&L':>12}")
    print("-" * 95)

    for key in all_keys:
        t10 = t10_map.get(key)
        t11 = t11_map.get(key)

        e10 = f"${t10.entry_price:.4f}" if t10 else "—"
        p10 = f"${t10.pnl:+,.2f}" if t10 else "—"
        e11 = f"${t11.entry_price:.4f}" if t11 else "—"
        p11 = f"${t11.pnl:+,.2f}" if t11 else "—"

        if t10 and t11:
            delta = t11.pnl - t10.pnl
            d_str = f"${delta:+,.2f}"
        elif t10 and not t11:
            d_str = f"REMOVED (was ${t10.pnl:+,.2f})"
        else:
            d_str = "NEW"

        print(f"{key:<25} {e10:>14} {p10:>12} {e11:>14} {p11:>12} {d_str:>12}")

    print(f"\n{'='*70}")
    print(f"0.4.10 final equity: ${equity_10:,.2f} (P&L ${equity_10 - config.INITIAL_CAPITAL:+,.2f})")
    print(f"0.4.11 final equity: ${equity_11:,.2f} (P&L ${equity_11 - config.INITIAL_CAPITAL:+,.2f})")
    diff = equity_11 - equity_10
    print(f"Difference: ${diff:+,.2f} ({diff / (equity_10 - config.INITIAL_CAPITAL) * 100:+.1f}% vs 0.4.10)" if equity_10 != config.INITIAL_CAPITAL else "")


if __name__ == "__main__":
    main()
