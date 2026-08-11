"""Backtesting engine — stonewang_daytrade_rtg_1.0: Red-to-Green Volume Breakout.

Entry detection (1-min bars, approximates live WebSocket bar stream):
  Signal A (Red-to-Green):
    - bar[i].close > open_price (crossed back above open)
    - bar[i].volume >= RTG_VOLUME_MULT × bar[i-1].volume (volume spike)
    - bar[i].volume >= RTG_MIN_VOLUME (liquidity floor)
  Signal B (Gap-and-Go):
    - bar[i-1].close > bar[i-1].open (prior bar bullish)
    - bar[i].high > bar[i-1].high (breakout)
    - bar[i-1].volume >= GAPGO_MIN_FIRST_BAR_VOL
    - bar[i].volume >= GAPGO_MIN_BREAKOUT_VOL

Entry window: 09:30 - 10:30 EST (1 hour)
One trade per symbol per day (no re-entry — keeps logic simple for first version).

Exit (evaluate_trade_rtg):
  3% stop, 10% target, 10-min time limit, 3% trailing after +5%.
"""

import json
import os
import re
import sys
import importlib.util

# Load version-specific config (must be before `import config`)
_ver_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_ver_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)
_spec = importlib.util.spec_from_file_location("config", os.path.join(_ver_dir, "config.py"))
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)
sys.modules["config"] = config

import pandas as pd
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import Adjustment, DataFeed

import config

# Load scanner from parent directory
from scanner import get_data_client, get_tradable_symbols

# Load strategy from rtg_1.0 directory
_strat_spec = importlib.util.spec_from_file_location("strategy", os.path.join(_ver_dir, "strategy.py"))
strategy = importlib.util.module_from_spec(_strat_spec)
_strat_spec.loader.exec_module(strategy)
sys.modules["strategy"] = strategy
evaluate_trade_rtg = strategy.evaluate_trade_rtg
TradeResult = strategy.TradeResult


# ── Leveraged ETF filter ─────────────────────────────────────────────
_LEV_PATTERN = re.compile(r'(2X|3X|BULL|BEAR)$', re.IGNORECASE)
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
    if symbol.endswith(("BULL", "BEAR")):
        return True
    if any(symbol.startswith(p) for p in _LEV_PREFIXES):
        return True
    return False


def get_trading_days(client, end_date, n_days):
    start = end_date - pd.Timedelta(days=n_days * 2 + 10)
    request = StockBarsRequest(
        symbol_or_symbols="SPY", timeframe=TimeFrame.Day,
        start=start, end=end_date, adjustment=Adjustment.RAW,
        feed=getattr(config, "DATA_FEED_OBJ", DataFeed.IEX),
    )
    bars = client.get_stock_bars(request)
    if bars.df.empty:
        return []
    df = bars.df
    dates = sorted(set(df.index.get_level_values("timestamp").date))
    return [pd.Timestamp(d) for d in dates[-n_days:]]


def bulk_scan_gaps(client, trading_days, symbols):
    """Scan for gap-up stocks across all trading days. Also fetches 20-day avg volume for RVOL."""
    start = trading_days[0] - pd.Timedelta(days=45)  # extra lookback for 20-day avg vol
    end = trading_days[-1] + pd.Timedelta(days=1)
    all_dates_set = {d.date() for d in trading_days}

    batch_size = 500
    symbol_data = {}
    total_batches = (len(symbols) + batch_size - 1) // batch_size

    for batch_idx in range(total_batches):
        batch = symbols[batch_idx * batch_size: (batch_idx + 1) * batch_size]
        if batch_idx % 5 == 0:
            print(f"  Bulk scanning batch {batch_idx + 1}/{total_batches}...")

        request = StockBarsRequest(
            symbol_or_symbols=batch, timeframe=TimeFrame.Day,
            start=start, end=end, adjustment=Adjustment.RAW,
            feed=getattr(config, "DATA_FEED_OBJ", DataFeed.IEX),
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
                        ts = idx_val[1] if hasattr(idx_val[1], "date") else pd.Timestamp(idx_val[1])
                    else:
                        ts = pd.Timestamp(idx_val) if not hasattr(idx_val, "date") else idx_val
                    curr_date = ts.date()
                    if curr_date not in all_dates_set:
                        continue
                    prev_close = float(prev["close"])
                    open_price = float(curr["open"])
                    volume = int(prev["volume"])
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
                    # RVOL: 20-day average volume
                    lookback_start = max(0, i - config.RVOL_LOOKBACK_DAYS - 1)
                    prior_vols = [int(sym_df.iloc[j]["volume"]) for j in range(lookback_start, i)]
                    avg_vol_20d = sum(prior_vols) / len(prior_vols) if prior_vols else 0
                    rvol = volume / avg_vol_20d if avg_vol_20d > 0 else 0

                    if symbol not in symbol_data:
                        symbol_data[symbol] = []
                    symbol_data[symbol].append({
                        "date": curr_date, "open_price": open_price,
                        "prev_close": prev_close, "gap_pct": gap_pct,
                        "volume": volume, "dollar_volume": dollar_volume,
                        "rvol": rvol,
                    })
            except (KeyError, IndexError):
                continue

    # Group by date, sort by RVOL descending (top candidates = highest RVOL)
    results = {}
    for symbol, entries in symbol_data.items():
        for entry in entries:
            d = entry["date"]
            if d not in results:
                results[d] = []
            results[d].append({**entry, "symbol": symbol})

    for d in results:
        df_d = pd.DataFrame(results[d])
        df_d = df_d.sort_values("rvol", ascending=False).reset_index(drop=True)
        results[d] = df_d

    return results


def get_1min_bars(client, symbol, date):
    market_open = pd.Timestamp(f"{date.date()} {config.MARKET_OPEN}", tz="America/New_York")
    market_close = pd.Timestamp(f"{date.date()} {config.MARKET_CLOSE}", tz="America/New_York")
    request = StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=market_open, end=market_close, adjustment=Adjustment.RAW,
        feed=getattr(config, "DATA_FEED_OBJ", DataFeed.IEX),
    )
    bars = client.get_stock_bars(request)
    if bars.df.empty:
        return pd.DataFrame()
    return bars.df


def _bars_to_list(bars_df, start_idx=0):
    result = []
    for i in range(start_idx, len(bars_df)):
        bar = bars_df.iloc[i]
        idx = bars_df.index[i]
        ts = idx
        if isinstance(idx, tuple):
            ts = idx[1]
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        ts = ts.tz_convert("America/New_York")
        result.append({
            "high": float(bar["high"]), "low": float(bar["low"]),
            "close": float(bar["close"]), "open": float(bar["open"]),
            "volume": int(bar["volume"]) if "volume" in bar.index else 0,
            "timestamp": ts,
        })
    return result


def _bar_ts_str(bars_list, idx):
    if 0 <= idx < len(bars_list):
        return bars_list[idx]["timestamp"].strftime("%H:%M")
    return "00:00"


def _bars_to_chart(bars_df):
    result = []
    for i in range(len(bars_df)):
        bar = bars_df.iloc[i]
        idx = bars_df.index[i]
        ts = idx[1] if isinstance(idx, tuple) else idx
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        ts = ts.tz_convert("America/New_York")
        result.append({
            "ts": ts.strftime("%H:%M"),
            "o": round(float(bar["open"]), 4), "h": round(float(bar["high"]), 4),
            "l": round(float(bar["low"]), 4), "c": round(float(bar["close"]), 4),
            "v": int(bar["volume"]) if "volume" in bar.index else 0,
        })
    return result


def find_rtg_entry_1min(bars_1m, open_price):
    """Find RTG or Gap-and-Go entry on 1-min bars.

    Returns (entry_price, entry_bar_idx, confirmed, signal_type).
    """
    if bars_1m.empty or len(bars_1m) < 2:
        return 0.0, -1, False, ""

    entry_start_str = getattr(config, "ENTRY_WINDOW_START", "09:30")
    entry_end_str = getattr(config, "ENTRY_WINDOW_END", "10:30")

    for i in range(1, len(bars_1m)):
        idx_val = bars_1m.index[i]
        ts = idx_val[1] if isinstance(idx_val, tuple) else idx_val
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        ts = ts.tz_convert("America/New_York")

        bar_time = ts.time()
        start_time = pd.Timestamp(f"{ts.date()} {entry_start_str}", tz="America/New_York").time()
        end_time = pd.Timestamp(f"{ts.date()} {entry_end_str}", tz="America/New_York").time()
        if not (start_time <= bar_time <= end_time):
            continue

        bar = bars_1m.iloc[i]
        prev_bar = bars_1m.iloc[i - 1]

        bar_open = float(bar["open"])
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bar_close = float(bar["close"])
        bar_vol = int(bar["volume"])
        prev_vol = int(prev_bar["volume"])
        prev_high = float(prev_bar["high"])
        prev_open = float(prev_bar["open"])
        prev_close = float(prev_bar["close"])

        # Signal A: Red-to-Green
        # Bar close crossed above open_price (stock was red, now green)
        # Volume spike: bar vol >= RTG_VOLUME_MULT × prior bar vol
        # Liquidity floor: bar vol >= RTG_MIN_VOLUME
        if (bar_close > open_price
                and prev_vol > 0
                and bar_vol >= config.RTG_VOLUME_MULT * prev_vol
                and bar_vol >= config.RTG_MIN_VOLUME):
            # Entry at open_price level (not signal bar close) for better price
            if getattr(config, "RTG_ENTRY_AT_OPEN", True):
                entry = round(open_price * 1.001, 4)  # open_price + 0.1% buffer
            else:
                entry = round(bar_close, 4)
            return entry, i, True, "rtg"

        # Signal B: Gap-and-Go (2-bar breakout)
        # Prior bar bullish (close > open)
        # This bar breaks prior bar's high
        # Both bars have minimum volume
        if (i >= 1
                and prev_close > prev_open
                and prev_vol >= config.GAPGO_MIN_FIRST_BAR_VOL
                and bar_high > prev_high
                and bar_vol >= config.GAPGO_MIN_BREAKOUT_VOL):
            return round(prev_high, 4), i, True, "gapgo"

    return 0.0, -1, False, ""


def get_rvol_sizing(rvol, equity):
    """Get position size based on RVOL-weighted tiers."""
    tiers = getattr(config, "RVOL_SIZING_TIERS", [(10.0, 0.50), (5.0, 0.30), (0.0, 0.15)])
    for rvol_min, pct in tiers:
        if rvol >= rvol_min:
            return round(equity * pct, 2)
    return round(equity * 0.15, 2)


def get_rvol_exit_params(rvol):
    """Get adaptive exit params based on RVOL tier.
    Returns (stop_pct, target_pct, trail_activate_pct, trail_pct).
    """
    tiers = getattr(config, "RVOL_EXIT_TIERS", [
        (10.0, 0.07, 0.30, 0.05, 0.03),
        (5.0,  0.05, 0.20, 0.05, 0.03),
        (0.0,  0.03, 0.10, 0.04, 0.02),
    ])
    for rvol_min, stop, target, trail_act, trail in tiers:
        if rvol >= rvol_min:
            return stop, target, trail_act, trail
    return 0.05, 0.20, 0.05, 0.03


def save_backtest_charts(chart_entries, filepath="versions/chart_data_rtg.json"):
    date_parts = sorted(set(v["date"] for v in chart_entries.values()))
    date_range = f"{date_parts[0]} to {date_parts[-1]}" if len(date_parts) > 1 else date_parts[0]
    output = {"date": date_range, "symbols": chart_entries}
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Chart data saved to {filepath} ({len(chart_entries)} symbols)")


def run_backtest(end_date=None, n_days=None):
    if n_days is None:
        n_days = config.BACKTEST_DAYS
    client = get_data_client()
    if end_date is None:
        end_date = pd.Timestamp.now(tz="America/New_York")

    trading_days = get_trading_days(client, end_date, n_days)
    if not trading_days:
        print("No trading days found.")
        return []

    print(f"[rtg_1.0] Backtesting {len(trading_days)} trading days: "
          f"{trading_days[0].date()} to {trading_days[-1].date()}")
    print(f"Capital: ${config.INITIAL_CAPITAL:,.2f} | RVOL-weighted sizing | "
          f"Max concurrent: {config.MAX_POSITIONS} | Max daily trades: {config.MAX_DAILY_TRADES}")
    print(f"Entry window: {config.ENTRY_WINDOW_START}-{config.ENTRY_WINDOW_END} EST")
    print(f"  Signal A (RTG): close > open AND vol >= {config.RTG_VOLUME_MULT}× prior AND vol >= {config.RTG_MIN_VOLUME:,}")
    print(f"  Signal B (GapGo): DISABLED")
    sizing_tiers = getattr(config, "RVOL_SIZING_TIERS", [])
    if sizing_tiers:
        print(f"  Sizing tiers: " + ", ".join(f"RVOL>{r:.0f}×→{p:.0%}" for r, p in sizing_tiers))
    exit_tiers = getattr(config, "RVOL_EXIT_TIERS", [])
    if exit_tiers:
        print(f"  Exit tiers: " + ", ".join(
            f"RVOL>{r:.0f}×→stop{s:.0%}/tgt{t:.0%}/trail{a:.0%}/{tr:.0%}"
            for r, s, t, a, tr in exit_tiers))
    print(f"  Re-entry: {'ON (max ' + str(config.RTG_REENTRY_MAX) + ')' if getattr(config, 'RTG_REENTRY_ALLOWED', False) else 'OFF'}")

    print("\nLoading tradable symbols...")
    symbols = get_tradable_symbols()
    print(f"Found {len(symbols)} tradable symbols")

    symbols = [s for s in symbols if not is_leveraged_etf(s)]
    print(f"After leveraged ETF filter: {len(symbols)} symbols")

    print("\nBulk scanning for gaps (with RVOL)...")
    gap_data = bulk_scan_gaps(client, trading_days, symbols)
    total_candidates = sum(len(v) for v in gap_data.values())
    print(f"Found {total_candidates} gap entries across {len(gap_data)} days")

    all_trades = []
    equity = config.INITIAL_CAPITAL
    chart_entries = {}

    for date in trading_days:
        date_key = date.date()
        if date_key not in gap_data or gap_data[date_key].empty:
            continue

        candidates = gap_data[date_key]
        # Select top N by RVOL
        max_cands = getattr(config, "MAX_CANDIDATES", 5)
        candidates = candidates.head(max_cands)

        print(f"\n--- {date_key} ({len(candidates)} candidates by RVOL, equity: ${equity:,.2f}) ---")
        for _, row in candidates.iterrows():
            sym = row["symbol"]
            open_price = row["open_price"]
            rvol = row.get("rvol", 0)
            gap_pct = row["gap_pct"]
            print(f"  {sym} gap={gap_pct:+.1%} RVOL={rvol:.1f}× open=${open_price:.2f}")

        daily_trades = 0
        daily_loss = 0.0
        max_daily_loss = equity * config.MAX_DAILY_LOSS_PCT
        entry_count = {}  # symbol -> count of entries (for re-entry tracking)
        cached_bars = {}  # symbol -> (bars_df, bars_list)

        for _, row in candidates.iterrows():
            symbol = row["symbol"]
            open_price = row["open_price"]
            rvol = row.get("rvol", 0)

            # Fetch and cache bars for this symbol
            if symbol not in cached_bars:
                bars_1m = get_1min_bars(client, symbol, date)
                if bars_1m.empty or len(bars_1m) < 2:
                    cached_bars[symbol] = (None, None)
                    continue
                cached_bars[symbol] = (bars_1m, _bars_to_list(bars_1m))
            bars_1m, all_bars_1m = cached_bars[symbol]
            if bars_1m is None:
                continue

            # First entry
            entries_for_sym = entry_count.get(symbol, 0)
            if entries_for_sym >= 1 + getattr(config, "RTG_REENTRY_MAX", 0):
                continue

            # Find entry signal
            start_bar = 0  # for re-entry, start search after previous exit
            entry_price, entry_bar_idx, confirmed, signal_type = find_rtg_entry_1min(
                bars_1m.iloc[start_bar:] if start_bar > 0 else bars_1m, open_price)
            if not confirmed or entry_price <= 0:
                continue

            if config.MAX_DAILY_TRADES > 0 and daily_trades >= config.MAX_DAILY_TRADES:
                break
            if max_daily_loss > 0 and daily_loss <= -max_daily_loss:
                print(f"  Daily loss ${daily_loss:,.2f} exceeded limit, stopping for day")
                break

            entry_slippage = getattr(config, "SLIPPAGE_ENTRY_PCT", 0.005)
            entry_price_actual = round(entry_price * (1 + entry_slippage), 4)

            # RVOL-weighted position sizing
            is_reentry = entries_for_sym > 0
            if is_reentry:
                reentry_pct = getattr(config, "RTG_REENTRY_SIZE_PCT", 0.50)
                pos_size = get_rvol_sizing(rvol, equity) * reentry_pct
            else:
                pos_size = get_rvol_sizing(rvol, equity)
            pos_size = max(config.MIN_POSITION_SIZE, pos_size)
            shares = int(pos_size / entry_price_actual)
            if shares <= 0:
                continue

            # Adaptive exit parameters based on RVOL
            stop_p, target_p, trail_act_p, trail_p = get_rvol_exit_params(rvol)

            remaining_list = all_bars_1m[entry_bar_idx + 1:]
            force_close_price = remaining_list[-1]["close"] if remaining_list else entry_price_actual

            result = evaluate_trade_rtg(
                entry_price=entry_price_actual,
                shares=shares,
                bars_after_entry=remaining_list,
                symbol=symbol,
                open_price=open_price,
                force_close_price=force_close_price,
                entry_bar_idx=entry_bar_idx,
                signal_type=signal_type + ("_re" if is_reentry else ""),
                stop_pct=stop_p,
                target_pct=target_p,
                trail_activate_pct=trail_act_p,
                trail_pct=trail_p,
            )
            result.date = str(date_key)
            result.open_price = open_price

            entry_ts = _bar_ts_str(all_bars_1m, entry_bar_idx)
            exit_bar = entry_bar_idx + 1 + result.exit_bar_idx
            exit_ts = _bar_ts_str(all_bars_1m, exit_bar)
            label = f"{signal_type}_re" if is_reentry else signal_type
            print(f"  {symbol} [{label}] entry=${entry_price_actual:.4f}@{entry_ts} "
                  f"exit=${result.exit_price:.4f}@{exit_ts} ({result.exit_reason}), "
                  f"P&L=${result.pnl:+,.2f} ({result.pnl_pct:+.2%}) "
                  f"[RVOL={rvol:.1f}× stop={stop_p:.0%} tgt={target_p:.0%}]")

            all_trades.append(result)
            equity += result.pnl
            daily_loss += result.pnl
            daily_trades += 1
            entry_count[symbol] = entries_for_sym + 1

            # Chart data
            sym_key = f"{symbol} ({date_key})"
            chart_bars = _bars_to_chart(bars_1m)
            events = chart_entries.get(sym_key, {}).get("events", [])
            events.append({"ts": entry_ts, "type": "buy", "price": entry_price_actual,
                           "label": f"BUY {shares}sh [{label}]"})
            events.append({"ts": exit_ts, "type": "sell", "price": result.exit_price,
                           "label": f"{result.exit_reason.upper()} {shares}sh"})
            chart_entries[sym_key] = {
                "date": str(date_key), "bars_1m": chart_bars, "events": events,
                "entry_price": entry_price_actual,
                "stop_price": result.stop_price, "target_price": result.target_price,
                "pnl": result.pnl, "open_price": open_price, "signal": label,
            }

            # Re-entry: if profitable exit (trail_stop or target), check for new RTG signal
            if (getattr(config, "RTG_REENTRY_ALLOWED", False)
                    and result.exit_reason in ("trail_stop", "target")
                    and entry_count.get(symbol, 0) <= config.RTG_REENTRY_MAX
                    and daily_trades < config.MAX_DAILY_TRADES):
                reentry_start = exit_bar + 1
                if reentry_start < len(bars_1m):
                    re_bars = bars_1m.iloc[reentry_start:]
                    re_entry, re_idx, re_conf, re_sig = find_rtg_entry_1min(re_bars, open_price)
                    if re_conf and re_entry > 0:
                        re_entry_actual = round(re_entry * (1 + entry_slippage), 4)
                        re_size = max(config.MIN_POSITION_SIZE, get_rvol_sizing(rvol, equity) * getattr(config, "RTG_REENTRY_SIZE_PCT", 0.50))
                        re_shares = int(re_size / re_entry_actual)
                        if re_shares > 0:
                            re_bar_idx = reentry_start + re_idx
                            re_remaining = all_bars_1m[re_bar_idx + 1:]
                            re_force = re_remaining[-1]["close"] if re_remaining else re_entry_actual
                            re_result = evaluate_trade_rtg(
                                entry_price=re_entry_actual, shares=re_shares,
                                bars_after_entry=re_remaining, symbol=symbol,
                                open_price=open_price, force_close_price=re_force,
                                entry_bar_idx=re_bar_idx, signal_type=signal_type + "_re",
                                stop_pct=stop_p, target_pct=target_p,
                                trail_activate_pct=trail_act_p, trail_pct=trail_p,
                            )
                            re_result.date = str(date_key)
                            re_result.open_price = open_price
                            re_entry_ts = _bar_ts_str(all_bars_1m, re_bar_idx)
                            re_exit_bar = re_bar_idx + 1 + re_result.exit_bar_idx
                            re_exit_ts = _bar_ts_str(all_bars_1m, re_exit_bar)
                            print(f"  {symbol} [{signal_type}_re] entry=${re_entry_actual:.4f}@{re_entry_ts} "
                                  f"exit=${re_result.exit_price:.4f}@{re_exit_ts} ({re_result.exit_reason}), "
                                  f"P&L=${re_result.pnl:+,.2f} ({re_result.pnl_pct:+.2%})")
                            all_trades.append(re_result)
                            equity += re_result.pnl
                            daily_loss += re_result.pnl
                            daily_trades += 1
                            entry_count[symbol] = entry_count.get(symbol, 0) + 1
                            events.append({"ts": re_entry_ts, "type": "buy", "price": re_entry_actual,
                                           "label": f"BUY {re_shares}sh [{signal_type}_re]"})
                            events.append({"ts": re_exit_ts, "type": "sell", "price": re_result.exit_price,
                                           "label": f"{re_result.exit_reason.upper()} {re_shares}sh"})
                            chart_entries[sym_key] = {
                                "date": str(date_key), "bars_1m": chart_bars, "events": events,
                                "entry_price": re_entry_actual,
                                "stop_price": re_result.stop_price, "target_price": re_result.target_price,
                                "pnl": chart_entries[sym_key]["pnl"] + re_result.pnl,
                                "open_price": open_price, "signal": signal_type,
                            }

    print(f"\n{'=' * 70}")
    print(f"[rtg_1.0] Backtest complete. Final equity: ${equity:,.2f}")
    print(f"Total trades: {len(all_trades)}")
    if all_trades:
        wins = [t for t in all_trades if t.pnl > 0]
        losses = [t for t in all_trades if t.pnl <= 0]
        win_rate = len(wins) / len(all_trades)
        avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.pnl for t in losses) / len(losses) if losses else 0
        total_pnl = sum(t.pnl for t in all_trades)
        print(f"Win rate: {win_rate:.1%} ({len(wins)}W / {len(losses)}L)")
        print(f"Avg win: ${avg_win:+,.2f} | Avg loss: ${avg_loss:+,.2f}")
        print(f"Total P&L: ${total_pnl:+,.2f} ({total_pnl/config.INITIAL_CAPITAL:+.1%})")
        # Exit reason breakdown
        from collections import Counter
        reasons = Counter(t.exit_reason for t in all_trades)
        print(f"Exit reasons: {dict(reasons)}")
        # Signal type breakdown
        sigs = Counter(t.signal_type for t in all_trades)
        print(f"Signal types: {dict(sigs)}")

    if chart_entries:
        save_backtest_charts(chart_entries)

    return all_trades


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=config.BACKTEST_DAYS)
    parser.add_argument("--end", type=str, default=None)
    args = parser.parse_args()
    end = pd.Timestamp(args.end, tz="America/New_York") if args.end else None
    run_backtest(end_date=end, n_days=args.days)
