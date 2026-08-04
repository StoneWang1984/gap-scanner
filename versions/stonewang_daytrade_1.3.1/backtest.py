"""Backtesting engine — Stone 1.3.1: 1.2.0 entry (3-bar) + rossway 0.1wp stop/target (tiered OCO)."""

import json
import os
import re

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


# ── Tiered stop/target (rossway 0.1wp) ──
def calc_stop_and_target(entry_price: float) -> tuple[float, float]:
    tiers = getattr(config, "STOP_TIERS", None)
    if tiers:
        for min_p, max_p, stop_pct, target_pct in tiers:
            if min_p <= entry_price < max_p:
                stop_price = round(entry_price * (1 - stop_pct), 2)
                target_price = round(entry_price * (1 + target_pct), 2)
                return stop_price, target_price
    return round(entry_price * 0.97, 2), round(entry_price * 1.03, 2)


def simulate_oco_trade(entry_price, stop_price, target_price, bars_list, shares=1):
    """Simple OCO: exit at first bar where high>=target or low<=stop."""
    highest = entry_price
    for i, bar in enumerate(bars_list):
        high = bar["high"]
        low = bar["low"]
        if high > highest:
            highest = high
        if low <= stop_price:
            if high >= target_price:
                if bar["open"] >= target_price:
                    return target_price, "take_profit", i, highest
            return stop_price, "stop_loss", i, highest
        if high >= target_price:
            return target_price, "take_profit", i, highest
    if bars_list:
        return bars_list[-1]["close"], "force_close", len(bars_list) - 1, highest
    return entry_price, "no_data", -1, highest


# ── Leveraged ETF filter ─────────────────────────────────────────────
_LEV_PATTERN = re.compile(r'(2X|3X|BULL|BEAR)$', re.IGNORECASE)
_LEV_SUFFIXES = ("BULL", "BEAR")  # single-letter suffixes removed (L/U/S matched AAPL/GOOGL/MS)
_LEV_PREFIXES = (
    "TQQQ", "SQQQ", "UPRO", "SPXU", "TNA", "TZA",
    "MSTU", "MSTZ", "CONL", "NAIL", "WEBL", "FNGU",
    "FNGD", "SOXL", "SOXS", "TECL", "TECS", "UDOW",
    "SDOW", "UMDD", "SMDD", "TQQ", "SQQ", "YINN",
    "YANG", "CURE", "LABD", "LABU", "DRN", "DRV",
    "DGP", "DGZ", "BOIL", "KOLD", "NUGT", "DUST",
    "JNUG", "JDST", "GLL", "UGL", "AXTU", "RDWU",
)


def is_leveraged_etf(symbol: str) -> bool:
    if _LEV_PATTERN.search(symbol):
        return True
    if any(symbol.endswith(s) for s in _LEV_SUFFIXES):
        return True
    if any(symbol.startswith(p) for p in _LEV_PREFIXES):
        return True
    return False


def get_trading_days(client: StockHistoricalDataClient, end_date: pd.Timestamp, n_days: int) -> list[pd.Timestamp]:
    start = end_date - pd.Timedelta(days=n_days * 2 + 10)
    request = StockBarsRequest(
        symbol_or_symbols="SPY", timeframe=TimeFrame.Day,
        start=start, end=end_date, adjustment=Adjustment.RAW, feed=getattr(config, "DATA_FEED_OBJ", DataFeed.IEX),
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
            start=start, end=end, adjustment=Adjustment.RAW, feed=getattr(config, "DATA_FEED_OBJ", DataFeed.IEX),
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
                    if gap_pct > getattr(config, "GAP_MAX", 1.0):
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
        start=market_open, end=market_close, adjustment=Adjustment.RAW, feed=getattr(config, "DATA_FEED_OBJ", DataFeed.IEX),
    )
    bars = client.get_stock_bars(request)
    if bars.df.empty:
        return pd.DataFrame()
    return bars.df


def get_1min_bars(client, symbol, date) -> pd.DataFrame:
    market_open = pd.Timestamp(f"{date.date()} {config.MARKET_OPEN}", tz="America/New_York")
    market_close = pd.Timestamp(f"{date.date()} {config.MARKET_CLOSE}", tz="America/New_York")
    request = StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=market_open, end=market_close, adjustment=Adjustment.RAW, feed=getattr(config, "DATA_FEED_OBJ", DataFeed.IEX),
    )
    bars = client.get_stock_bars(request)
    if bars.df.empty:
        return pd.DataFrame()
    return bars.df


def find_entry_with_confirmation_1min(bars_1m, open_price):
    """1分钟K线折返点检测 — 3根确认bar (1.2.0 style: low>bottom + close>bottom + >=1阳线)。"""
    if bars_1m.empty or len(bars_1m) < 4:
        return 0, -1, False
    pullback_idx = -1
    pullback_price = 0.0
    for i in range(len(bars_1m)):
        if bars_1m.iloc[i]["low"] < open_price:
            pullback_idx = i
            pullback_price = bars_1m.iloc[i]["low"]
            break
    if pullback_idx < 0:
        return 0, -1, False
    if not config.ENTRY_CONFIRMATION:
        return pullback_price, pullback_idx, True
    confirm_count = 0
    bullish_count = 0
    last_confirm_idx = -1
    for i in range(pullback_idx + 1, len(bars_1m)):
        bar_low = bars_1m.iloc[i]["low"]
        bar_close = bars_1m.iloc[i]["close"]
        bar_open = bars_1m.iloc[i]["open"]

        if bar_low < open_price and bar_low < pullback_price:
            pullback_idx = i
            pullback_price = bar_low
            confirm_count = 0
            bullish_count = 0
            last_confirm_idx = -1
            continue

        if bar_low <= pullback_price or bar_close <= pullback_price:
            continue

        confirm_count += 1
        last_confirm_idx = i
        if bar_close > bar_open:
            bullish_count += 1

        if confirm_count >= 3 and bullish_count >= 1:
            return pullback_price, last_confirm_idx, True

    return 0, -1, False


def locate_5min_bar_index(bars_5m, entry_timestamp):
    """Map 1-min entry timestamp to 5-min bar index."""
    # Handle multi-index (symbol, timestamp) tuples
    if isinstance(entry_timestamp, tuple):
        entry_ts = pd.Timestamp(entry_timestamp[1])
    else:
        entry_ts = pd.Timestamp(entry_timestamp)
    if entry_ts.tzinfo is None:
        entry_ts = entry_ts.tz_localize('UTC').tz_convert('America/New_York')
    bucket_minute = (entry_ts.minute // 5) * 5
    bucket_ts = entry_ts.replace(minute=bucket_minute, second=0, microsecond=0)
    for i in range(len(bars_5m)):
        idx = bars_5m.index[i]
        bar_ts = pd.Timestamp(idx) if not isinstance(idx, tuple) else pd.Timestamp(idx[1])
        if bar_ts.tzinfo is None:
            bar_ts = bar_ts.tz_localize('UTC').tz_convert('America/New_York')
        bar_bucket = bar_ts.replace(minute=(bar_ts.minute // 5) * 5, second=0, microsecond=0)
        if bar_bucket == bucket_ts:
            return i
    return 0


def find_entry_with_confirmation(bars_5m, open_price):
    """DEPRECATED: 旧版5分钟入场检测，保留兼容。"""
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
            ts = ts.tz_localize('UTC')
        ts = ts.tz_convert('America/New_York')
        result.append({
            "high": bar["high"], "low": bar["low"], "close": bar["close"],
            "open": bar["open"], "volume": int(bar["volume"]) if "volume" in bar.index else 0,
            "timestamp": ts,
        })
    return result


def _bars_to_chart(bars_df):
    """Convert DataFrame to chart_data.json bar format."""
    result = []
    for i in range(len(bars_df)):
        bar = bars_df.iloc[i]
        idx = bars_df.index[i]
        ts = idx
        if isinstance(idx, tuple):
            ts = idx[1]
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize('UTC')
        ts = ts.tz_convert('America/New_York')
        result.append({
            "ts": ts.strftime("%H:%M"),
            "o": round(float(bar["open"]), 4), "h": round(float(bar["high"]), 4),
            "l": round(float(bar["low"]), 4), "c": round(float(bar["close"]), 4),
            "v": int(bar["volume"]) if "volume" in bar.index else 0,
        })
    return result


def _find_target_bar(bars_list, start_idx, target_price):
    """Find first bar after start_idx where high >= target_price. Returns bar index or None."""
    for i in range(start_idx, len(bars_list)):
        if bars_list[i]["high"] >= target_price:
            return i
    return None


def _bar_ts_str(bars_list, idx):
    """Get HH:MM timestamp string from bars_list at index."""
    if 0 <= idx < len(bars_list):
        return bars_list[idx]["timestamp"].strftime("%H:%M")
    return "00:00"


def save_backtest_charts(chart_entries, filepath="versions/chart_data.json"):
    """Save collected chart data to JSON file for dashboard."""
    date_parts = sorted(set(v["date"] for v in chart_entries.values()))
    date_range = f"{date_parts[0]} to {date_parts[-1]}" if len(date_parts) > 1 else date_parts[0]
    output = {"date": date_range, "symbols": chart_entries}
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Chart data saved to {filepath} ({len(chart_entries)} symbols)")


def run_backtest(end_date=None, n_days=config.BACKTEST_DAYS) -> list[TradeResult]:
    client = get_data_client()
    if end_date is None:
        end_date = pd.Timestamp.now(tz="America/New_York")

    trading_days = get_trading_days(client, end_date, n_days)
    if not trading_days:
        print("No trading days found.")
        return []

    print(f"[Stone 1.3.1] Backtesting {len(trading_days)} trading days: {trading_days[0].date()} to {trading_days[-1].date()}")
    print(f"Capital: ${config.INITIAL_CAPITAL:,.0f} | Deploy: {config.EQUITY_POSITION_RATIO:.0%} | "
          f"Per-stock cap: ${config.MAX_POSITION_SIZE:,.0f} | Max daily trades: {config.MAX_DAILY_TRADES}")
    retracements = getattr(config, "PROFIT_RETRACEMENT_TIERS", [0.10, 0.20, 0.35, 0.50, 0.75, 1.00, 1.25, 1.50])
    sell_ratios = getattr(config, "PARTIAL_SELL_RATIOS", [1/8]*8)
    caps = getattr(config, "TARGET_CAP_TIERS", [0.01, 0.02, 0.035, 0.05, 0.08, 0.10, 0.13, 0.18])
    trail_pcts = getattr(config, "TRAILING_STOP_PCTS", [0.01, 0.015, 0.02, 0.025, 0.03, 0.035, 0.04, 0.05])
    tier_desc = " + ".join(f"{int(r*100)}%/1÷{int(round(1/s))}" for r, s in zip(retracements, sell_ratios))
    cap_desc = "/".join(f"{int(c*100)}%" for c in caps)
    print(f"First trade: {tier_desc} | Caps: {cap_desc} | Trail: {'/'.join(f'{t:.1%}' for t in trail_pcts)}")
    reentry_ret1 = getattr(config, "REENTRY_PROFIT_RETRACEMENT_1", 0.75)
    use_tiered = getattr(config, "USE_TIERED_STOP_TARGET", False)
    if use_tiered:
        tiers = getattr(config, "STOP_TIERS", [])
        print(f"Exit: tiered OCO ({len(tiers)} price tiers) | Entry: 3-bar (1.2.0 style)")
    else:
        print(f"Exit: 8-tier ladder | Entry: 3-tier (1.3.0 style)")
    reentry_trail = getattr(config, "REENTRY_TRAILING_PCT", 0.01)
    reentry_max_bars = getattr(config, "REENTRY_MAX_BARS_BEFORE_TARGET", 0)
    tl_str = f"{reentry_max_bars} bars" if reentry_max_bars > 0 else "none"
    print(f"Re-entry: {reentry_ret1:.0%} retracement/50% + {reentry_trail:.0%} trail | "
          f"Time limit: {tl_str} | Pullback stop: {config.PULLBACK_STOP_THRESHOLD:.0%}")

    print("\nLoading tradable symbols...")
    symbols = get_tradable_symbols()
    print(f"Found {len(symbols)} tradable symbols")

    # Filter leveraged ETFs
    symbols = [s for s in symbols if not is_leveraged_etf(s)]
    print(f"After leveraged ETF filter: {len(symbols)} symbols")

    print("\nBulk scanning for gaps...")
    gap_data = bulk_scan_gaps(client, trading_days, symbols)
    total_candidates = sum(len(v) for v in gap_data.values())
    print(f"Found {total_candidates} gap entries across {len(gap_data)} days")

    all_trades: list[TradeResult] = []
    equity = config.INITIAL_CAPITAL
    chart_entries = {}  # chart data for dashboard

    for date in trading_days:
        date_key = date.date()
        if date_key not in gap_data or gap_data[date_key].empty:
            continue

        n_cands = len(gap_data[date_key])
        # v1.2: monitor ALL candidates (like live), limit by simultaneous positions
        max_positions = getattr(config, "MAX_POSITIONS_PER_DAY", 0)
        if max_positions <= 0:
            max_positions = n_cands  # 0 = no limit
        active_positions = 0
        # Slot-based allocation: divide equity by max positions (same as live)
        # With EQUITY_POSITION_RATIO=1.0, deploys 100% of equity
        pos_per_stock = min(equity * config.EQUITY_POSITION_RATIO / max(max_positions, 1), config.MAX_POSITION_SIZE)
        candidates = gap_data[date_key]  # all candidates, not just top N

        print(f"\n--- {date_key} ({len(gap_data[date_key])} candidates, equity: ${equity:,.0f}, "
              f"per-stock: ${pos_per_stock:,.0f}) ---")

        daily_trades = 0
        daily_stopped = False
        daily_loss = 0.0
        max_daily_loss = equity * getattr(config, "MAX_DAILY_LOSS_PCT", 0.05)

        for _, row in candidates.iterrows():
            if (config.MAX_DAILY_TRADES > 0 and daily_trades >= config.MAX_DAILY_TRADES) or daily_stopped:
                break

            # Check simultaneous position limit (like live)
            if max_positions > 0 and active_positions >= max_positions:
                continue

            # Daily loss circuit breaker
            if max_daily_loss > 0 and daily_loss <= -max_daily_loss:
                print(f"  Daily loss ${daily_loss:,.2f} exceeded limit ${-max_daily_loss:,.2f}, stopping for day")
                daily_stopped = True
                break

            symbol = row["symbol"]
            open_price = row["open_price"]

            bars_5m = get_5min_bars(client, symbol, date)
            if bars_5m.empty or len(bars_5m) < 3:
                continue
            bars_1m = get_1min_bars(client, symbol, date)

            all_bars_5m = _bars_to_list(bars_5m)

            # ========== FIRST TRADE: 1-min entry detection ==========
            if bars_1m.empty or len(bars_1m) < 2:
                # Fallback to 5-min if 1-min unavailable
                pullback, entry_bar_idx, confirmed = find_entry_with_confirmation(bars_5m, open_price)
                entry_bar_idx_5m = entry_bar_idx
            else:
                pullback, entry_bar_idx_1m, confirmed = find_entry_with_confirmation_1min(bars_1m, open_price)
                # Map 1-min entry to 5-min bar index
                if confirmed and entry_bar_idx_1m >= 0:
                    entry_ts_1m = bars_1m.index[entry_bar_idx_1m]
                    entry_bar_idx_5m = locate_5min_bar_index(bars_5m, entry_ts_1m)
                else:
                    entry_bar_idx_5m = -1
            if not confirmed or pullback <= 0:
                print(f"  {symbol}: no confirmed entry, skipping")
                continue

            # 0.4.11: Skip if entry price >= open price
            if pullback >= open_price:
                print(f"  {symbol}: entry ${pullback:.4f} >= open ${open_price:.4f}, skipping")
                continue

            # Entry time check — use 5-min bar timestamp for cutoff
            idx_val = bars_5m.index[entry_bar_idx_5m]
            if isinstance(idx_val, tuple):
                entry_ts = pd.Timestamp(idx_val[1])
            else:
                entry_ts = pd.Timestamp(idx_val)
            if entry_ts.tzinfo is None:
                entry_ts = entry_ts.tz_localize('UTC')
            entry_ts = entry_ts.tz_convert('America/New_York')
            cutoff = pd.Timestamp(f"{date_key} 10:00", tz="America/New_York")
            if entry_ts > cutoff:
                print(f"  {symbol}: entry after 10:00, skipping")
                continue

            # ATR (still use 5-min bars for ATR calculation)
            bars_for_atr = []
            for j in range(min(entry_bar_idx_5m + 1, len(bars_5m))):
                b = bars_5m.iloc[j]
                bars_for_atr.append({"high": b["high"], "low": b["low"], "close": b["close"]})
            atr = calc_atr(bars_for_atr, period=14)

            # ── Entry slippage model: dynamic based on gap size ──
            gap_pct = (open_price - pullback) / open_price if open_price > 0 else 0
            slippage_base = getattr(config, "SLIPPAGE_ENTRY_BASE", 0.01)
            slippage_gap_factor = getattr(config, "SLIPPAGE_ENTRY_GAP_FACTOR", 0.15)
            slippage_max = getattr(config, "SLIPPAGE_ENTRY_MAX", 0.05)
            entry_slippage = min(slippage_max, slippage_base + gap_pct * slippage_gap_factor)
            entry_price_actual = round(pullback * (1 + entry_slippage), 2)

            stop_price = calc_stop_price(entry_price_actual, atr)
            # 0.4.14: Cap stop loss at max percentage
            stop_max_pct = getattr(config, "STOP_LOSS_MAX_PCT", 0)
            if stop_max_pct > 0:
                min_stop = round(entry_price_actual * (1 - stop_max_pct), 2)
                stop_price = max(stop_price, min_stop)

            pos_size = min(pos_per_stock, config.MAX_POSITION_SIZE)
            shares = int(pos_size / entry_price_actual)
            if shares <= 0:
                continue

            # Remaining 5-min bars after entry (position management uses 5-min)
            remaining_list = all_bars_5m[entry_bar_idx_5m + 1:]
            force_close_price = remaining_list[-1]["close"] if remaining_list else None

            # Alias for chart events (5-min bar index)
            entry_bar_idx = entry_bar_idx_5m

            use_tiered = getattr(config, "USE_TIERED_STOP_TARGET", False)

            if use_tiered:
                # ── Tiered OCO exit (rossway 0.1wp) ──
                stop_price_tier, target_price_tier = calc_stop_and_target(entry_price_actual)
                exit_price, exit_reason, exit_bar, highest = simulate_oco_trade(
                    entry_price_actual, stop_price_tier, target_price_tier, remaining_list, shares)
                pnl = (exit_price - entry_price_actual) * shares
                pnl_pct = (exit_price - entry_price_actual) / entry_price_actual
                print(f"  {symbol} [1st] entry=${entry_price_actual:.4f} exit=${exit_price:.4f} ({exit_reason}), "
                      f"P&L=${pnl:,.2f} ({pnl_pct:.2%}), stop=${stop_price_tier:.2f} target=${target_price_tier:.2f}, high=${highest:.4f}")
                result = TradeResult(
                    symbol=symbol, date=str(date_key), entry_price=entry_price_actual,
                    exit_price=exit_price, shares=shares, pnl=round(pnl, 2),
                    pnl_pct=round(pnl_pct, 4), exit_reason=exit_reason,
                    stop_price=stop_price_tier, sell_target=target_price_tier,
                    open_price=open_price, trailing_high=highest,
                )
            else:
                # ── 8-tier ladder exit (original 1.3.0) ──
                plan = build_trade_plan(symbol, open_price, entry_price_actual, atr,
                                         position_size=min(pos_per_stock, config.MAX_POSITION_SIZE))
                time_limit = getattr(config, "FIRST_TRADE_TIME_LIMIT_BARS", 0)
                result = evaluate_trade_stone(
                    plan, remaining_list, force_close_price,
                    time_limit_bars=time_limit,
                )
                result.date = str(date_key)
                result.open_price = open_price
                result.sell_target = plan.targets[-1]
                result.stop_price = plan.stop_price
                type_tag = f"[1st/{plan.target_mode}]"
                extra = ""
                retracements = getattr(config, "PROFIT_RETRACEMENT_TIERS", [0.10, 0.20, 0.35, 0.50, 0.75, 1.00, 1.25, 1.50])
                if result.partial_sells:
                    for ti, (p, s) in enumerate(result.partial_sells):
                        if s > 0:
                            extra += f", {retracements[ti]*100:.0f}%@${p:.4f}"
                if result.trailing_high > result.entry_price:
                    extra += f", high=${result.trailing_high:.4f}"
                print(f"  {symbol} {type_tag} entry=${entry_price_actual:.4f} exit=${result.exit_price:.4f} ({result.exit_reason}), "
                      f"P&L=${result.pnl:,.2f} ({result.pnl_pct:.2%}){extra}")

            all_trades.append(result)
            equity += result.pnl
            daily_loss += result.pnl
            daily_trades += 1
            active_positions += 1

            # ── Collect chart data for first trade ──
            sym_key = f"{symbol} ({date_key})"
            chart_bars = _bars_to_chart(bars_5m)
            events = []
            # Buy event
            events.append({"ts": _bar_ts_str(all_bars_5m, entry_bar_idx), "type": "buy",
                           "price": round(entry_price_actual, 4), "label": f"BUY {shares}sh"})
            # Target sells — find actual bar times
            next_search = entry_bar_idx + 1
            retracements = getattr(config, "PROFIT_RETRACEMENT_TIERS", [0.10, 0.20, 0.35, 0.50, 0.75, 1.00, 1.25, 1.50])
            if result.partial_sells:
                for ti, (p, s) in enumerate(result.partial_sells):
                    if s > 0:
                        t_idx = _find_target_bar(all_bars_5m, next_search, plan.targets[ti])
                        ts_str = _bar_ts_str(all_bars_5m, t_idx) if t_idx is not None else _bar_ts_str(all_bars_5m, next_search)
                        pct_label = f"{int(retracements[ti]*100)}%"
                        events.append({"ts": ts_str, "type": "sell", "price": round(p, 4),
                                       "label": f"TARGET_{pct_label} {s}sh"})
                        next_search = (t_idx or next_search) + 1
            # Exit event
            exit_bar_in_all = entry_bar_idx + 1 + result.exit_bar_idx
            target_reasons = {f"target_{int(r*100)}" for r in getattr(config, "PROFIT_RETRACEMENT_TIERS", [])}
            if result.exit_reason not in target_reasons:
                exit_label = result.exit_reason.upper().replace("_", " ")
                total_sold = sum(s for _, s in (result.partial_sells or []))
                remaining_shares = shares - total_sold
                if remaining_shares > 0:
                    events.append({"ts": _bar_ts_str(all_bars_5m, exit_bar_in_all), "type": "sell",
                                   "price": round(result.exit_price, 4), "label": f"{exit_label} {remaining_shares}sh"})

            if use_tiered:
                targets_dict = {"OCO": round(target_price_tier, 4)}
                chart_stop = stop_price_tier
            else:
                retracements = getattr(config, "PROFIT_RETRACEMENT_TIERS", [0.10, 0.20, 0.35, 0.50, 0.75, 1.00, 1.25, 1.50])
                targets_dict = {f"{int(r*100)}%": round(t, 4) for r, t in zip(retracements, plan.targets)}
                chart_stop = plan.stop_price
            chart_entries[sym_key] = {
                "date": str(date_key), "bars_5m": chart_bars, "bars_1m": [],
                "events": events,
                "entry_price": round(entry_price_actual, 4),
                "stop_price": round(chart_stop, 4),
                "targets": targets_dict,
                "pnl": round(result.pnl, 2), "open_price": round(open_price, 4),
            }

            # ========== RE-ENTRY TRADES ==========
            # Find bars after first trade's exit
            exit_bar_in_all = entry_bar_idx + 1 + result.exit_bar_idx
            bars_after_exit = all_bars_5m[exit_bar_in_all + 1:]

            # Decrement active position count after first trade exits
            active_positions -= 1

            if result.exit_reason == "force_close" or not bars_after_exit:
                continue

            # Re-entry cutoff time check (same as live)
            reentry_cutoff = getattr(config, "REENTRY_CUTOFF_TIME", "12:30")
            reentry_cutoff_ts = pd.Timestamp(f"{date_key} {reentry_cutoff}", tz="America/New_York")

            reentry_round = 1
            while (config.MAX_DAILY_TRADES == 0 or daily_trades < config.MAX_DAILY_TRADES) and not daily_stopped:
                if not bars_after_exit or len(bars_after_exit) < 3:
                    break

                # Daily loss circuit breaker (check before each re-entry)
                if max_daily_loss > 0 and daily_loss <= -max_daily_loss:
                    print(f"  Daily loss ${daily_loss:,.2f} exceeded limit ${-max_daily_loss:,.2f}, stopping for day")
                    daily_stopped = True
                    break

                # Re-entry cutoff time check
                if bars_after_exit[0]["timestamp"].tz_convert("America/New_York") > reentry_cutoff_ts:
                    break

                reentry_price, prev_high, reentry_idx, reentry_confirmed = find_reentry_point(
                    bars_after_exit, open_price, initial_highest=result.trailing_high
                )

                if not reentry_confirmed or reentry_price <= 0:
                    break

                # ── Re-entry slippage model: market buy fills at ask, not bar low ──
                reentry_slippage = getattr(config, "SLIPPAGE_ENTRY_PCT", 0.005)
                reentry_price_actual = round(reentry_price * (1 + reentry_slippage), 2)

                # 0.4.14: Minimum pullback from peak for re-entry
                reentry_min_pullback = getattr(config, "REENTRY_MIN_PULLBACK", 0)
                if reentry_min_pullback > 0 and prev_high > 0:
                    pullback_pct = (prev_high - reentry_price) / prev_high
                    if pullback_pct < reentry_min_pullback:
                        print(f"  {symbol}: re-entry pullback {pullback_pct:.1%} < min {reentry_min_pullback:.0%}, skipping")
                        break

                # Check if significant pullback occurred — skip re-entry for THIS stock only
                if prev_high > 0 and (prev_high - reentry_price) / prev_high > config.PULLBACK_STOP_THRESHOLD:
                    print(f"  {symbol}: significant pullback from ${prev_high:.4f}, skipping re-entry")
                    break

                # Re-entry trade — 0.4.14: ATR stop, retracement target + trailing
                # ATR-based stop
                reentry_bars_for_atr = []
                for j in range(min(reentry_idx + 1, len(bars_after_exit))):
                    b = bars_after_exit[j]
                    reentry_bars_for_atr.append({"high": b["high"], "low": b["low"], "close": b["close"]})
                reentry_atr = calc_atr(reentry_bars_for_atr, period=14)
                if reentry_atr > 0:
                    reentry_stop = round(reentry_price_actual - getattr(config, "REENTRY_STOP_ATR_MULT", 1.5) * reentry_atr, 2)
                    fallback = round(reentry_price_actual * (1 - getattr(config, "REENTRY_STOP_PCT_FALLBACK", 0.04)), 2)
                    reentry_stop = max(reentry_stop, fallback)
                else:
                    reentry_stop = round(reentry_price_actual * (1 - config.REENTRY_STOP_PCT), 2)

                # 0.4.14: Cap re-entry stop loss at max percentage
                if stop_max_pct > 0:
                    reentry_min_stop = round(reentry_price_actual * (1 - stop_max_pct), 2)
                    reentry_stop = max(reentry_stop, reentry_min_stop)

                reentry_retracement_1 = getattr(config, "REENTRY_PROFIT_RETRACEMENT_1", 0.75)
                reentry_target_1 = round(reentry_price_actual + reentry_retracement_1 * (prev_high - reentry_price_actual), 2)

                pos_size_re = min(pos_per_stock, config.MAX_POSITION_SIZE)
                reentry_pos_ratio = getattr(config, "REENTRY_POSITION_RATIO", 0.5)
                reentry_pos_dollar = pos_size_re * reentry_pos_ratio
                # Check minimum position size for re-entry
                reentry_min_pos = getattr(config, "REENTRY_MIN_POSITION_SIZE", 0)
                if reentry_min_pos > 0 and reentry_pos_dollar < reentry_min_pos:
                    print(f"  {symbol}: re-entry pos ${reentry_pos_dollar:.2f} < min ${reentry_min_pos:.0f}, skipping")
                    break
                reentry_shares = int(reentry_pos_dollar / reentry_price_actual)
                if reentry_shares <= 0:
                    break

                # Remaining bars after re-entry
                reentry_remaining = bars_after_exit[reentry_idx + 1:]
                reentry_force_close = reentry_remaining[-1]["close"] if reentry_remaining else None

                reentry_result = evaluate_reentry_trade(
                    entry_price=reentry_price_actual,
                    prev_high=prev_high,
                    shares=reentry_shares,
                    symbol=symbol,
                    open_price=open_price,
                    bars_after_entry=reentry_remaining,
                    force_close_price=reentry_force_close,
                    stop_price=reentry_stop,
                    reentry_profit_retracement_1=reentry_retracement_1,
                    reentry_trailing_pct=getattr(config, "REENTRY_TRAILING_PCT", 0.01),
                )
                reentry_result.date = str(date_key)

                type_tag = f"[Re{reentry_round}]"
                re_extra = ""
                if reentry_result.trailing_high > reentry_result.entry_price:
                    re_extra += f", high=${reentry_result.trailing_high:.4f}"
                print(f"  {symbol} {type_tag} entry=${reentry_price_actual:.4f} exit=${reentry_result.exit_price:.4f} "
                      f"({reentry_result.exit_reason}), P&L=${reentry_result.pnl:,.2f} ({reentry_result.pnl_pct:.2%})"
                      f", stop=${reentry_stop:.4f}, target=${reentry_target_1:.4f}{re_extra}")

                all_trades.append(reentry_result)
                equity += reentry_result.pnl
                daily_loss += reentry_result.pnl
                daily_trades += 1

                # ── Collect chart data for re-entry trade ──
                re_sym_key = f"{symbol} RE ({date_key})"
                # Re-entry bars: offset into all_bars_5m
                reentry_start_in_all = exit_bar_in_all + 1 + reentry_idx
                re_events = []
                re_events.append({"ts": _bar_ts_str(all_bars_5m, reentry_start_in_all), "type": "buy",
                                  "price": round(reentry_price_actual, 4), "label": f"RE-ENTRY BUY {reentry_shares}sh"})
                # Exit event (trailing, stop, force close)
                reentry_exit_in_all = reentry_start_in_all + 1 + reentry_result.exit_bar_idx
                re_exit_label = reentry_result.exit_reason.upper().replace("_", " ")
                re_events.append({"ts": _bar_ts_str(all_bars_5m, reentry_exit_in_all), "type": "sell",
                                  "price": round(reentry_result.exit_price, 4), "label": f"{re_exit_label} {reentry_shares}sh"})

                chart_entries[re_sym_key] = {
                    "date": str(date_key), "bars_5m": chart_bars, "bars_1m": [],
                    "events": re_events,
                    "entry_price": round(reentry_price_actual, 4),
                    "stop_price": round(reentry_stop, 4),
                    "targets": {"75% retracement": round(reentry_target_1, 4)},
                    "pnl": round(reentry_result.pnl, 2), "open_price": round(open_price, 4),
                }

                reentry_round += 1

                # Prepare for next re-entry
                reentry_exit_bar = reentry_idx + 1 + reentry_result.exit_bar_idx
                bars_after_exit = bars_after_exit[reentry_exit_bar + 1:]

                if reentry_result.exit_reason == "reentry_force_close":
                    break

    print(f"\n{'='*60}")
    print(f"[Stone 1.3.1] Backtest complete. Final equity: ${equity:,.2f}")
    print(f"Total trades: {len(all_trades)}")

    if chart_entries:
        save_backtest_charts(chart_entries)

    return all_trades
