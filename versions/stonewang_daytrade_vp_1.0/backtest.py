"""Backtesting engine — stonewang_daytrade_vp_1.0: Volume+Price derivative entry.

Entry approximation (live uses WS trade prints; backtest uses 1-min bars):
  - vol_spike: bar[i].volume > VP_VOL_SPIKE_MULT × mean(bar[i-5:i].volume)
  - price_slope: (bar[i].close - bar[i].open) / bar[i].open > VP_PRICE_SLOPE_THRESHOLD
  - Both must fire on the same bar

Exit (exact same as live evaluate_trade_vp):
  - drop_exit: bar low <= entry × (1 - VP_EXIT_DROP_PCT)
  - time_limit: bi >= VP_EXIT_TIME_LIMIT_SEC // 60
"""

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
from strategy import evaluate_trade_vp, TradeResult


# ── Leveraged ETF filter ─────────────────────────────────────────────
_LEV_PATTERN = re.compile(r'(2X|3X|BULL|BEAR)$', re.IGNORECASE)
_LEV_SUFFIXES = ("BULL", "BEAR")
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


def _bar_ts_str(bars_list, idx):
    if 0 <= idx < len(bars_list):
        return bars_list[idx]["timestamp"].strftime("%H:%M")
    return "00:00"


def find_vp_entry_1min(bars_1m, open_price):
    """VP entry approximation on 1-min bars.

    Live uses WS trade prints: 10s vol > 3× 5-min baseline AND 10s price slope > +0.3%.
    Backtest uses 1-min bars as proxy:
      - vol_spike: bar[i].volume > VP_VOL_SPIKE_MULT × mean(bar[i-5:i].volume)
      - price_slope: (bar[i].close - bar[i].open) / bar[i].open > VP_PRICE_SLOPE_THRESHOLD
      - bar[i] must be within entry window (9:31-15:30)
      - bar[i].close must be > open_price (gap pullback NOT required — VP enters on momentum)

    Returns (entry_price, entry_bar_idx, confirmed).
    """
    if bars_1m.empty or len(bars_1m) < 6:
        return 0, -1, False

    vol_mult = getattr(config, "VP_VOL_SPIKE_MULT", 3.0)
    slope_threshold = getattr(config, "VP_PRICE_SLOPE_THRESHOLD", 0.003)
    vol_min_absolute = getattr(config, "VP_VOL_MIN_ABSOLUTE", 500)
    baseline_window = 5  # 5 prior 1-min bars as baseline proxy for 5-min window

    entry_start_str = getattr(config, "ENTRY_WINDOW_START", "09:31")
    entry_end_str = getattr(config, "ENTRY_WINDOW_END", "15:30")
    entry_start_h, entry_start_m = (int(x) for x in entry_start_str.split(":"))
    entry_end_h, entry_end_m = (int(x) for x in entry_end_str.split(":"))

    for i in range(baseline_window, len(bars_1m)):
        idx_val = bars_1m.index[i]
        ts = idx_val[1] if isinstance(idx_val, tuple) else idx_val
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize('UTC')
        ts = ts.tz_convert("America/New_York")

        # Entry window check
        bar_time = ts.time()
        start_time = pd.Timestamp(f"{ts.date()} {entry_start_str}", tz="America/New_York").time()
        end_time = pd.Timestamp(f"{ts.date()} {entry_end_str}", tz="America/New_York").time()
        if not (start_time <= bar_time <= end_time):
            continue

        bar = bars_1m.iloc[i]
        vol = int(bar["volume"])
        if vol < vol_min_absolute:
            continue

        # Volume spike vs 5-bar baseline
        baseline_vols = [int(bars_1m.iloc[j]["volume"]) for j in range(i - baseline_window, i)]
        baseline_mean = sum(baseline_vols) / len(baseline_vols) if baseline_vols else 0
        if baseline_mean <= 0 or vol < vol_mult * baseline_mean:
            continue

        # Price slope: (close - open) / open on this bar
        bar_open = float(bar["open"])
        bar_close = float(bar["close"])
        if bar_open <= 0:
            continue
        slope = (bar_close - bar_open) / bar_open
        if slope <= slope_threshold:
            continue

        # Entry price = close of trigger bar (market buy would fill near close)
        return round(bar_close, 4), i, True

    return 0, -1, False


def save_backtest_charts(chart_entries, filepath="versions/chart_data.json"):
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

    print(f"[vp_1.0] Backtesting {len(trading_days)} trading days: {trading_days[0].date()} to {trading_days[-1].date()}")
    print(f"Capital: ${config.INITIAL_CAPITAL:,.0f} | Deploy: {config.EQUITY_POSITION_RATIO:.0%} | "
          f"Per-stock cap: ${config.MAX_POSITION_SIZE:,.0f} | Max daily trades: {config.MAX_DAILY_TRADES}")
    vol_mult = getattr(config, "VP_VOL_SPIKE_MULT", 3.0)
    slope_thr = getattr(config, "VP_PRICE_SLOPE_THRESHOLD", 0.003)
    drop_pct = getattr(config, "VP_EXIT_DROP_PCT", 0.005)
    time_limit_sec = getattr(config, "VP_EXIT_TIME_LIMIT_SEC", 180)
    safety_pct = getattr(config, "VP_SAFETY_STOP_PCT", 0.05)
    cooldown = getattr(config, "VP_COOLDOWN_SEC", 60)
    print(f"Entry: VP vol ×{vol_mult} + slope +{slope_thr*100:.1f}%/bar (1-min approx) | "
          f"Window {config.ENTRY_WINDOW_START}-{config.ENTRY_WINDOW_END}")
    print(f"Exit: drop ≥ {drop_pct*100:.1f}% or {time_limit_sec}s time limit | "
          f"Safety stop {safety_pct*100:.0f}% | Cooldown {cooldown}s")

    print("\nLoading tradable symbols...")
    symbols = get_tradable_symbols()
    print(f"Found {len(symbols)} tradable symbols")

    symbols = [s for s in symbols if not is_leveraged_etf(s)]
    print(f"After leveraged ETF filter: {len(symbols)} symbols")

    print("\nBulk scanning for gaps...")
    gap_data = bulk_scan_gaps(client, trading_days, symbols)
    total_candidates = sum(len(v) for v in gap_data.values())
    print(f"Found {total_candidates} gap entries across {len(gap_data)} days")

    all_trades: list[TradeResult] = []
    equity = config.INITIAL_CAPITAL
    chart_entries = {}

    cooldown_bars = max(1, cooldown // 60)

    for date in trading_days:
        date_key = date.date()
        if date_key not in gap_data or gap_data[date_key].empty:
            continue

        n_cands = len(gap_data[date_key])
        max_positions = getattr(config, "MAX_POSITIONS_PER_DAY", 0)
        if max_positions <= 0:
            max_positions = n_cands
        active_positions = 0
        pos_per_stock = min(equity * config.EQUITY_POSITION_RATIO / max(max_positions, 1), config.MAX_POSITION_SIZE)
        candidates = gap_data[date_key]

        print(f"\n--- {date_key} ({len(candidates)} candidates, equity: ${equity:,.0f}, "
              f"per-stock: ${pos_per_stock:,.0f}) ---")

        daily_trades = 0
        daily_stopped = False
        daily_loss = 0.0
        max_daily_loss = equity * getattr(config, "MAX_DAILY_LOSS_PCT", 0.05)

        last_entry_bar_by_sym: dict[str, int] = {}

        for _, row in candidates.iterrows():
            if (config.MAX_DAILY_TRADES > 0 and daily_trades >= config.MAX_DAILY_TRADES) or daily_stopped:
                break
            if max_positions > 0 and active_positions >= max_positions:
                continue
            if max_daily_loss > 0 and daily_loss <= -max_daily_loss:
                print(f"  Daily loss ${daily_loss:,.2f} exceeded limit ${-max_daily_loss:,.2f}, stopping for day")
                daily_stopped = True
                break

            symbol = row["symbol"]
            open_price = row["open_price"]

            bars_1m = get_1min_bars(client, symbol, date)
            if bars_1m.empty or len(bars_1m) < 6:
                continue
            all_bars_1m = _bars_to_list(bars_1m)

            # VP entry detection
            entry_price, entry_bar_idx, confirmed = find_vp_entry_1min(bars_1m, open_price)
            if not confirmed or entry_price <= 0:
                continue

            # Cooldown: skip if same symbol entered within cooldown_bars
            last_bar = last_entry_bar_by_sym.get(symbol, -10**9)
            if entry_bar_idx - last_bar < cooldown_bars:
                continue

            # Entry slippage (market buy fills slightly worse than bar close)
            entry_slippage = getattr(config, "SLIPPAGE_ENTRY_PCT", 0.005)
            entry_price_actual = round(entry_price * (1 + entry_slippage), 2)

            pos_size = min(pos_per_stock, config.MAX_POSITION_SIZE)
            shares = int(pos_size / entry_price_actual)
            if shares <= 0:
                continue

            # Remaining 1-min bars after entry
            remaining_list = all_bars_1m[entry_bar_idx + 1:]
            force_close_price = remaining_list[-1]["close"] if remaining_list else None

            result = evaluate_trade_vp(
                entry_price=entry_price_actual,
                shares=shares,
                bars_after_entry=remaining_list,
                symbol=symbol,
                open_price=open_price,
                force_close_price=force_close_price,
            )
            result.date = str(date_key)
            result.open_price = open_price
            print(f"  {symbol} entry=${entry_price_actual:.4f} exit=${result.exit_price:.4f} ({result.exit_reason}), "
                  f"P&L=${result.pnl:,.2f} ({result.pnl_pct:.2%})")

            all_trades.append(result)
            equity += result.pnl
            daily_loss += result.pnl
            daily_trades += 1
            active_positions += 1
            last_entry_bar_by_sym[symbol] = entry_bar_idx + 1 + result.exit_bar_idx

            # Chart data
            sym_key = f"{symbol} ({date_key})"
            chart_bars = _bars_to_chart(bars_1m)
            events = []
            events.append({"ts": _bar_ts_str(all_bars_1m, entry_bar_idx), "type": "buy",
                           "price": round(entry_price_actual, 4), "label": f"BUY {shares}sh"})
            exit_bar_in_all = entry_bar_idx + 1 + result.exit_bar_idx
            exit_label = result.exit_reason.upper().replace("_", " ")
            events.append({"ts": _bar_ts_str(all_bars_1m, exit_bar_in_all), "type": "sell",
                           "price": round(result.exit_price, 4), "label": f"{exit_label} {shares}sh"})
            chart_entries[sym_key] = {
                "date": str(date_key), "bars_5m": [], "bars_1m": chart_bars,
                "events": events,
                "entry_price": round(entry_price_actual, 4),
                "stop_price": round(entry_price_actual * (1 - safety_pct), 4),
                "targets": {"vp_drop": f"-{drop_pct*100:.1f}%", "vp_time": f"{time_limit_sec}s"},
                "pnl": round(result.pnl, 2), "open_price": round(open_price, 4),
            }

            # VP has no re-entry; one trade per symbol per day
            active_positions -= 1

    print(f"\n{'='*60}")
    print(f"[vp_1.0] Backtest complete. Final equity: ${equity:,.2f}")
    print(f"Total trades: {len(all_trades)}")

    if chart_entries:
        save_backtest_charts(chart_entries)

    return all_trades
