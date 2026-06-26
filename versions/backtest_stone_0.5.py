"""Backtesting engine — Stone 0.5: MACD 2nd-derivative signals on 5-minute bars."""

import pandas as pd
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import Adjustment, DataFeed

import config
from scanner import get_data_client, get_tradable_symbols
from strategy_05 import (
    calc_macd,
    check_macd_buy_signal,
    calc_position_size,
    evaluate_trade_macd,
    TradeResult05,
)
from backtest import get_trading_days, bulk_scan_gaps


def get_5min_bars_with_warmup(
    client: StockHistoricalDataClient, symbol: str, date_ts: pd.Timestamp,
    warmup_days: int = config.MACD_WARMUP_DAYS,
) -> pd.DataFrame:
    """Fetch 5-min bars including prior days for MACD warmup."""
    start = date_ts - pd.Timedelta(days=warmup_days * 2 + 5)  # extra calendar days
    end = date_ts + pd.Timedelta(days=1)
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=start,
        end=end,
        adjustment=Adjustment.RAW,
        feed=DataFeed.IEX,
    )
    try:
        bars = client.get_stock_bars(request)
    except Exception:
        return pd.DataFrame()
    if bars.df.empty:
        return pd.DataFrame()
    df = bars.df
    if isinstance(df.index, pd.MultiIndex):
        if symbol in df.index.get_level_values("symbol"):
            df = df.xs(symbol, level="symbol", drop_level=False)
    return df


def _bars_to_list(bars_df: pd.DataFrame, date_key) -> list[dict]:
    """Convert 5-min bar DataFrame to list of dicts, filtered to a specific date."""
    target_date = pd.Timestamp(date_key).date() if not isinstance(date_key, pd.Timestamp) else date_key.date()
    result = []
    for idx, row in bars_df.iterrows():
        ts = idx[1] if isinstance(idx, tuple) else idx
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC").tz_convert("America/New_York")
        elif str(ts.tzinfo) != "America/New_York":
            ts = ts.tz_convert("America/New_York")
        if ts.date() != target_date:
            continue
        result.append({
            "timestamp": ts,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": int(row["volume"]),
        })
    return result


def _all_closes_from_df(bars_df: pd.DataFrame) -> list[float]:
    """Extract all close prices from DataFrame (for MACD warmup)."""
    closes = []
    for idx, row in bars_df.iterrows():
        closes.append(float(row["close"]))
    return closes


def _all_lows_from_df(bars_df: pd.DataFrame) -> list[float]:
    """Extract all low prices from DataFrame (for stop loss check)."""
    lows = []
    for idx, row in bars_df.iterrows():
        lows.append(float(row["low"]))
    return lows


def run_backtest_05(
    end_date: pd.Timestamp | None = None, n_days: int = config.BACKTEST_DAYS
) -> list[TradeResult05]:
    """Run Stone 0.5 backtest with MACD 2nd-derivative signals on 5-min bars."""

    client = get_data_client()

    if end_date is None:
        end_date = pd.Timestamp.now(tz="America/New_York")

    print(f"[Stone 0.5] Backtesting {n_days} trading days")
    print(f"Capital: ${config.INITIAL_CAPITAL:,.0f} | Deploy: {config.EQUITY_POSITION_RATIO:.0%}")
    print(f"MACD: {config.MACD_FAST}/{config.MACD_SLOW}/{config.MACD_SIGNAL} | 2nd-derivative signals | 5-min bars")
    print(f"Stop loss: {config.MACD_STOP_PCT:.0%} | Entry cutoff: {config.MACD_ENTRY_CUTOFF_TIME} | Force close: {config.FORCE_CLOSE_TIME}")

    symbols = get_tradable_symbols()
    print(f"Loading tradable symbols... Found {len(symbols)} tradable symbols")

    trading_days = get_trading_days(client, end_date=end_date, n_days=n_days)
    if not trading_days:
        print("No trading days found.")
        return []

    start_date = trading_days[0]
    end_d = trading_days[-1]
    print(f"Backtest period: {start_date.date()} to {end_d.date()}")

    print(f"\nBulk scanning for gaps...")
    gap_data = bulk_scan_gaps(client, trading_days, symbols)
    print(f"Found {sum(len(v) for v in gap_data.values())} gap entries across {len(gap_data)} days")

    equity = config.INITIAL_CAPITAL
    all_trades: list[TradeResult05] = []

    for date_key, candidates_df in sorted(gap_data.items()):
        if candidates_df.empty:
            continue

        deploy = calc_position_size(equity)
        pos_per_stock = min(deploy, config.MAX_POSITION_SIZE)
        max_stocks = max(config.MAX_POSITIONS_PER_DAY, int(deploy / pos_per_stock))
        candidates_df = candidates_df.head(max_stocks)

        daily_trades = 0
        print(f"\n--- {date_key} ({len(candidates_df)} candidates, equity: ${equity:,.0f}, deploy: ${deploy:,.0f}) ---")

        for _, cand in candidates_df.iterrows():
            if daily_trades >= config.MAX_DAILY_TRADES:
                break

            symbol = cand["symbol"]
            open_price = cand["open_price"]

            # Fetch 5-min bars with warmup
            bars_df = get_5min_bars_with_warmup(client, symbol, pd.Timestamp(date_key))
            if bars_df.empty:
                continue

            # Get today's bars
            today_bars = _bars_to_list(bars_df, date_key)
            if len(today_bars) < 5:
                print(f"  {symbol}: only {len(today_bars)} 5-min bars today")
                continue

            # Get all closes/lows including warmup for MACD calculation
            all_closes = _all_closes_from_df(bars_df)
            all_lows = _all_lows_from_df(bars_df)
            if len(all_closes) < config.MACD_WARMUP_BARS:
                print(f"  {symbol}: only {len(all_closes)} total bars for MACD warmup")
                continue

            # Calculate MACD on all closes (including warmup)
            macd_line, signal_line, histogram = calc_macd(all_closes)

            # Find the index offset: where today's bars start in all_closes
            warmup_count = len(all_closes) - len(today_bars)
            today_closes = [b["close"] for b in today_bars]

            # Find buy signal in today's portion of MACD
            buy_idx = check_macd_buy_signal(macd_line, signal_line, warmup_count)
            if buy_idx < 0:
                print(f"  {symbol}: no MACD buy signal")
                continue

            # Convert back to today's bar index
            entry_bar_today = buy_idx - warmup_count
            if entry_bar_today < 0:
                print(f"  {symbol}: buy signal in warmup period, skipping")
                continue

            # Entry time check
            entry_ts = today_bars[entry_bar_today]["timestamp"]
            cutoff = pd.Timestamp(f"{date_key} {config.MACD_ENTRY_CUTOFF_TIME}", tz="America/New_York")
            if entry_ts > cutoff:
                print(f"  {symbol}: buy signal at {entry_ts.strftime('%H:%M')} after cutoff {config.MACD_ENTRY_CUTOFF_TIME}, skipping")
                continue

            # Position sizing
            entry_price = today_closes[entry_bar_today]
            pos_size = min(calc_position_size(equity), config.MAX_POSITION_SIZE)
            shares = int(pos_size / entry_price)
            if shares <= 0:
                continue

            # Stop loss price
            stop_price = round(entry_price * (1 - config.MACD_STOP_PCT), 4)

            # Force close index
            force_close_ts = pd.Timestamp(f"{date_key} {config.FORCE_CLOSE_TIME}", tz="America/New_York")
            force_close_today = None
            for i, bar in enumerate(today_bars):
                if bar["timestamp"] >= force_close_ts:
                    force_close_today = i
                    break

            # Evaluate trade (all indices are absolute in all_closes/macd_line arrays)
            result = evaluate_trade_macd(
                symbol=symbol,
                open_price=open_price,
                shares=shares,
                all_closes=all_closes,
                all_lows=all_lows,
                macd_line=macd_line,
                signal_line=signal_line,
                entry_bar_idx=buy_idx,
                stop_price=stop_price,
                force_close_idx=(warmup_count + force_close_today) if force_close_today is not None else None,
            )
            result.date = str(date_key)
            # Adjust bar indices to today-relative
            result.entry_bar_idx = entry_bar_today
            result.exit_bar_idx = result.exit_bar_idx - warmup_count

            entry_time = entry_ts.strftime("%H:%M")
            exit_time = today_bars[result.exit_bar_idx]["timestamp"].strftime("%H:%M") if 0 <= result.exit_bar_idx < len(today_bars) else "?"
            print(f"  {symbol}: entry=${entry_price:.4f} ({entry_time}) exit=${result.exit_price:.4f} ({exit_time}) "
                  f"P&L=${result.pnl:,.2f} ({result.pnl_pct:.2%}) [{result.exit_reason}] stop=${stop_price:.4f}")

            all_trades.append(result)
            equity += result.pnl
            daily_trades += 1

    print(f"\n{'=' * 60}")
    print(f"[Stone 0.5] Backtest complete. Final equity: ${equity:,.2f}")
    print(f"Total trades: {len(all_trades)}")

    return all_trades
