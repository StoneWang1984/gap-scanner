"""Backtesting engine — optimized: bulk scan + per-day 5min fetch."""

import pandas as pd
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import Adjustment, DataFeed

import config
from scanner import get_data_client, get_tradable_symbols
from strategy import build_trade_plan, evaluate_trade, evaluate_trade_stone, calc_atr, calc_stop_price, calc_price_at_retracement, TradeResult, TradePlan


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
    """Bulk scan for gaps across ALL trading days at once.

    Fetches daily bars for all symbols over the entire backtest period in one
    batch, then identifies gaps per day. Returns dict: date -> DataFrame of candidates.
    """
    start = trading_days[0] - pd.Timedelta(days=7)
    end = trading_days[-1] + pd.Timedelta(days=1)

    all_dates_set = {d.date() for d in trading_days}

    # Fetch in batches of 500 symbols
    batch_size = 500
    symbol_data = {}  # symbol -> list of {date, open, prev_close, volume, dollar_volume}

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
                        "date": curr_date,
                        "open_price": open_price,
                        "prev_close": prev_close,
                        "gap_pct": gap_pct,
                        "volume": volume,
                        "dollar_volume": dollar_volume,
                    })
            except (KeyError, IndexError):
                continue

    # Organize by date
    results = {}
    for symbol, entries in symbol_data.items():
        for entry in entries:
            d = entry["date"]
            if d not in results:
                results[d] = []
            results[d].append({**entry, "symbol": symbol})

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

    # Confirmation: next bar holds above pullback low
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


def run_backtest(end_date=None, n_days=config.BACKTEST_DAYS, strategy="stone") -> list[TradeResult]:
    client = get_data_client()

    if end_date is None:
        end_date = pd.Timestamp.now(tz="America/New_York")

    trading_days = get_trading_days(client, end_date, n_days)
    if not trading_days:
        print("No trading days found.")
        return []

    strategy_label = "Stone" if strategy == "stone" else "Base"
    print(f"[{strategy_label}] Backtesting {len(trading_days)} trading days: {trading_days[0].date()} to {trading_days[-1].date()}")
    print(f"Capital: ${config.POSITION_SIZE:,.0f} | Max positions: {config.MAX_POSITIONS_PER_DAY} | "
          f"Min $vol: ${config.MIN_DOLLAR_VOLUME:,.0f}")
    if strategy == "stone":
        print(f"Stone策略: 入场截止{config.ENTRY_TIME_CUTOFF} | "
              f"跳空分级: <{config.GAP_TIER_2_THRESHOLD:.0%}/{config.GAP_TIER_3_THRESHOLD:.0%} | "
              f"ATR: {config.GAP_TIER_1_ATR_MULT}/{config.GAP_TIER_2_ATR_MULT}/{config.GAP_TIER_3_ATR_MULT}")

    print("\nLoading tradable symbols...")
    symbols = get_tradable_symbols()
    print(f"Found {len(symbols)} tradable symbols")

    print("\nBulk scanning for gaps across all days...")
    gap_data = bulk_scan_gaps(client, trading_days, symbols)
    total_candidates = sum(len(v) for v in gap_data.values())
    print(f"Found {total_candidates} gap entries across {len(gap_data)} days")

    all_trades: list[TradeResult] = []
    equity = config.INITIAL_CAPITAL

    for date in trading_days:
        date_key = date.date()
        if date_key not in gap_data or gap_data[date_key].empty:
            continue

        candidates = gap_data[date_key].head(config.MAX_POSITIONS_PER_DAY)
        print(f"\n--- {date_key} ({len(gap_data[date_key])} candidates, taking top {len(candidates)}) ---")

        positions_today = 0

        for _, row in candidates.iterrows():
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

            # Stone: entry time window check
            if strategy == "stone" and entry_bar_idx < len(bars_5m):
                entry_idx = bars_5m.index[entry_bar_idx]
                if isinstance(entry_idx, tuple):
                    entry_ts = pd.Timestamp(entry_idx[1])
                elif isinstance(entry_idx, pd.Timestamp):
                    entry_ts = entry_idx
                else:
                    entry_ts = None
                if entry_ts is not None:
                    cutoff = pd.Timestamp(f"{date_key} {config.ENTRY_TIME_CUTOFF}", tz="America/New_York")
                    if entry_ts > cutoff:
                        print(f"  {symbol}: entry at {entry_ts.strftime('%H:%M')} after cutoff, skipping")
                        continue

            bars_for_atr = []
            for j in range(min(entry_bar_idx + 1, len(bars_5m))):
                b = bars_5m.iloc[j]
                bars_for_atr.append({"high": b["high"], "low": b["low"], "close": b["close"]})
            atr = calc_atr(bars_for_atr, period=14)

            shares = int(config.POSITION_SIZE / pullback)
            if shares <= 0:
                continue

            # Stone: gap tiering
            if strategy == "stone":
                if gap_pct > config.GAP_TIER_3_THRESHOLD:
                    atr_mult = config.GAP_TIER_3_ATR_MULT
                    trail_75 = config.GAP_TIER_3_TRAIL_75
                    trail_150 = config.GAP_TIER_3_TRAIL_150
                    tier_label = "T3"
                elif gap_pct > config.GAP_TIER_2_THRESHOLD:
                    atr_mult = config.GAP_TIER_2_ATR_MULT
                    trail_75 = config.GAP_TIER_2_TRAIL_75
                    trail_150 = config.GAP_TIER_2_TRAIL_150
                    tier_label = "T2"
                else:
                    atr_mult = config.GAP_TIER_1_ATR_MULT
                    trail_75 = config.GAP_TIER_1_TRAIL_75
                    trail_150 = config.GAP_TIER_1_TRAIL_150
                    tier_label = "T1"

                stop_price = calc_stop_price(pullback, atr, atr_mult=atr_mult)
                target_75 = calc_price_at_retracement(pullback, open_price, config.PROFIT_RETRACEMENT_75)
                target_150 = calc_price_at_retracement(pullback, open_price, config.PROFIT_RETRACEMENT_150)
                plan = TradePlan(
                    symbol=symbol, open_price=open_price, pullback=pullback,
                    target_75=target_75, target_150=target_150,
                    stop_price=stop_price, shares=shares, atr=atr,
                )
            else:
                plan = build_trade_plan(symbol, open_price, pullback, atr)
                plan.shares = shares

            cost = shares * pullback
            tier_str = f" [{tier_label}]" if strategy == "stone" else ""
            print(f"  {symbol}{tier_str}: entry=${plan.pullback:.4f}, ATR=${plan.atr:.4f}, "
                  f"stop=${plan.stop_price:.4f} ({(1-plan.stop_price/plan.pullback):.1%}), "
                  f"shares={plan.shares:,}, cost=${cost:,.0f}")

            remaining_bars = bars_5m.iloc[entry_bar_idx + 1:]
            remaining_list = []
            for idx, bar in remaining_bars.iterrows():
                remaining_list.append({
                    "high": bar["high"], "low": bar["low"],
                    "close": bar["close"],
                    "timestamp": idx if isinstance(idx, pd.Timestamp) else date,
                })

            force_close_price = remaining_list[-1]["close"] if remaining_list else None

            if strategy == "stone":
                result = evaluate_trade_stone(plan, remaining_list, force_close_price,
                                              trail_pct_75=trail_75, trail_pct_150=trail_150)
            else:
                result = evaluate_trade(plan, remaining_list, force_close_price)

            result.date = str(date_key)
            result.open_price = open_price
            result.sell_target = plan.target_150
            result.stop_price = plan.stop_price

            extra = ""
            if result.partial_sell_shares > 0:
                extra = f", partial={result.partial_sell_shares:,}sh@${result.partial_sell_price:.4f}"
            if result.trailing_high > result.entry_price:
                extra += f", high=${result.trailing_high:.4f}"
            print(f"  Result: exit=${result.exit_price:.4f} ({result.exit_reason}), "
                  f"P&L=${result.pnl:,.2f} ({result.pnl_pct:.2%}){extra}")

            all_trades.append(result)
            equity += result.pnl
            positions_today += 1

    print(f"\n{'='*60}")
    print(f"[{strategy_label}] Backtest complete. Final equity: ${equity:,.2f}")
    print(f"Total trades: {len(all_trades)}")
    return all_trades
