"""Backtesting engine — Stone 0.2: multi-entry, short selling, gap tiering."""

import pandas as pd
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import Adjustment, DataFeed

import config
from scanner import get_data_client, get_tradable_symbols
from strategy import (
    build_trade_plan, evaluate_trade_stone, evaluate_short_trade,
    calc_atr, calc_stop_price, calc_price_at_retracement,
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
    """Bulk scan for gap UP and gap DOWN stocks."""
    start = trading_days[0] - pd.Timedelta(days=7)
    end = trading_days[-1] + pd.Timedelta(days=1)
    all_dates_set = {d.date() for d in trading_days}

    batch_size = 500
    long_data = {}   # symbol -> list of entries (gap up)
    short_data = {}  # symbol -> list of entries (gap down)

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
                    dollar_volume = prev_close * volume

                    entry = {
                        "date": curr_date,
                        "open_price": open_price,
                        "prev_close": prev_close,
                        "gap_pct": gap_pct,
                        "volume": volume,
                        "dollar_volume": dollar_volume,
                    }

                    # Long: gap up
                    if gap_pct >= config.GAP_THRESHOLD:
                        if volume < config.MIN_VOLUME:
                            continue
                        if not (config.PRICE_MIN <= open_price <= config.PRICE_MAX):
                            continue
                        if dollar_volume < config.MIN_DOLLAR_VOLUME:
                            continue
                        if symbol not in long_data:
                            long_data[symbol] = []
                        long_data[symbol].append(entry)

                    # Short: gap down
                    elif gap_pct <= -config.SHORT_GAP_THRESHOLD:
                        if volume < config.SHORT_MIN_VOLUME:
                            continue
                        if not (config.SHORT_PRICE_MIN <= open_price <= config.SHORT_PRICE_MAX):
                            continue
                        if dollar_volume < config.SHORT_MIN_DOLLAR_VOLUME:
                            continue
                        entry["direction"] = "short"
                        if symbol not in short_data:
                            short_data[symbol] = []
                        short_data[symbol].append(entry)

            except (KeyError, IndexError):
                continue

    # Organize by date
    results = {}
    for symbol, entries in long_data.items():
        for entry in entries:
            d = entry["date"]
            entry["symbol"] = symbol
            entry["direction"] = "long"
            if d not in results:
                results[d] = []
            results[d].append(entry)

    for symbol, entries in short_data.items():
        for entry in entries:
            d = entry["date"]
            entry["symbol"] = symbol
            if d not in results:
                results[d] = []
            results[d].append(entry)

    # Convert to DataFrames
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
    """Find first pullback entry with optional confirmation."""
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

    # Try later pullbacks
    for i in range(pullback_idx + 2, len(bars_5m)):
        bar = bars_5m.iloc[i]
        prev_bar = bars_5m.iloc[i - 1]
        if bar["low"] < open_price and prev_bar["low"] >= bar["low"]:
            if i + 1 < len(bars_5m) and bars_5m.iloc[i + 1]["low"] >= bar["low"]:
                return bar["low"], i, True

    return pullback_price, pullback_idx, True


def find_short_entry(bars_5m, open_price, prev_close):
    """Find short entry: wait for bounce then short on failure.

    Criteria for high-confidence short:
    1. Price bounces above open (dead cat bounce)
    2. Bounce retraces at least SHORT_BOUNCE_MIN_RETRACE of the gap
    3. Price drops back below open → short at open price
    """
    if bars_5m.empty or len(bars_5m) < 3:
        return 0, -1, False

    gap_size = prev_close - open_price  # positive for gap down
    if gap_size <= 0:
        return 0, -1, False

    min_bounce = open_price + gap_size * config.SHORT_BOUNCE_MIN_RETRACE

    # Find bounce high
    bounce_high = 0.0
    bounce_idx = -1
    for i in range(len(bars_5m)):
        bh = bars_5m.iloc[i]["high"]
        if bh > bounce_high:
            bounce_high = bh
            bounce_idx = i

    # Bounce must retrace at least 50% of the gap
    if bounce_high < min_bounce:
        return 0, -1, False

    # After bounce, find when price drops back below open
    for i in range(bounce_idx + 1, len(bars_5m)):
        if bars_5m.iloc[i]["low"] <= open_price:
            # Confirmation: next bar stays below open
            if i + 1 < len(bars_5m) and bars_5m.iloc[i + 1]["high"] <= open_price * 1.01:
                entry_price = bars_5m.iloc[i]["low"]
                return entry_price, i, True

    return 0, -1, False


def get_gap_tier_params(gap_pct):
    """Return (atr_mult, trail_75, trail_150, tier_label) based on gap size."""
    if gap_pct > config.GAP_TIER_3_THRESHOLD:
        return (config.GAP_TIER_3_ATR_MULT, config.GAP_TIER_3_TRAIL_75,
                config.GAP_TIER_3_TRAIL_150, "T3")
    elif gap_pct > config.GAP_TIER_2_THRESHOLD:
        return (config.GAP_TIER_2_ATR_MULT, config.GAP_TIER_2_TRAIL_75,
                config.GAP_TIER_2_TRAIL_150, "T2")
    else:
        return (config.GAP_TIER_1_ATR_MULT, config.GAP_TIER_1_TRAIL_75,
                config.GAP_TIER_1_TRAIL_150, "T1")


def run_backtest(end_date=None, n_days=config.BACKTEST_DAYS, strategy="stone") -> list[TradeResult]:
    client = get_data_client()

    if end_date is None:
        end_date = pd.Timestamp.now(tz="America/New_York")

    trading_days = get_trading_days(client, end_date, n_days)
    if not trading_days:
        print("No trading days found.")
        return []

    print(f"[Stone 0.2] Backtesting {len(trading_days)} trading days: {trading_days[0].date()} to {trading_days[-1].date()}")
    print(f"Capital: ${config.INITIAL_CAPITAL:,.0f} | Position: ${config.POSITION_SIZE:,.0f} | "
          f"Max stocks: {config.MAX_POSITIONS_PER_DAY} | Max entries/stock: {config.MAX_ENTRIES_PER_STOCK}")
    print(f"Gap threshold: {config.GAP_THRESHOLD:.0%} | Short gap: {config.SHORT_GAP_THRESHOLD:.0%} | "
          f"Short stop: {config.SHORT_STOP_PCT:.0%} | Short target: {config.SHORT_TARGET_PCT:.0%}")

    print("\nLoading tradable symbols...")
    symbols = get_tradable_symbols()
    print(f"Found {len(symbols)} tradable symbols")

    print("\nBulk scanning for gaps (long + short)...")
    gap_data = bulk_scan_gaps(client, trading_days, symbols)

    long_count = sum(len(v[v["direction"] == "long"]) for v in gap_data.values() if "direction" in v.columns)
    short_count = sum(len(v[v["direction"] == "short"]) for v in gap_data.values() if "direction" in v.columns)
    print(f"Found {long_count} long + {short_count} short gap entries across {len(gap_data)} days")

    all_trades: list[TradeResult] = []
    equity = config.INITIAL_CAPITAL

    for date in trading_days:
        date_key = date.date()
        if date_key not in gap_data or gap_data[date_key].empty:
            continue

        day_df = gap_data[date_key]
        long_cands = day_df[day_df["direction"] == "long"].head(config.MAX_POSITIONS_PER_DAY)
        short_cands = day_df[day_df["direction"] == "short"]

        print(f"\n--- {date_key} (long: {len(day_df[day_df['direction']=='long'])}, "
              f"short: {len(day_df[day_df['direction']=='short'])}) ---")

        positions_today = 0

        # ── Long trades (with multi-entry) ──
        for _, row in long_cands.iterrows():
            if positions_today >= config.MAX_POSITIONS_PER_DAY:
                break

            symbol = row["symbol"]
            open_price = row["open_price"]
            gap_pct = row["gap_pct"]

            bars_5m = get_5min_bars(client, symbol, date)
            if bars_5m.empty or len(bars_5m) < 3:
                continue

            pullback, entry_bar_idx, confirmed = find_entry_with_confirmation(bars_5m, open_price)
            if not confirmed or pullback <= 0:
                print(f"  {symbol}: no confirmed entry, skipping")
                continue

            # Gap tiering
            atr_mult, trail_75, trail_150, tier_label = get_gap_tier_params(gap_pct)

            bars_for_atr = []
            for j in range(min(entry_bar_idx + 1, len(bars_5m))):
                b = bars_5m.iloc[j]
                bars_for_atr.append({"high": b["high"], "low": b["low"], "close": b["close"]})
            atr = calc_atr(bars_for_atr, period=14)

            stop_price = calc_stop_price(pullback, atr, atr_mult=atr_mult)
            target_75 = calc_price_at_retracement(pullback, open_price, config.PROFIT_RETRACEMENT_75)
            target_150 = calc_price_at_retracement(pullback, open_price, config.PROFIT_RETRACEMENT_150)

            first_pullback = pullback  # remember for re-entries

            # ── Multiple entries per stock ──
            scan_start = entry_bar_idx + 1
            for entry_num in range(1, config.MAX_ENTRIES_PER_STOCK + 1):
                if positions_today >= config.MAX_POSITIONS_PER_DAY:
                    break

                if entry_num == 1:
                    entry_price = pullback
                else:
                    # Re-entry: find bar where low is near first pullback
                    found_reentry = False
                    for k in range(scan_start, len(bars_5m)):
                        bar_low = bars_5m.iloc[k]["low"]
                        if bar_low <= first_pullback * (1 + config.REENTRY_PRICE_TOLERANCE):
                            # Check if previous bar was above (pullback pattern)
                            if k > 0 and bars_5m.iloc[k-1]["low"] > bar_low * (1 - 0.01):
                                entry_price = bar_low
                                entry_bar_idx = k
                                found_reentry = True
                                break
                    if not found_reentry:
                        break

                    # Recalculate ATR from recent bars
                    bars_for_atr2 = []
                    for j in range(max(0, entry_bar_idx - 13), min(entry_bar_idx + 1, len(bars_5m))):
                        b = bars_5m.iloc[j]
                        bars_for_atr2.append({"high": b["high"], "low": b["low"], "close": b["close"]})
                    atr = calc_atr(bars_for_atr2, period=14)
                    stop_price = calc_stop_price(entry_price, atr, atr_mult=atr_mult)
                    target_75 = calc_price_at_retracement(entry_price, open_price, config.PROFIT_RETRACEMENT_75)
                    target_150 = calc_price_at_retracement(entry_price, open_price, config.PROFIT_RETRACEMENT_150)

                shares = int(config.POSITION_SIZE / entry_price)
                if shares <= 0:
                    break

                plan = TradePlan(
                    symbol=symbol, open_price=open_price, pullback=entry_price,
                    target_75=target_75, target_150=target_150,
                    stop_price=stop_price, shares=shares, atr=atr,
                )

                cost = shares * entry_price
                entry_label = f"#{entry_num}" if entry_num > 1 else ""
                print(f"  {symbol} [{tier_label}]{entry_label}: entry=${entry_price:.4f}, "
                      f"stop=${stop_price:.4f} ({(1-stop_price/entry_price):.1%}), "
                      f"shares={shares:,}, cost=${cost:,.0f}")

                # Build remaining bars list
                remaining_bars = bars_5m.iloc[entry_bar_idx + 1:]
                remaining_list = []
                for idx, bar in remaining_bars.iterrows():
                    remaining_list.append({
                        "high": bar["high"], "low": bar["low"],
                        "close": bar["close"],
                        "timestamp": idx if isinstance(idx, pd.Timestamp) else date,
                    })

                force_close_price = remaining_list[-1]["close"] if remaining_list else None

                result = evaluate_trade_stone(
                    plan, remaining_list, force_close_price,
                    trail_pct_75=trail_75, trail_pct_150=trail_150,
                )
                result.date = str(date_key)
                result.open_price = open_price
                result.sell_target = plan.target_150
                result.stop_price = plan.stop_price
                result.entry_index = entry_num

                extra = ""
                if result.partial_sell_shares > 0:
                    extra = f", partial={result.partial_sell_shares:,}sh@${result.partial_sell_price:.4f}"
                if result.trailing_high > result.entry_price:
                    extra += f", high=${result.trailing_high:.4f}"
                print(f"    Result: exit=${result.exit_price:.4f} ({result.exit_reason}), "
                      f"P&L=${result.pnl:,.2f} ({result.pnl_pct:.2%}){extra}")

                all_trades.append(result)
                equity += result.pnl
                positions_today += 1

                # Update scan_start for next entry
                if result.exit_bar_idx >= 0:
                    scan_start = entry_bar_idx + 1 + result.exit_bar_idx + 1
                else:
                    break  # no exit found, stop re-entries

        # ── Short trades ──
        for _, row in short_cands.iterrows():
            if positions_today >= config.MAX_POSITIONS_PER_DAY:
                break

            symbol = row["symbol"]
            open_price = row["open_price"]
            prev_close = row["prev_close"]

            bars_5m = get_5min_bars(client, symbol, date)
            if bars_5m.empty or len(bars_5m) < 3:
                continue

            short_entry, short_bar_idx, short_confirmed = find_short_entry(bars_5m, open_price, prev_close)
            if not short_confirmed or short_entry <= 0:
                continue

            shares = int(config.POSITION_SIZE / short_entry)
            if shares <= 0:
                continue

            # ATR for the short
            bars_for_atr = []
            for j in range(min(short_bar_idx + 1, len(bars_5m))):
                b = bars_5m.iloc[j]
                bars_for_atr.append({"high": b["high"], "low": b["low"], "close": b["close"]})
            atr = calc_atr(bars_for_atr, period=14)

            remaining_bars = bars_5m.iloc[short_bar_idx + 1:]
            remaining_list = []
            for idx, bar in remaining_bars.iterrows():
                remaining_list.append({
                    "high": bar["high"], "low": bar["low"],
                    "close": bar["close"],
                    "timestamp": idx if isinstance(idx, pd.Timestamp) else date,
                })

            force_close_price = remaining_list[-1]["close"] if remaining_list else None

            result = evaluate_short_trade(
                symbol, short_entry, open_price, shares, atr,
                remaining_list, force_close_price,
            )
            result.date = str(date_key)

            print(f"  {symbol} [SHORT]: entry=${short_entry:.4f}, "
                  f"stop=${result.stop_price:.4f}, target=${result.sell_target:.4f}, "
                  f"shares={shares:,}")
            print(f"    Result: exit=${result.exit_price:.4f} ({result.exit_reason}), "
                  f"P&L=${result.pnl:,.2f} ({result.pnl_pct:.2%})")

            all_trades.append(result)
            equity += result.pnl
            positions_today += 1

    print(f"\n{'='*60}")
    print(f"[Stone 0.2] Backtest complete. Final equity: ${equity:,.2f}")
    print(f"Total trades: {len(all_trades)}")
    return all_trades
