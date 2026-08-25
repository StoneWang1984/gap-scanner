"""Backtesting engine — stonewang_daytrade_1025out_1.0: RTG + 10:25 Exit.

Entry detection (1-min bars, approximates live WebSocket bar stream):
  Signal A (Red-to-Green):
    - bar[i].close > open_price (crossed back above open)
    - bar[i].volume >= RTG_VOLUME_MULT × bar[i-1].volume (volume spike)
    - bar[i].volume >= RTG_MIN_VOLUME (liquidity floor)
  Signal B (Gap-and-Go): DISABLED

Entry window: 09:30 - 10:24 EST (before 10:25 exit)
One trade per symbol per day (no re-entry).

Exit: 10:25 EST market sell or 3% hard stop loss.
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


_CRYPTO_ETF_NAMES = {"BITX", "BITU", "XRPI", "UXRP", "XRPC", "XRPZ", "BTF", "BTFG",
                      "XRP", "ETHW", "SOLX", "DEFI", "BKCH", "CRPT", "STCE"}
_CRYPTO_ETF_PREFIXES = ("XRP", "BTC", "BIT", "ETH", "SOL", "DOGE", "LTC", "ADA")

def is_crypto_etf(symbol: str) -> bool:
    if symbol in _CRYPTO_ETF_NAMES:
        return True
    if any(symbol.startswith(k) and len(symbol) <= 6 for k in _CRYPTO_ETF_PREFIXES):
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


def find_rtg_entry_1min(bars_1m, open_price, min_volume=None):
    """Find RTG or Gap-and-Go entry on 1-min bars.

    Returns (entry_price, entry_bar_idx, confirmed, signal_type).
    """
    if bars_1m.empty or len(bars_1m) < 2:
        return 0.0, -1, False, ""
    if min_volume is None:
        min_volume = config.RTG_MIN_VOLUME

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
                and bar_vol >= min_volume):
            # First entry: at open_price + 0.1% (better price, matches live)
            # Re-entry: at signal bar close (market price at time of signal)
            # Caller decides based on entry_count which price to use
            entry_at_open = round(open_price * 1.001, 4)
            entry_at_close = round(bar_close * 1.001, 4)
            return entry_at_open, entry_at_close, i, True, "rtg"

        # Signal B: Gap-and-Go (2-bar breakout)
        # Prior bar bullish (close > open)
        # This bar breaks prior bar's high
        # Both bars have minimum volume
        if (i >= 1
                and prev_close > prev_open
                and prev_vol >= config.GAPGO_MIN_FIRST_BAR_VOL
                and bar_high > prev_high
                and bar_vol >= config.GAPGO_MIN_BREAKOUT_VOL):
            entry_at_open = round(prev_high, 4)
            entry_at_close = round(bar_high, 4)
            return entry_at_open, entry_at_close, i, True, "gapgo"

    return 0.0, 0.0, -1, False, ""


def _get_rvol_tier(rvol):
    tiers = getattr(config, "RVOL_SIZING_TIERS", [(10.0, 0.50), (5.0, 0.30), (0.0, 0.15)])
    for rvol_min, pct in tiers:
        if rvol >= rvol_min:
            return rvol_min, pct
    return 0.0, 0.15


def get_rvol_sizing(rvol, equity, same_tier_count=1):
    """Get position size based on RVOL-weighted tiers, split among same-tier candidates."""
    _, pct = _get_rvol_tier(rvol)
    split_pct = pct / max(same_tier_count, 1)
    return round(equity * split_pct, 2)


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

    print(f"[1025out_1.0] Backtesting {len(trading_days)} trading days: "
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
    print(f"  Exit: {getattr(config, 'EXIT_TIME', '10:00')} market sell or {getattr(config, 'STOP_LOSS_PCT', 0.03):.0%} stop loss")
    print(f"  Re-entry: {'ON (max ' + str(config.RTG_REENTRY_MAX) + ')' if getattr(config, 'RTG_REENTRY_ALLOWED', False) else 'OFF'}")

    print("\nLoading tradable symbols...")
    symbols = get_tradable_symbols()
    print(f"Found {len(symbols)} tradable symbols")

    symbols = [s for s in symbols if not is_leveraged_etf(s)]
    print(f"After leveraged ETF filter: {len(symbols)} symbols")

    symbols = [s for s in symbols if not is_crypto_etf(s)]
    print(f"After crypto ETF filter: {len(symbols)} symbols")

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
        # Backtest: raise daily trade limit to allow all candidates to trade concurrently
        bt_max_daily_trades = max(config.MAX_DAILY_TRADES, len(candidates) * 20)

        # Pre-compute same-tier counts for fair sizing split
        tier_counts = {}
        for _, r in candidates.iterrows():
            rvol_r = r.get("rvol", 0)
            tier_key = _get_rvol_tier(rvol_r)[0]
            tier_counts[tier_key] = tier_counts.get(tier_key, 0) + 1

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

            # RVOL-adaptive min volume: high RVOL relaxes liquidity floor
            min_vol = config.RTG_MIN_VOLUME
            if rvol >= 10:
                min_vol = max(config.RTG_MIN_VOLUME // 3, 5000)
            elif rvol >= 5:
                min_vol = max(config.RTG_MIN_VOLUME // 2, 10000)

            entry_slippage = getattr(config, "SLIPPAGE_ENTRY_PCT", 0.005)
            same_tier = tier_counts.get(_get_rvol_tier(rvol)[0], 1)
            stop_p = getattr(config, "STOP_LOSS_PCT", 0.03)  # Fixed 3% stop loss
            exit_time = getattr(config, "EXIT_TIME", "10:00")  # 10:00 exit
            exit_h, exit_m = (int(x) for x in exit_time.split(":"))
            sym_key = f"{symbol} ({date_key})"
            chart_bars = _bars_to_chart(bars_1m)
            events = chart_entries.get(sym_key, {}).get("events", [])

            # Bar-by-bar simulation: scan for RTG, enter, exit, continue scanning
            search_start = 0
            last_exit_reason = None
            _stop_cooldown_bar = 0
            while True:
                if bt_max_daily_trades > 0 and daily_trades >= bt_max_daily_trades:
                    break
                if max_daily_loss > 0 and daily_loss <= -max_daily_loss:
                    print(f"  Daily loss ${daily_loss:,.2f} exceeded limit, stopping for day")
                    break
                if entry_count.get(symbol, 0) > config.RTG_REENTRY_MAX:
                    break

                # Find next RTG signal after search_start
                if search_start >= len(bars_1m):
                    break
                entry_at_open, entry_at_close, entry_bar_idx, confirmed, signal_type = find_rtg_entry_1min(
                    bars_1m.iloc[search_start:], open_price, min_volume=min_vol)
                if not confirmed or entry_at_open <= 0:
                    break  # No more RTG signals for this symbol

                # Use open_price entry for first entry, close price for re-entry
                entries_for_sym_pre = entry_count.get(symbol, 0)
                is_reentry_bt = entries_for_sym_pre > 0
                entry_price = entry_at_close

                # Re-entry rules (match live_trade.py)
                if is_reentry_bt:
                    # Stop-loss = setup failed → no re-entry
                    if last_exit_reason == "stop_loss":
                        break
                    # Max re-entries per stock
                    if entries_for_sym_pre > getattr(config, "RTG_REENTRY_MAX", 1):
                        break
                    # Don't chase: re-entry price must be < 115% of open
                    max_re = open_price * getattr(config, "REENTRY_MAX_PRICE_VS_OPEN", 1.15)
                    if entry_price > max_re:
                        search_start = search_start + max(entry_bar_idx, 0) + 1
                        continue
                    # Must pull back >=3% from day high
                    min_pb = getattr(config, "REENTRY_MIN_PULLBACK", 0.03)
                    abs_bar = search_start + max(entry_bar_idx, 0)
                    if abs_bar < len(all_bars_1m):
                        day_h = max(b["high"] for b in all_bars_1m[:abs_bar + 1])
                        if entry_price > day_h * (1 - min_pb):
                            search_start = abs_bar + 1
                            continue

                # Stop-loss cooldown: skip 3 bars after stop_loss exit
                if last_exit_reason == "stop_loss" and entry_bar_idx < _stop_cooldown_bar:
                    search_start = _stop_cooldown_bar
                    continue

                if bt_max_daily_trades > 0 and daily_trades >= bt_max_daily_trades:
                    break
                if max_daily_loss > 0 and daily_loss <= -max_daily_loss:
                    break

                # Adjust entry_bar_idx to absolute position
                entry_bar_idx = search_start + entry_bar_idx

                entries_for_sym = entries_for_sym_pre
                is_reentry = entries_for_sym > 0
                entry_price_actual = round(entry_price * (1 + entry_slippage), 4)

                # RVOL-weighted position sizing
                if is_reentry:
                    reentry_pct = getattr(config, "RTG_REENTRY_SIZE_PCT", 0.50)
                    pos_size = get_rvol_sizing(rvol, equity, same_tier_count=same_tier) * reentry_pct
                else:
                    pos_size = get_rvol_sizing(rvol, equity, same_tier_count=same_tier)
                pos_size = max(config.MIN_POSITION_SIZE, pos_size)
                shares = int(pos_size / entry_price_actual)
                if shares <= 0:
                    search_start = entry_bar_idx + 1
                    continue

                remaining_list = all_bars_1m[entry_bar_idx + 1:]
                force_close_price = remaining_list[-1]["close"] if remaining_list else entry_price_actual

                # 1025out exit: 3% stop loss or 10:25 time-based exit
                exit_price = 0.0
                exit_reason = "force_close"
                exit_bar_idx = len(remaining_list) - 1
                stop_price = entry_price_actual * (1 - stop_p)
                for bi in range(len(remaining_list)):
                    rb = remaining_list[bi]
                    rb_ts = rb["ts"]
                    rb_h, rb_m = (int(x) for x in rb_ts.split(":"))
                    bar_low = rb["l"]
                    bar_close = rb["c"]
                    # Stop loss
                    if bar_low <= stop_price:
                        exit_price = stop_price
                        exit_reason = "stop_loss"
                        exit_bar_idx = bi
                        break
                    # 10:25 time exit
                    if rb_h > exit_h or (rb_h == exit_h and rb_m >= exit_m):
                        exit_price = bar_close
                        exit_reason = "10:25_exit"
                        exit_bar_idx = bi
                        break
                if exit_price == 0.0:
                    exit_price = force_close_price
                    exit_reason = "force_close"

                pnl = (exit_price - entry_price_actual) * shares
                pnl_pct = exit_price / entry_price_actual - 1

                result = TradeResult(
                    symbol=symbol,
                    entry_price=entry_price_actual,
                    exit_price=round(exit_price, 4),
                    shares=shares,
                    pnl=round(pnl, 2),
                    pnl_pct=pnl_pct,
                    exit_reason=exit_reason,
                    exit_bar_idx=exit_bar_idx,
                    stop_price=round(stop_price, 4),
                    target_price=0.0,
                    signal_type=signal_type + ("_re" if is_reentry else ""),
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
                      f"[RVOL={rvol:.1f}× stop={stop_p:.0%}]")

                all_trades.append(result)
                equity += result.pnl
                daily_loss += result.pnl
                daily_trades += 1
                entry_count[symbol] = entries_for_sym + 1

                events.append({"ts": entry_ts, "type": "buy", "price": entry_price_actual,
                               "label": f"BUY {shares}sh [{label}]"})
                events.append({"ts": exit_ts, "type": "sell", "price": result.exit_price,
                               "label": f"{result.exit_reason.upper()} {shares}sh"})

                # After exit, set cooldown and continue
                search_start = exit_bar + 1
                last_exit_reason = result.exit_reason
                if result.exit_reason == "stop_loss":
                    _stop_cooldown_bar = exit_bar + 3  # 3-bar cooldown after stop
                else:
                    _stop_cooldown_bar = 0

            # Save chart data for this symbol
            if events:
                chart_entries[sym_key] = {
                    "date": str(date_key), "bars_1m": chart_bars, "events": events,
                    "entry_price": entry_price_actual,
                    "stop_price": result.stop_price, "target_price": result.target_price,
                    "pnl": sum(t.pnl for t in all_trades if t.symbol == symbol and t.date == str(date_key)),
                    "open_price": open_price, "signal": signal_type,
                }

    print(f"\n{'=' * 70}")
    print(f"[1025out_1.0] Backtest complete. Final equity: ${equity:,.2f}")
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
