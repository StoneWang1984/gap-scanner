"""Backtesting engine — Stone 0.4: three-tier first trade + re-entry + equity compounding."""

import pandas as pd
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import Adjustment, DataFeed

import config
from scanner import get_data_client, get_tradable_symbols
from strategy import (
    build_trade_plan, evaluate_trade_stone, evaluate_reentry_trade,
    calc_atr, calc_stop_price, calc_price_at_retracement, calc_position_size,
    find_reentry_point,
    TradeResult, TradePlan,
)


def get_trading_days(client: StockHistoricalDataClient, end_date: pd.Timestamp, n_days: int) -> list[pd.Timestamp]:
    start = end_date - pd.Timedelta(days=n_days * 2 + 10)
    request = StockBarsRequest(
        symbol_or_symbols="SPY", timeframe=TimeFrame.Day,
        start=start, end=end_date, adjustment=Adjustment.RAW, feed=DataFeed.IEX,
    )
    bars = client.get_stock_bars(request)
    if bars.df.empty:
        return []
    df = bars.df
    dates = sorted(set(df.index.get_level_values("timestamp").date))
    return [pd.Timestamp(d) for d in dates[-n_days:]]


def bulk_scan_gaps(
    client: StockHistoricalDataClient,
    trading_days: list[pd.Timestamp],
    symbols: list[str],
) -> dict:
    start = trading_days[0] - pd.Timedelta(days=7)
    end = trading_days[-1] + pd.Timedelta(days=1)
    all_dates_set = {d.date() for d in trading_days}

    batch_size = 500
    symbol_data = {}

    total_batches = (len(symbols) + batch_size - 1) // batch_size
    for batch_idx in range(total_batches):
        batch = symbols[batch_idx * batch_size : (batch_idx + 1) * batch_size]
        if batch_idx % 10 == 0:
            print(f"  Bulk scanning batch {batch_idx + 1}/{total_batches}...")

        request = StockBarsRequest(
            symbol_or_symbols=batch, timeframe=TimeFrame.Day,
            start=start, end=end, adjustment=Adjustment.RAW, feed=DataFeed.IEX,
        )
        try:
            bars = client.get_stock_bars(request)
        except Exception as e:
            print(f"  API error: {e}")
            continue

        if bars.df.empty:
            continue
        df = bars.df

        for symbol in batch:
            try:
                sym_df = df[df.index.get_level_values("symbol") == symbol].sort_index()
                if len(sym_df) < 2:
                    continue
                for i in range(1, len(sym_df)):
                    curr = sym_df.iloc[i]
                    prev = sym_df.iloc[i - 1]
                    idx_val = sym_df.index[i]
                    if isinstance(idx_val, tuple):
                        ts = idx_val[1] if hasattr(idx_val[1], 'date') else pd.Timestamp(idx_val[1])
                    else:
                        ts = pd.Timestamp(idx_val) if not hasattr(idx_val, 'date') else idx_val
                    curr_date = ts.date()
                    if curr_date not in all_dates_set:
                        continue
                    prev_close = prev["close"]
                    open_price = curr["open"]
                    volume = prev["volume"]
                    if prev_close <= 0:
                        continue
                    gap_pct = (open_price / prev_close) - 1.0
                    if gap_pct < config.GAP_THRESHOLD:
                        continue
                    if volume < config.MIN_VOLUME:
                        continue
                    if not (config.PRICE_MIN <= open_price <= config.PRICE_MAX):
                        continue
                    dollar_volume = prev_close * volume
                    if dollar_volume < config.MIN_DOLLAR_VOLUME:
                        continue
                    if symbol not in symbol_data:
                        symbol_data[symbol] = []
                    symbol_data[symbol].append({
                        "date": curr_date, "open_price": open_price,
                        "prev_close": prev_close, "gap_pct": gap_pct,
                        "volume": volume, "dollar_volume": dollar_volume,
                    })
            except (KeyError, IndexError):
                continue

    results = {}
    for symbol, entries in symbol_data.items():
        for entry in entries:
            d = entry["date"]
            if d not in results:
                results[d] = []
            results[d].append({**entry, "symbol": symbol})

    for d in results:
        results[d] = pd.DataFrame(results[d]).sort_values("gap_pct", ascending=False)

    return results


def get_5min_bars(client, symbol, date) -> pd.DataFrame:
    market_open = pd.Timestamp(f"{date.date()} {config.MARKET_OPEN}", tz="America/New_York")
    market_close = pd.Timestamp(f"{date.date()} {config.MARKET_CLOSE}", tz="America/New_York")
    request = StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=market_open, end=market_close, adjustment=Adjustment.RAW, feed=DataFeed.IEX,
    )
    bars = client.get_stock_bars(request)
    if bars.df.empty:
        return pd.DataFrame()
    return bars.df


def find_entry_with_confirmation(bars_5m, open_price):
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
    next_bar = bars_5m.iloc[pullback_idx + 1]
    if next_bar["low"] >= pullback_price:
        return pullback_price, pullback_idx, True
    for i in range(pullback_idx + 2, len(bars_5m)):
        bar = bars_5m.iloc[i]
        prev_bar = bars_5m.iloc[i - 1]
        if bar["low"] < open_price and prev_bar["low"] >= bar["low"]:
            if i + 1 < len(bars_5m) and bars_5m.iloc[i + 1]["low"] >= bar["low"]:
                return bar["low"], i, True
    return pullback_price, pullback_idx, True


def _bars_to_list(bars_df, start_idx=0):
    """Convert DataFrame rows to list of dicts starting from start_idx."""
    result = []
    for i in range(start_idx, len(bars_df)):
        bar = bars_df.iloc[i]
        idx = bars_df.index[i]
        ts = idx
        if isinstance(idx, tuple):
            ts = idx[1]
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize('UTC').tz_convert('America/New_York')
        result.append({
            "high": bar["high"], "low": bar["low"], "close": bar["close"],
            "open": bar["open"], "volume": int(bar["volume"]) if "volume" in bar.index else 0,
            "timestamp": ts,
        })
    return result


def run_backtest(end_date=None, n_days=config.BACKTEST_DAYS) -> list[TradeResult]:
    client = get_data_client()
    if end_date is None:
        end_date = pd.Timestamp.now(tz="America/New_York")

    trading_days = get_trading_days(client, end_date, n_days)
    if not trading_days:
        print("No trading days found.")
        return []

    print(f"[Stone 0.4] Backtesting {len(trading_days)} trading days: {trading_days[0].date()} to {trading_days[-1].date()}")
    print(f"Capital: ${config.INITIAL_CAPITAL:,.0f} | Deploy: {config.EQUITY_POSITION_RATIO:.0%} | "
          f"Per-stock cap: ${config.MAX_POSITION_SIZE:,.0f} | Max daily trades: {config.MAX_DAILY_TRADES}")
    print(f"First trade: 1/4@75% + 1/3@112.5% + 1/3@150% | Trail: {config.TRAILING_STOP_PCT_75:.0%}/{config.TRAILING_STOP_PCT_1125:.0%}/{config.TRAILING_STOP_PCT_150:.0%}")
    print(f"Re-entry: {config.REENTRY_STOP_PCT:.0%} stop | 1/3@150% target | {config.REENTRY_TRAILING_PCT:.0%} trail | "
          f"Pullback stop: {config.PULLBACK_STOP_THRESHOLD:.0%}")

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

        print(f"\n--- {date_key} ({len(gap_data[date_key])} candidates, equity: ${equity:,.0f}, "
              f"deploy: ${deployable:,.0f}, per-stock: ${pos_per_stock:,.0f}) ---")

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

            # Remaining bars after entry
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
            result.sell_target = plan.target_150
            result.stop_price = plan.stop_price

            type_tag = "[1st]"
            extra = ""
            if result.partial_sell_shares > 0:
                extra += f", 1/4@${result.partial_sell_price:.4f}"
            if result.partial2_sell_shares > 0:
                extra += f", 1/3@${result.partial2_sell_price:.4f}"
            if result.partial3_sell_shares > 0:
                extra += f", 1/3@${result.partial3_sell_price:.4f}"
            if result.trailing_high > result.entry_price:
                extra += f", high=${result.trailing_high:.4f}"
            print(f"  {symbol} {type_tag} entry=${pullback:.4f} exit=${result.exit_price:.4f} ({result.exit_reason}), "
                  f"P&L=${result.pnl:,.2f} ({result.pnl_pct:.2%}){extra}")

            all_trades.append(result)
            equity += result.pnl
            daily_trades += 1

            # ========== RE-ENTRY TRADES ==========
            # Find bars after first trade's exit
            exit_bar_in_all = entry_bar_idx + 1 + result.exit_bar_idx
            bars_after_exit = all_bars[exit_bar_in_all + 1:]

            if result.exit_reason == "force_close" or not bars_after_exit:
                continue

            reentry_round = 1
            while daily_trades < config.MAX_DAILY_TRADES and not daily_stopped:
                if not bars_after_exit or len(bars_after_exit) < 3:
                    break

                reentry_price, prev_high, reentry_idx, reentry_confirmed = find_reentry_point(
                    bars_after_exit, open_price
                )

                if not reentry_confirmed or reentry_price <= 0:
                    break

                # Check if significant pullback occurred
                if prev_high > 0 and (prev_high - reentry_price) / prev_high > config.PULLBACK_STOP_THRESHOLD:
                    print(f"  {symbol}: significant pullback from ${prev_high:.4f}, stopping day")
                    daily_stopped = True
                    break

                # Re-entry trade
                reentry_stop = round(reentry_price * (1 - config.REENTRY_STOP_PCT), 4)
                reentry_target = round(reentry_price + config.REENTRY_PROFIT_RETRACEMENT * (prev_high - reentry_price), 4)

                pos_size_re = min(calc_position_size(equity), config.MAX_POSITION_SIZE)
                reentry_shares = int(pos_size_re / reentry_price)
                if reentry_shares <= 0:
                    break

                # Remaining bars after re-entry
                reentry_remaining = bars_after_exit[reentry_idx + 1:]
                reentry_force_close = reentry_remaining[-1]["close"] if reentry_remaining else None

                reentry_result = evaluate_reentry_trade(
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

                # Prepare for next re-entry
                reentry_exit_bar = reentry_idx + 1 + reentry_result.exit_bar_idx
                bars_after_exit = bars_after_exit[reentry_exit_bar + 1:]

                if reentry_result.exit_reason == "reentry_force_close":
                    break

    print(f"\n{'='*60}")
    print(f"[Stone 0.4] Backtest complete. Final equity: ${equity:,.2f}")
    print(f"Total trades: {len(all_trades)}")
    return all_trades
