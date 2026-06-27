"""Backtesting engine — Stone 0.4.2: five optimizations over 0.4."""

import pandas as pd
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import Adjustment, DataFeed

import config
from scanner import get_data_client, get_tradable_symbols
from strategy import (
    calc_atr, calc_position_size, calc_price_at_retracement,
    TradePlan, TradeResult,
)
from backtest import (
    get_trading_days, bulk_scan_gaps, get_5min_bars,
    find_entry_with_confirmation, _bars_to_list,
)
from strategy_042 import (
    calc_stop_price_042, check_early_stop,
    evaluate_trade_stone_042,
    find_reentry_point_042, evaluate_reentry_trade_042,
    check_volume_ratio, check_prior_gain,
)


def run_backtest_042(end_date=None, n_days=config.BACKTEST_DAYS) -> list[TradeResult]:
    client = get_data_client()
    if end_date is None:
        end_date = pd.Timestamp.now(tz="America/New_York")

    trading_days = get_trading_days(client, end_date, n_days)
    if not trading_days:
        print("No trading days found.")
        return []

    print(f"[Stone 0.4.2] Backtesting {len(trading_days)} trading days: "
          f"{trading_days[0].date()} to {trading_days[-1].date()}")
    print(f"Capital: ${config.INITIAL_CAPITAL:,.0f} | Deploy: {config.EQUITY_POSITION_RATIO:.0%} | "
          f"Max daily trades: {config.MAX_DAILY_TRADES}")
    print(f"First trade: 1/3@75% + 1/3@100% + 1/3@150% | Trail: "
          f"{config.TRAILING_STOP_PCT_75:.0%}/{config.TRAILING_STOP_PCT_1125:.0%}/{config.TRAILING_STOP_PCT_150:.0%}"
          f" (late x{config.TRAILING_LATE_FACTOR})")
    print(f"Stop: 5% fixed for <${config.LOW_PRICE_THRESHOLD} | Early stop: {config.EARLY_STOP_BARS} bars | "
          f"Vol ratio: {config.VOLUME_RATIO_MIN}x")
    print(f"Late close: 14:30 if <75% | Re-entry pullback: {config.REENTRY_MIN_PULLBACK_042:.0%} | "
          f"Re-entry early exit: {config.REENTRY_EARLY_EXIT_BARS} bars")

    print("\nLoading tradable symbols...")
    symbols = get_tradable_symbols()
    print(f"Found {len(symbols)} tradable symbols")

    print("\nBulk scanning for gaps...")
    gap_data = bulk_scan_gaps(client, trading_days, symbols)
    total_candidates = sum(len(v) for v in gap_data.values())
    print(f"Found {total_candidates} gap entries across {len(gap_data)} days")

    all_trades: list[TradeResult] = []
    equity = config.INITIAL_CAPITAL

    for date in trading_days:
        date_key = date.date()
        if date_key not in gap_data or gap_data[date_key].empty:
            continue

        deployable = calc_position_size(equity)
        pos_per_stock = min(deployable, config.MAX_POSITION_SIZE)
        max_stocks_today = max(config.MAX_POSITIONS_PER_DAY, int(deployable / pos_per_stock))
        candidates = gap_data[date_key].head(max_stocks_today)

        print(f"\n--- {date_key} ({len(gap_data[date_key])} candidates, "
              f"equity: ${equity:,.0f}, deploy: ${deployable:,.0f}) ---")

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

            # ========== FIRST TRADE ==========
            pullback, entry_bar_idx, confirmed = find_entry_with_confirmation(bars_5m, open_price)
            if not confirmed or pullback <= 0:
                print(f"  {symbol}: no confirmed entry, skipping")
                continue

            # Entry time check
            idx_val = bars_5m.index[entry_bar_idx]
            if isinstance(idx_val, tuple):
                entry_ts = pd.Timestamp(idx_val[1])
            else:
                entry_ts = pd.Timestamp(idx_val)
            if entry_ts.tzinfo is None:
                entry_ts = entry_ts.tz_localize('UTC').tz_convert('America/New_York')
            cutoff = pd.Timestamp(f"{date_key} 10:00", tz="America/New_York")
            if entry_ts > cutoff:
                print(f"  {symbol}: entry after 10:00, skipping")
                continue

            # ATR
            bars_for_atr = []
            for j in range(min(entry_bar_idx + 1, len(bars_5m))):
                b = bars_5m.iloc[j]
                bars_for_atr.append({"high": b["high"], "low": b["low"], "close": b["close"]})
            atr = calc_atr(bars_for_atr, period=14)

            # ── 1. Stop price: low-price optimization ──
            stop_price = calc_stop_price_042(pullback, atr, open_price)

            target_75 = calc_price_at_retracement(pullback, open_price, config.PROFIT_RETRACEMENT_75)
            target_150 = calc_price_at_retracement(pullback, open_price, config.PROFIT_RETRACEMENT_150)

            pos_size = min(calc_position_size(equity), config.MAX_POSITION_SIZE)
            shares = int(pos_size / pullback)
            if shares <= 0:
                continue

            plan = TradePlan(
                symbol=symbol, open_price=open_price, pullback=pullback,
                target_75=target_75, target_1125=target_75,  # placeholder, 042 uses 100%
                target_150=target_150, stop_price=stop_price, shares=shares, atr=atr,
            )

            # Remaining bars after entry
            remaining_list = all_bars[entry_bar_idx + 1:]
            force_close_price = remaining_list[-1]["close"] if remaining_list else None

            result = evaluate_trade_stone_042(
                plan, remaining_list, force_close_price,
                entry_bar_idx=entry_bar_idx,
            )

            # ── 1b. Early stop check (overrides if triggered) ──
            early_triggered, early_price, early_bi = check_early_stop(remaining_list, pullback)
            if early_triggered:
                pnl = (early_price - pullback) * shares
                pnl_pct = pnl / (pullback * shares) if pullback > 0 else 0
                result = TradeResult(
                    symbol=symbol, date=str(date_key),
                    entry_price=pullback, exit_price=early_price, shares=shares,
                    pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4),
                    exit_reason="early_stop",
                    open_price=open_price, sell_target=plan.target_150,
                    stop_price=stop_price,
                    trailing_high=max(b["high"] for b in remaining_list[:config.EARLY_STOP_BARS]),
                    exit_bar_idx=early_bi, position_size=pullback * shares,
                    trade_type="first",
                )

            result.date = str(date_key)
            result.open_price = open_price

            type_tag = "[1st]"
            extra = ""
            if result.partial_sell_shares > 0:
                extra += f", 1/3@${result.partial_sell_price:.4f}"
            if result.partial2_sell_shares > 0:
                extra += f", 1/3@${result.partial2_sell_price:.4f}"
            if result.partial3_sell_shares > 0:
                extra += f", 1/3@${result.partial3_sell_price:.4f}"
            if result.trailing_high > result.entry_price:
                extra += f", high=${result.trailing_high:.4f}"
            print(f"  {symbol} {type_tag} entry=${pullback:.4f} exit=${result.exit_price:.4f} "
                  f"({result.exit_reason}), P&L=${result.pnl:,.2f} ({result.pnl_pct:.2%}){extra}")

            all_trades.append(result)
            equity += result.pnl
            daily_trades += 1

            # ========== RE-ENTRY TRADES ==========
            exit_bar_in_all = entry_bar_idx + 1 + result.exit_bar_idx
            bars_after_exit = all_bars[exit_bar_in_all + 1:]

            if result.exit_reason == "force_close" or not bars_after_exit:
                continue
            if result.exit_reason == "late_close":
                continue

            reentry_round = 1
            while daily_trades < config.MAX_DAILY_TRADES and not daily_stopped:
                if not bars_after_exit or len(bars_after_exit) < 3:
                    break

                # ── 2. Re-entry with deeper pullback requirement ──
                reentry_price, prev_high, reentry_idx, reentry_confirmed = find_reentry_point_042(
                    bars_after_exit, open_price, initial_highest=result.trailing_high
                )

                if not reentry_confirmed or reentry_price <= 0:
                    break

                if prev_high > 0 and (prev_high - reentry_price) / prev_high > config.PULLBACK_STOP_THRESHOLD:
                    print(f"  {symbol}: significant pullback from ${prev_high:.4f}, stopping day")
                    daily_stopped = True
                    break

                reentry_stop = round(reentry_price * (1 - config.REENTRY_STOP_PCT), 4)
                reentry_target = round(reentry_price + config.REENTRY_PROFIT_RETRACEMENT * (prev_high - reentry_price), 4)

                pos_size_re = min(calc_position_size(equity), config.MAX_POSITION_SIZE)
                reentry_shares = int(pos_size_re / reentry_price)
                if reentry_shares <= 0:
                    break

                reentry_remaining = bars_after_exit[reentry_idx + 1:]
                reentry_force_close = reentry_remaining[-1]["close"] if reentry_remaining else None

                # ── 2b. Re-entry with early exit check ──
                reentry_result = evaluate_reentry_trade_042(
                    entry_price=reentry_price,
                    prev_high=prev_high,
                    shares=reentry_shares,
                    symbol=symbol,
                    open_price=open_price,
                    bars_after_entry=reentry_remaining,
                    force_close_price=reentry_force_close,
                )
                reentry_result.date = str(date_key)

                type_tag = f"[Re{reentry_round}]"
                re_extra = ""
                if reentry_result.partial_sell_shares > 0:
                    re_extra += f", 1/3@${reentry_result.partial_sell_price:.4f}"
                if reentry_result.trailing_high > reentry_result.entry_price:
                    re_extra += f", high=${reentry_result.trailing_high:.4f}"
                print(f"  {symbol} {type_tag} entry=${reentry_price:.4f} exit=${reentry_result.exit_price:.4f} "
                      f"({reentry_result.exit_reason}), P&L=${reentry_result.pnl:,.2f} ({reentry_result.pnl_pct:.2%})"
                      f", prev_high=${prev_high:.4f}, target=${reentry_target:.4f}{re_extra}")

                all_trades.append(reentry_result)
                equity += reentry_result.pnl
                daily_trades += 1
                reentry_round += 1

                reentry_exit_bar = reentry_idx + 1 + reentry_result.exit_bar_idx
                bars_after_exit = bars_after_exit[reentry_exit_bar + 1:]

                if reentry_result.exit_reason == "reentry_force_close":
                    break

    print(f"\n{'='*60}")
    print(f"[Stone 0.4.2] Backtest complete. Final equity: ${equity:,.2f}")
    print(f"Total trades: {len(all_trades)}")
    return all_trades
