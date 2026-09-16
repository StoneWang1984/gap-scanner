"""Backtesting engine — stonewang_daytrade_rtg_7.0: ORB + ATR Stops + Progressive Trail + Range-High Failed-Entry.

Entry detection:
  Opening Range Breakout (ORB):
    - Wait ORB_BARS (3) one-min bars to establish opening range
    - Enter on breakout above range high with 0.2% buffer
    - Min opening range width 0.5% (skip flat opens)
    - Only uses bars at/after 09:30 (filters pre-market)
  Fallback: Red-to-Green volume breakout (if ORB disabled/invalid)

Entry window: 09:30 - 10:30 EST (1 hour)

Exit (rtg_7.0):
  ATR-based stops + gap expansion + progressive trailing + failed-entry (price < range_high).
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

# Load strategy from rtg_7.0 directory (v7.0: ATR stops + progressive trail + range-high failed-entry)
_strat_spec = importlib.util.spec_from_file_location("strategy", os.path.join(_ver_dir, "strategy_v6.py"))
strategy = importlib.util.module_from_spec(_strat_spec)
_strat_spec.loader.exec_module(strategy)
sys.modules["strategy"] = strategy
evaluate_trade_rtg = strategy.evaluate_trade_rtg
TradeResult = strategy.TradeResult
calc_atr_stop = strategy.calc_atr_stop


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
    """Scan for gap-up stocks + afternoon momentum across all trading days."""
    start = trading_days[0] - pd.Timedelta(days=45)
    end = trading_days[-1] + pd.Timedelta(days=1)
    all_dates_set = {d.date() for d in trading_days}
    atr_period = getattr(config, "ATR_PERIOD", 14)

    batch_size = 500
    symbol_data = {}
    afternoon_data = {}  # symbol -> list of afternoon momentum entries
    aft_price_max = getattr(config, "AFTERNOON_PRICE_MAX", 200.0)
    aft_min_gain = getattr(config, "AFTERNOON_MIN_GAIN_PCT", 0.02)
    aft_min_rvol = getattr(config, "AFTERNOON_MIN_RVOL", 2.0)
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
                    gap_day_vol = int(curr["volume"])
                    prev_vol = int(prev["volume"])
                    if prev_close <= 0:
                        continue
                    gap_pct = (open_price / prev_close) - 1.0
                    if gap_pct < config.GAP_THRESHOLD:
                        continue
                    if gap_pct > getattr(config, "GAP_MAX", 1.0):
                        continue
                    if prev_vol < config.MIN_VOLUME:
                        continue
                    if not (config.PRICE_MIN <= open_price <= config.PRICE_MAX):
                        continue
                    dollar_volume = prev_close * prev_vol
                    if dollar_volume < config.MIN_DOLLAR_VOLUME:
                        continue
                    # RVOL: gap-day volume / 20-day average volume (prior days only)
                    lookback_start = max(0, i - config.RVOL_LOOKBACK_DAYS - 1)
                    prior_vols = [int(sym_df.iloc[j]["volume"]) for j in range(lookback_start, i)]
                    avg_vol_20d = sum(prior_vols) / len(prior_vols) if prior_vols else 0
                    rvol = gap_day_vol / avg_vol_20d if avg_vol_20d > 0 else 0

                    # ATR: 14-day average True Range (from daily bars before gap day)
                    atr = 0.0
                    if i >= 2:
                        tr_start = max(1, i - atr_period)
                        true_ranges = []
                        for j in range(tr_start, i):
                            c = sym_df.iloc[j]
                            p = sym_df.iloc[j - 1]
                            tr = max(
                                float(c["high"]) - float(c["low"]),
                                abs(float(c["high"]) - float(p["close"])),
                                abs(float(c["low"]) - float(p["close"])),
                            )
                            true_ranges.append(tr)
                        if true_ranges:
                            atr = sum(true_ranges) / len(true_ranges)

                    if symbol not in symbol_data:
                        symbol_data[symbol] = []
                    symbol_data[symbol].append({
                        "date": curr_date, "open_price": open_price,
                        "prev_close": prev_close, "gap_pct": gap_pct,
                        "volume": gap_day_vol, "dollar_volume": dollar_volume,
                        "rvol": rvol, "atr": atr,
                    })

                    # Afternoon momentum: gain from close > 2%, price $2-$200, RVOL > 2
                    curr_close = float(curr["close"])
                    gain_from_close = (curr_close - prev_close) / prev_close
                    if (gain_from_close >= aft_min_gain
                            and config.PRICE_MIN <= curr_close <= aft_price_max
                            and rvol >= aft_min_rvol):
                        if symbol not in afternoon_data:
                            afternoon_data[symbol] = []
                        afternoon_data[symbol].append({
                            "date": curr_date, "open_price": open_price,
                            "close_price": curr_close, "prev_close": prev_close,
                            "gap_pct": gain_from_close, "volume": gap_day_vol,
                            "rvol": rvol, "atr": atr,
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

    # Group afternoon data by date
    aft_results = {}
    for symbol, entries in afternoon_data.items():
        for entry in entries:
            d = entry["date"]
            if d not in aft_results:
                aft_results[d] = []
            aft_results[d].append({**entry, "symbol": symbol})

    for d in aft_results:
        df_d = pd.DataFrame(aft_results[d])
        df_d = df_d.sort_values("rvol", ascending=False).reset_index(drop=True)
        aft_results[d] = df_d

    return results, aft_results


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


def find_orb_entry_1min(bars_1m, open_price, min_volume=None):
    """Opening Range Breakout entry (rtg_7.0).
    Phase 1: build opening range from first ORB_BARS market-open bars.
    Phase 2: enter on breakout above range high with volume confirmation.
    Returns (entry_at_open, entry_at_close, entry_bar_idx, confirmed, signal_type, range_high).
    Only uses bars at or after market open (09:30) — filters pre-market bars.
    """
    import datetime as _dt
    orb_bars = getattr(config, "ORB_BARS", 3)
    if bars_1m.empty or len(bars_1m) < orb_bars + 1:
        return 0.0, 0.0, -1, False, "", 0.0
    if min_volume is None:
        min_volume = config.RTG_MIN_VOLUME

    # Filter: only keep bars at or after market open (09:30)
    mkt_open_time = _dt.time(9, 30)
    market_indices = []
    for i in range(len(bars_1m)):
        idx_val = bars_1m.index[i]
        ts = idx_val[1] if isinstance(idx_val, tuple) else idx_val
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        ts = ts.tz_convert("America/New_York")
        if ts.time() >= mkt_open_time:
            market_indices.append(i)

    if len(market_indices) < orb_bars + 1:
        return 0.0, 0.0, -1, False, "", 0.0

    entry_end_str = getattr(config, "ENTRY_WINDOW_END", "10:30")
    end_time = pd.Timestamp(f"2000-01-01 {entry_end_str}").time()

    # Phase 1: build opening range from first ORB_BARS market-open bars
    range_high = 0.0
    range_low = float('inf')
    for i in market_indices[:orb_bars]:
        bar = bars_1m.iloc[i]
        h = float(bar["high"])
        l = float(bar["low"])
        if h > range_high:
            range_high = h
        if l < range_low:
            range_low = l

    if range_high <= 0:
        return 0.0, 0.0, -1, False, "", 0.0

    range_width = (range_high - range_low) / range_high
    min_range = getattr(config, "ORB_MIN_RANGE_PCT", 0.005)

    # If range too narrow, fall back to RTG
    if range_width < min_range:
        result = find_rtg_entry_1min(bars_1m, open_price, min_volume=min_volume)
        # find_rtg_entry_1min returns (entry_at_open, entry_at_close, idx, confirmed, signal_type)
        # Append range_high=0.0 to match new signature
        return result + (0.0,)

    # Phase 2: breakout above range high with volume
    for j in range(orb_bars, len(market_indices)):
        i = market_indices[j]
        prev_i = market_indices[j - 1]
        idx_val = bars_1m.index[i]
        ts = idx_val[1] if isinstance(idx_val, tuple) else idx_val
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        ts = ts.tz_convert("America/New_York")

        bar_time = ts.time()
        if bar_time > end_time:
            continue

        bar = bars_1m.iloc[i]
        prev_bar = bars_1m.iloc[prev_i]
        bar_close = float(bar["close"])
        bar_vol = int(bar["volume"])
        prev_vol = int(prev_bar["volume"])

        # Breakout: close > range_high, volume spike
        if (bar_close > range_high
                and prev_vol > 0
                and bar_vol >= config.RTG_VOLUME_MULT * prev_vol
                and bar_vol >= min_volume):
            if getattr(config, "RTG_ENTRY_AT_OPEN", True):
                entry_at_open = round(open_price * 1.001, 4)
            else:
                buf = getattr(config, "ORB_BREAKOUT_BUFFER", 0.002)
                entry_at_open = round(range_high * (1 + buf), 4)
            entry_at_close = round(bar_close * 1.001, 4)
            return entry_at_open, entry_at_close, i, True, "orb_rtg", range_high

    return 0.0, 0.0, -1, False, "", 0.0


def find_momentum_entry_1min(bars_1m, min_volume=None):
    """Afternoon momentum breakout: price + volume both rising (量价齐升).

    Returns (entry_price, entry_bar_idx, confirmed, signal_type).
    """
    import datetime as _dt
    if bars_1m.empty or len(bars_1m) < 6:
        return 0.0, -1, False, ""
    if min_volume is None:
        min_volume = config.RTG_MIN_VOLUME

    aft_end_str = getattr(config, "AFTERNOON_ENTRY_END", "15:30")
    aft_end_h, aft_end_m = (int(x) for x in aft_end_str.split(":"))
    aft_end_time = _dt.time(aft_end_h, aft_end_m)
    aft_start_time = _dt.time(10, 30)

    for i in range(5, len(bars_1m)):
        idx_val = bars_1m.index[i]
        ts = idx_val[1] if isinstance(idx_val, tuple) else idx_val
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        ts = ts.tz_convert("America/New_York")
        bar_time = ts.time()
        if bar_time < aft_start_time or bar_time > aft_end_time:
            continue

        # Check last 5 bars
        recent = [bars_1m.iloc[j] for j in range(i - 5, i)]
        current = bars_1m.iloc[i]

        # Up bars: at least 3 of 5
        up_count = sum(1 for b in recent if float(b["close"]) > float(b["open"]))
        if up_count < 3:
            continue

        # Volume increasing
        vols = [int(b["volume"]) for b in recent]
        if vols[-1] < vols[0]:
            continue

        # Breakout above recent high
        recent_high = max(float(b["high"]) for b in recent)
        if float(current["close"]) <= recent_high:
            continue

        # Volume confirmation
        avg_vol = sum(vols) / len(vols)
        cur_vol = int(current["volume"])
        if cur_vol < avg_vol * 1.5 or cur_vol < min_volume:
            continue

        entry = round(float(current["close"]) * 1.001, 4)
        return entry, i, True, "momentum"

    return 0.0, -1, False, ""


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


def get_atr_stop_params_bt(rvol, atr, entry_price, gap_pct=0):
    """ATR-based stop with gap expansion for backtest (matches live_trade.py get_atr_stop_params)."""
    atr_mult = 2.0
    for rvol_min, mult in getattr(config, "ATR_MULT_TIERS", [(10.0, 3.0), (5.0, 2.5), (0.0, 2.0)]):
        if rvol >= rvol_min:
            atr_mult = mult
            break
    atr_stop_pct = (atr_mult * atr) / entry_price if entry_price > 0 else 0
    gap_factor = getattr(config, "GAP_STOP_FACTOR", 0.3)
    gap_stop_pct = abs(gap_pct) * gap_factor
    stop_pct = max(atr_stop_pct, gap_stop_pct)
    stop_pct = max(getattr(config, "ATR_STOP_MIN_PCT", 0.02),
                   min(getattr(config, "ATR_STOP_MAX_PCT", 0.08), stop_pct))
    trail_pct = max(0.005, min(0.05, (getattr(config, "ATR_TRAIL_MULT", 2.0) * atr) / entry_price)) if entry_price > 0 else 0.02
    target_pct = 0.0
    trail_activate_pct = 0.01  # Fixed 1% activation (matches live_trade.py)
    return stop_pct, target_pct, trail_activate_pct, trail_pct


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

    print(f"[rtg_7.0] Backtesting {len(trading_days)} trading days: "
          f"{trading_days[0].date()} to {trading_days[-1].date()}")
    print(f"Capital: ${config.INITIAL_CAPITAL:,.2f} | RVOL-weighted sizing | "
          f"Max concurrent: {config.MAX_POSITIONS} | Max daily trades: {config.MAX_DAILY_TRADES}")
    print(f"  [Fixed] ATR calculated from daily bars | RVOL uses gap-day volume | "
          f"Concurrent positions | Realistic stop fills | SEC/FINRA fees")
    print(f"Entry window: {config.ENTRY_WINDOW_START}-{config.ENTRY_WINDOW_END} EST")
    orb_bars = getattr(config, "ORB_BARS", 3)
    orb_buf = getattr(config, "ORB_BREAKOUT_BUFFER", 0.002)
    orb_min = getattr(config, "ORB_MIN_RANGE_PCT", 0.005)
    if getattr(config, "ORB_ENABLED", True):
        print(f"  ORB Entry: wait {orb_bars} bars, breakout +{orb_buf:.1%} buffer, min range {orb_min:.1%}")
    else:
        print(f"  Signal A (RTG): close > open AND vol >= {config.RTG_VOLUME_MULT}× prior AND vol >= {config.RTG_MIN_VOLUME:,}")
    print(f"  ATR Stops: max(ATR×mult, gap×0.3), clamped {getattr(config, 'ATR_STOP_MIN_PCT', 0.02):.0%}-{getattr(config, 'ATR_STOP_MAX_PCT', 0.08):.0%}")
    print(f"  Progressive Trail: " + ", ".join(
        f">{t:.0%}→{p:.1%}" for t, p in sorted(getattr(config, "PROGRESSIVE_TRAIL_TIERS", []), reverse=True)))
    print(f"  Failed-Entry (rtg_7.0): price < range_high → breakout failed")
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

    symbols = [s for s in symbols if not is_crypto_etf(s)]
    print(f"After crypto ETF filter: {len(symbols)} symbols")

    print("\nBulk scanning for gaps (with RVOL + ATR)...")
    gap_data, aft_data = bulk_scan_gaps(client, trading_days, symbols)
    total_candidates = sum(len(v) for v in gap_data.values())
    total_aft = sum(len(v) for v in aft_data.values())
    print(f"Found {total_candidates} gap entries + {total_aft} afternoon momentum entries across {len(gap_data)} days")

    all_trades = []
    equity = config.INITIAL_CAPITAL
    chart_entries = {}

    for date in trading_days:
        date_key = date.date()
        has_gap = date_key in gap_data and not gap_data[date_key].empty
        has_aft = getattr(config, "AFTERNOON_SCAN_ENABLED", False) and date_key in aft_data and not aft_data[date_key].empty
        if not has_gap and not has_aft:
            continue

        # Morning gap candidates
        if has_gap:
            candidates = gap_data[date_key]
            min_rvol = getattr(config, "MIN_ENTRY_RVOL", 2.0)
            min_price = getattr(config, "MIN_ENTRY_PRICE", 2.0)
            candidates = candidates[(candidates["rvol"] >= min_rvol) & (candidates["open_price"] >= min_price)]
            max_cands = getattr(config, "MAX_CANDIDATES", 5)
            candidates = candidates.head(max_cands)
        else:
            candidates = pd.DataFrame()

        # Afternoon momentum candidates
        if has_aft:
            aft_cands = aft_data[date_key]
            aft_max = getattr(config, "AFTERNOON_MAX_CANDIDATES", 5)
            aft_cands = aft_cands.head(aft_max)
        else:
            aft_cands = pd.DataFrame()

        n_gap = len(candidates) if not candidates.empty else 0
        n_aft = len(aft_cands) if not aft_cands.empty else 0
        print(f"\n--- {date_key} ({n_gap} gap + {n_aft} afternoon candidates, equity: ${equity:,.2f}) ---")
        for _, row in (candidates.iterrows() if not candidates.empty else []):
            sym = row["symbol"]
            open_price = row["open_price"]
            rvol = row.get("rvol", 0)
            gap_pct = row["gap_pct"]
            atr_val = row.get("atr", 0)
            atr_str = f" ATR=${atr_val:.2f}" if atr_val > 0 else ""
            print(f"  [GAP] {sym} gap={gap_pct:+.1%} RVOL={rvol:.1f}× open=${open_price:.2f}{atr_str}")
        for _, row in (aft_cands.iterrows() if not aft_cands.empty else []):
            sym = row["symbol"]
            rvol = row.get("rvol", 0)
            gain = row.get("gap_pct", 0)
            cp = row.get("close_price", 0)
            print(f"  [AFT] {sym} +{gain:.1%} RVOL={rvol:.1f}× ${cp:.2f}")

        daily_trades = 0
        daily_loss = 0.0
        max_daily_loss = equity * config.MAX_DAILY_LOSS_PCT
        max_daily_profit = 0.0  # Peak intraday profit for profit protection
        entry_count = {}  # symbol -> count of entries (for re-entry tracking)
        cached_bars = {}  # symbol -> (bars_df, bars_list)

        # Pre-compute same-tier counts for fair sizing split
        tier_counts = {}
        for _, r in candidates.iterrows():
            rvol_r = r.get("rvol", 0)
            tier_key = _get_rvol_tier(rvol_r)[0]
            tier_counts[tier_key] = tier_counts.get(tier_key, 0) + 1

        # Pre-fetch 1-min bars for all candidates
        cand_info = {}  # symbol -> dict with candidate metadata
        for _, row in candidates.iterrows():
            symbol = row["symbol"]
            if symbol in cached_bars:
                continue
            bars_1m = get_1min_bars(client, symbol, date)
            if bars_1m.empty or len(bars_1m) < 2:
                cached_bars[symbol] = (None, None)
                continue
            cached_bars[symbol] = (bars_1m, _bars_to_list(bars_1m))

        for _, row in candidates.iterrows():
            symbol = row["symbol"]
            rvol = row.get("rvol", 0)
            atr_val = row.get("atr", 0)
            open_price = row["open_price"]
            gap_pct = row.get("gap_pct", 0)
            # ATR-based stops when available, else RVOL fallback (matches live)
            if atr_val > 0:
                stop_p, target_p, trail_act_p, trail_p = get_atr_stop_params_bt(rvol, atr_val, open_price, gap_pct=gap_pct)
            else:
                stop_p, target_p, trail_act_p, trail_p = get_rvol_exit_params(rvol)
            same_tier = tier_counts.get(_get_rvol_tier(rvol)[0], 1)
            min_vol = config.RTG_MIN_VOLUME
            if rvol >= 10:
                min_vol = max(config.RTG_MIN_VOLUME // 3, 5000)
            elif rvol >= 5:
                min_vol = max(config.RTG_MIN_VOLUME // 2, 10000)
            cand_info[symbol] = {
                "open_price": open_price, "rvol": rvol, "gap_pct": gap_pct,
                "atr": atr_val, "stop_pct": stop_p, "target_pct": target_p,
                "trail_act_pct": trail_act_p, "trail_pct": trail_p,
                "same_tier": same_tier, "min_vol": min_vol,
                "signal": "gap",
            }

        # Pre-fetch 1-min bars + cand_info for afternoon momentum candidates
        if not aft_cands.empty:
            for _, row in aft_cands.iterrows():
                symbol = row["symbol"]
                if symbol in cached_bars:
                    continue
                bars_1m = get_1min_bars(client, symbol, date)
                if bars_1m.empty or len(bars_1m) < 2:
                    cached_bars[symbol] = (None, None)
                    continue
                cached_bars[symbol] = (bars_1m, _bars_to_list(bars_1m))

            for _, row in aft_cands.iterrows():
                symbol = row["symbol"]
                if symbol in cand_info:
                    continue  # Already in from gap scan
                rvol = row.get("rvol", 0)
                atr_val = row.get("atr", 0)
                open_price = row.get("open_price", 0)
                gap_pct = row.get("gap_pct", 0)
                if atr_val > 0:
                    stop_p, target_p, trail_act_p, trail_p = get_atr_stop_params_bt(rvol, atr_val, open_price, gap_pct=gap_pct)
                else:
                    stop_p = getattr(config, "AFTERNOON_STOP_PCT", 0.03)
                    target_p = 0.0
                    trail_act_p = getattr(config, "AFTERNOON_TRAIL_ACTIVATE_PCT", 0.01)
                    trail_p = getattr(config, "AFTERNOON_TRAIL_PCT", 0.015)
                min_vol = config.RTG_MIN_VOLUME
                if rvol >= 10:
                    min_vol = max(config.RTG_MIN_VOLUME // 3, 5000)
                elif rvol >= 5:
                    min_vol = max(config.RTG_MIN_VOLUME // 2, 10000)
                cand_info[symbol] = {
                    "open_price": open_price, "rvol": rvol, "gap_pct": gap_pct,
                    "atr": atr_val, "stop_pct": stop_p, "target_pct": target_p,
                    "trail_act_pct": trail_act_p, "trail_pct": trail_p,
                    "same_tier": 1, "min_vol": min_vol,
                    "signal": "afternoon",
                }

        # ── Concurrent bar-by-bar simulation ──────────────────────────────
        # Track open positions as list of dicts (similar to live TradeResult)
        open_positions = []  # each: {symbol, entry_price, shares, entry_bar_idx, ...}
        entered_symbols = set()  # symbols that already entered (no re-entry when disabled)
        stop_exit_symbols = set()  # symbols that exited via stop_loss (no re-entry)
        last_signal_bar = {}  # symbol -> last bar_idx where entry signal was found
        entry_slippage = getattr(config, "SLIPPAGE_ENTRY_PCT", 0.005)
        orb_enabled = getattr(config, "ORB_ENABLED", True)

        # Get max bar count across all candidate symbols
        max_bars = 0
        for sym in cand_info:
            bars_1m, _ = cached_bars.get(sym, (None, None))
            if bars_1m is not None:
                max_bars = max(max_bars, len(bars_1m))

        mkt_open_ts = pd.Timestamp(f"{date_key} {config.MARKET_OPEN}", tz="America/New_York")
        entry_start_str = getattr(config, "ENTRY_WINDOW_START", "09:30")
        entry_end_str = getattr(config, "ENTRY_WINDOW_END", "10:30")
        protect_delay = getattr(config, "DAILY_PROFIT_PROTECT_DELAY_SEC", 180)
        profit_protect_active = False

        for bar_idx in range(max_bars):
            # ── Exit monitoring: check all open positions against this bar ──
            positions_to_close = []
            for pos in open_positions:
                sym = pos["symbol"]
                bars_1m, all_bars_1m = cached_bars[sym]
                if bar_idx >= len(all_bars_1m):
                    continue
                bar = all_bars_1m[bar_idx]

                # Update highest
                if bar["high"] > pos["highest"]:
                    pos["highest"] = bar["high"]

                stop_price = round(pos["entry_price"] * (1 - pos["stop_pct"]), 4)
                exit_slippage = getattr(config, "SLIPPAGE_EXIT_PCT", 0.0)
                reason = None
                exit_price = 0.0

                # 1. Hard stop
                if bar["low"] <= stop_price:
                    if bar["open"] < stop_price:
                        exit_price = round(bar["open"] * (1 - exit_slippage), 4)
                    else:
                        exit_price = stop_price
                    reason = "stop_loss"

                # 2. Failed-entry cut (rtg_7.0): price < range_high → breakout failed
                if reason is None and getattr(config, "FAILED_ENTRY_ENABLED", True):
                    range_high = pos.get("range_high", 0.0)
                    if range_high > 0 and bar["close"] < range_high:
                        exit_price = bar["close"]
                        reason = "failed_entry"

                # 3. Progressive trailing stop
                if reason is None:
                    if not pos["trail_active"]:
                        if pos["highest"] >= pos["entry_price"] * (1 + pos["trail_act_pct"]):
                            pos["trail_active"] = True
                    if pos["trail_active"]:
                        stock_profit_pct = (pos["highest"] - pos["entry_price"]) / pos["entry_price"]
                        effective_trail = pos["trail_pct"]
                        for tier_profit, tier_trail in getattr(config, "PROGRESSIVE_TRAIL_TIERS", []):
                            if stock_profit_pct >= tier_profit:
                                effective_trail = tier_trail
                                break
                        trail_stop = round(pos["highest"] * (1 - effective_trail), 4)
                        if trail_stop > pos.get("trail_stop_price", 0):
                            pos["trail_stop_price"] = trail_stop
                        if bar["low"] <= pos["trail_stop_price"]:
                            if bar["open"] < pos["trail_stop_price"]:
                                exit_price = round(bar["open"] * (1 - exit_slippage), 4)
                            else:
                                exit_price = pos["trail_stop_price"]
                            reason = "trail_stop"

                # 4. Target (disabled when 0%)
                if reason is None and pos["target_pct"] > 0:
                    target_price = round(pos["entry_price"] * (1 + pos["target_pct"]), 4)
                    if bar["high"] >= target_price:
                        exit_price = target_price
                        reason = "target"

                if reason is not None:
                    # Apply exit slippage for non-limit exits
                    if reason == "force_close":
                        fc_slip = getattr(config, "SLIPPAGE_FORCE_CLOSE_PCT", 0.01)
                        exit_price = round(exit_price * (1 - fc_slip), 4)
                    elif exit_slippage > 0 and reason not in ("stop_loss", "trail_stop", "target"):
                        exit_price = round(exit_price * (1 - exit_slippage), 4)

                    # Regulatory fees
                    sale_proceeds = exit_price * pos["shares"]
                    reg_fees = round(sale_proceeds * 0.000027 + pos["shares"] * 0.000166, 4)
                    pnl = round((exit_price - pos["entry_price"]) * pos["shares"] - reg_fees, 2)
                    pnl_pct = round(pnl / (pos["entry_price"] * pos["shares"]), 4) if pos["entry_price"] > 0 else 0.0

                    result = TradeResult(
                        symbol=sym, date=str(date_key),
                        entry_price=pos["entry_price"], exit_price=round(exit_price, 4),
                        shares=pos["shares"], pnl=pnl, pnl_pct=pnl_pct,
                        exit_reason=reason, open_price=pos["open_price"],
                        stop_price=stop_price, target_price=round(pos["entry_price"] * (1 + pos["target_pct"]), 4) if pos["target_pct"] > 0 else 0,
                        trailing_high=round(pos["highest"], 4),
                        exit_bar_idx=bar_idx, entry_bar_idx=pos["entry_bar_idx"],
                        position_size=round(pos["entry_price"] * pos["shares"], 2),
                        signal_type=pos["signal_type"],
                    )
                    positions_to_close.append((pos, result))

            # Process closes
            for pos, result in positions_to_close:
                open_positions.remove(pos)
                all_trades.append(result)
                equity += result.pnl
                daily_loss += result.pnl
                daily_trades += 1
                # Only allow re-entry if RTG_REENTRY_ALLOWED and not stop_loss
                if result.exit_reason == "stop_loss":
                    stop_exit_symbols.add(pos["symbol"])
                    entered_symbols.add(pos["symbol"])  # Block re-entry after stop
                elif getattr(config, "RTG_REENTRY_ALLOWED", False):
                    entered_symbols.discard(pos["symbol"])  # Allow re-entry
                else:
                    entered_symbols.add(pos["symbol"])  # Block re-entry
                entry_ts = _bar_ts_str(cached_bars[pos["symbol"]][1], pos["entry_bar_idx"])
                exit_ts = _bar_ts_str(cached_bars[pos["symbol"]][1], bar_idx)
                print(f"  {pos['symbol']} [{pos['signal_type']}] entry=${pos['entry_price']:.4f}@{entry_ts} "
                      f"exit=${result.exit_price:.4f}@{exit_ts} ({result.exit_reason}), "
                      f"P&L=${result.pnl:+,.2f} ({result.pnl_pct:+.2%})")

                sym_key = f"{pos['symbol']} ({date_key})"
                if sym_key not in chart_entries:
                    chart_entries[sym_key] = {
                        "date": str(date_key),
                        "bars_1m": _bars_to_chart(cached_bars[pos["symbol"]][0]),
                        "events": [],
                        "entry_price": pos["entry_price"],
                        "open_price": pos["open_price"],
                        "signal": pos["signal_type"],
                    }
                chart_entries[sym_key]["events"].append(
                    {"ts": entry_ts, "type": "buy", "price": pos["entry_price"],
                     "label": f"BUY {pos['shares']}sh [{pos['signal_type']}]"})
                chart_entries[sym_key]["events"].append(
                    {"ts": exit_ts, "type": "sell", "price": result.exit_price,
                     "label": f"{result.exit_reason.upper()} {pos['shares']}sh"})

            # ── Daily loss circuit breaker ──
            if max_daily_loss > 0 and daily_loss <= -max_daily_loss:
                print(f"  Daily loss ${daily_loss:,.2f} exceeded limit, stopping for day")
                # Force-close remaining positions
                for pos in open_positions[:]:
                    sym = pos["symbol"]
                    bars_1m, all_bars_1m = cached_bars[sym]
                    if bar_idx < len(all_bars_1m):
                        fc_price = all_bars_1m[bar_idx]["close"]
                    else:
                        fc_price = all_bars_1m[-1]["close"]
                    fc_slip = getattr(config, "SLIPPAGE_FORCE_CLOSE_PCT", 0.01)
                    fc_price = round(fc_price * (1 - fc_slip), 4)
                    sale_proceeds = fc_price * pos["shares"]
                    reg_fees = round(sale_proceeds * 0.000027 + pos["shares"] * 0.000166, 4)
                    pnl = round((fc_price - pos["entry_price"]) * pos["shares"] - reg_fees, 2)
                    result = TradeResult(
                        symbol=sym, date=str(date_key),
                        entry_price=pos["entry_price"], exit_price=fc_price,
                        shares=pos["shares"], pnl=pnl,
                        pnl_pct=round(pnl / (pos["entry_price"] * pos["shares"]), 4),
                        exit_reason="circuit_breaker", open_price=pos["open_price"],
                        signal_type=pos["signal_type"],
                    )
                    all_trades.append(result)
                    equity += pnl
                    daily_loss += pnl
                    daily_trades += 1
                    print(f"  Circuit breaker close {sym}, P&L=${pnl:+,.2f}")
                open_positions.clear()
                break

            # ── Daily profit protection (rtg_7.0: close declining positions, continue trading) ──
            if getattr(config, "DAILY_PROFIT_PROTECT_ENABLED", False):
                bar_ts = None
                for sym in cand_info:
                    _, all_bars = cached_bars.get(sym, (None, None))
                    if all_bars and bar_idx < len(all_bars):
                        bar_ts = all_bars[bar_idx]["timestamp"]
                        break
                if bar_ts is not None:
                    elapsed = (bar_ts - mkt_open_ts).total_seconds()
                    if elapsed >= protect_delay:
                        current_profit = daily_loss
                        for pos in open_positions:
                            _, all_bars = cached_bars[pos["symbol"]]
                            if bar_idx < len(all_bars):
                                cur = all_bars[bar_idx]["close"]
                                current_profit += (cur - pos["entry_price"]) * pos["shares"]
                        if current_profit > max_daily_profit:
                            max_daily_profit = current_profit
                        protect_min = getattr(config, "DAILY_PROFIT_PROTECT_MIN", 5.0)
                        protect_ratio = getattr(config, "DAILY_PROFIT_PROTECT_RATIO", 0.90)
                        if max_daily_profit >= protect_min and current_profit < max_daily_profit * protect_ratio:
                            print(f"  Profit protection! Peak ${max_daily_profit:+,.2f}, now ${current_profit:+,.2f}")
                            # Close only declining positions (price going down), keep rising
                            positions_to_close = []
                            for pos in open_positions[:]:
                                sym = pos["symbol"]
                                _, all_bars_1m = cached_bars[sym]
                                if bar_idx < len(all_bars_1m):
                                    cur_price = all_bars_1m[bar_idx]["close"]
                                    prev_price = all_bars_1m[bar_idx - 1]["close"] if bar_idx >= 1 and bar_idx - 1 < len(all_bars_1m) else cur_price
                                else:
                                    cur_price = all_bars_1m[-1]["close"]
                                    prev_price = cur_price
                                price_declining = cur_price < prev_price
                                if price_declining:
                                    positions_to_close.append(pos)
                            for pos in positions_to_close:
                                sym = pos["symbol"]
                                _, all_bars_1m = cached_bars[sym]
                                if bar_idx < len(all_bars_1m):
                                    fc_price = all_bars_1m[bar_idx]["close"]
                                else:
                                    fc_price = all_bars_1m[-1]["close"]
                                fc_slip = getattr(config, "SLIPPAGE_FORCE_CLOSE_PCT", 0.01)
                                fc_price = round(fc_price * (1 - fc_slip), 4)
                                sale_proceeds = fc_price * pos["shares"]
                                reg_fees = round(sale_proceeds * 0.000027 + pos["shares"] * 0.000166, 4)
                                pnl = round((fc_price - pos["entry_price"]) * pos["shares"] - reg_fees, 2)
                                result = TradeResult(
                                    symbol=sym, date=str(date_key),
                                    entry_price=pos["entry_price"], exit_price=fc_price,
                                    shares=pos["shares"], pnl=pnl,
                                    pnl_pct=round(pnl / (pos["entry_price"] * pos["shares"]), 4),
                                    exit_reason="profit_protect", open_price=pos["open_price"],
                                    signal_type=pos["signal_type"],
                                )
                                all_trades.append(result)
                                equity += pnl
                                daily_loss += pnl
                                daily_trades += 1
                                open_positions.remove(pos)
                                print(f"  Profit protect close {sym} (declining), P&L=${pnl:+,.2f}")
                            # Reset peak after closing declining positions, continue trading (no break)

            # ── Entry monitoring: fill available slots ──
            if len(open_positions) >= config.MAX_POSITIONS:
                continue

            # Check if we're in entry window
            bar_time = None
            for sym in cand_info:
                _, all_bars = cached_bars.get(sym, (None, None))
                if all_bars and bar_idx < len(all_bars):
                    bar_time = all_bars[bar_idx]["timestamp"].time()
                    break
            if bar_time is None:
                continue
            entry_start_time = pd.Timestamp(f"{date_key} {entry_start_str}", tz="America/New_York").time()
            entry_end_time = pd.Timestamp(f"{date_key} {entry_end_str}", tz="America/New_York").time()

            # ── Morning entry (09:30-10:30): gap ORB ──
            if entry_start_time <= bar_time <= entry_end_time and not candidates.empty:
                cand_list = list(candidates.iterrows())
                cand_list.sort(key=lambda x: x[1].get("rvol", 0), reverse=True)

                for _, row in cand_list:
                    if len(open_positions) >= config.MAX_POSITIONS:
                        break
                    symbol = row["symbol"]
                    if symbol in entered_symbols or symbol in stop_exit_symbols:
                        continue
                    if symbol not in cand_info:
                        continue
                    ci = cand_info[symbol]
                    bars_1m, all_bars_1m = cached_bars.get(symbol, (None, None))
                    if bars_1m is None:
                        continue

                    if orb_enabled:
                        entry_at_open, entry_at_close, entry_bi, confirmed, signal_type, orb_range_high = find_orb_entry_1min(
                            bars_1m.iloc[:bar_idx + 1], ci["open_price"], min_volume=ci["min_vol"])
                    else:
                        entry_at_open, entry_at_close, entry_bi, confirmed, signal_type = find_rtg_entry_1min(
                            bars_1m.iloc[:bar_idx + 1], ci["open_price"], min_volume=ci["min_vol"])
                        orb_range_high = 0.0

                    if not confirmed or entry_at_open <= 0:
                        continue
                    if entry_bi <= last_signal_bar.get(symbol, -1):
                        continue
                    last_signal_bar[symbol] = entry_bi

                    entry_price_actual = round(entry_at_close * (1 + entry_slippage), 4)

                    # Position sizing (full all-in when MAX_POSITIONS=1, else RVOL-weighted)
                    if config.MAX_POSITIONS <= 1:
                        pos_size = max(config.MIN_POSITION_SIZE, equity)
                    else:
                        pos_size = get_rvol_sizing(ci["rvol"], equity, same_tier_count=ci["same_tier"])
                        pos_size = max(config.MIN_POSITION_SIZE, pos_size)
                    shares = int(pos_size / entry_price_actual)
                    if shares <= 0:
                        continue

                    open_positions.append({
                        "symbol": symbol, "entry_price": entry_price_actual,
                        "shares": shares, "entry_bar_idx": entry_bi,
                        "open_price": ci["open_price"], "gap_pct": ci["gap_pct"],
                        "signal_type": signal_type, "highest": entry_price_actual,
                        "trail_active": False, "trail_stop_price": 0.0,
                        "stop_pct": ci["stop_pct"], "target_pct": ci["target_pct"],
                        "trail_act_pct": ci["trail_act_pct"], "trail_pct": ci["trail_pct"],
                        "rvol": ci["rvol"], "atr": ci["atr"],
                        "range_high": orb_range_high,
                    })
                    entered_symbols.add(symbol)
                    entry_count[symbol] = entry_count.get(symbol, 0) + 1
                    daily_trades += 1
                    entry_ts = _bar_ts_str(all_bars_1m, entry_bi)
                    atr_str = f" ATR=${ci['atr']:.2f}" if ci["atr"] > 0 else ""
                    print(f"  {symbol} [{signal_type}] entry=${entry_price_actual:.4f}@{entry_ts} "
                          f"{shares}sh [RVOL={ci['rvol']:.1f}×{atr_str} stop={ci['stop_pct']:.0%}]")

            # ── Afternoon momentum entry (10:30-15:30) ──
            if (getattr(config, "AFTERNOON_SCAN_ENABLED", False)
                    and not aft_cands.empty
                    and len(open_positions) < config.MAX_POSITIONS):
                import datetime as _dt
                aft_end_str = getattr(config, "AFTERNOON_ENTRY_END", "15:30")
                aft_start_time = _dt.time(10, 30)
                aft_end_h, aft_end_m = (int(x) for x in aft_end_str.split(":"))
                aft_end_time = _dt.time(aft_end_h, aft_end_m)

                if aft_start_time <= bar_time <= aft_end_time:
                    aft_list = list(aft_cands.iterrows())
                    aft_list.sort(key=lambda x: x[1].get("rvol", 0), reverse=True)

                    for _, row in aft_list:
                        if len(open_positions) >= config.MAX_POSITIONS:
                            break
                        symbol = row["symbol"]
                        if symbol in entered_symbols or symbol in stop_exit_symbols:
                            continue
                        if symbol not in cand_info:
                            continue
                        ci = cand_info[symbol]
                        bars_1m, all_bars_1m = cached_bars.get(symbol, (None, None))
                        if bars_1m is None:
                            continue

                        # Momentum entry: volume-price breakout
                        entry_price, entry_bi, confirmed, signal_type = find_momentum_entry_1min(
                            bars_1m.iloc[:bar_idx + 1], min_volume=ci["min_vol"])

                        if not confirmed or entry_price <= 0:
                            continue
                        if entry_bi <= last_signal_bar.get(symbol, -1):
                            continue
                        last_signal_bar[symbol] = entry_bi

                        entry_price_actual = round(entry_price * (1 + entry_slippage), 4)

                        # Position sizing: full all-in when MAX_POSITIONS=1
                        if config.MAX_POSITIONS <= 1:
                            pos_size = max(config.MIN_POSITION_SIZE, equity)
                        else:
                            pos_size = get_rvol_sizing(ci["rvol"], equity, same_tier_count=ci["same_tier"])
                            pos_size = max(config.MIN_POSITION_SIZE, pos_size)
                        shares = int(pos_size / entry_price_actual)
                        if shares <= 0:
                            continue

                        open_positions.append({
                            "symbol": symbol, "entry_price": entry_price_actual,
                            "shares": shares, "entry_bar_idx": entry_bi,
                            "open_price": ci["open_price"], "gap_pct": ci["gap_pct"],
                            "signal_type": signal_type, "highest": entry_price_actual,
                            "trail_active": False, "trail_stop_price": 0.0,
                            "stop_pct": ci["stop_pct"], "target_pct": ci["target_pct"],
                            "trail_act_pct": ci["trail_act_pct"], "trail_pct": ci["trail_pct"],
                            "rvol": ci["rvol"], "atr": ci["atr"],
                            "range_high": 0.0,  # No ORB range for momentum entry
                        })
                        entered_symbols.add(symbol)
                        entry_count[symbol] = entry_count.get(symbol, 0) + 1
                        daily_trades += 1
                        entry_ts = _bar_ts_str(all_bars_1m, entry_bi)
                        print(f"  {symbol} [{signal_type}] entry=${entry_price_actual:.4f}@{entry_ts} "
                              f"{shares}sh [RVOL={ci['rvol']:.1f}× stop={ci['stop_pct']:.0%} trail={ci['trail_pct']:.1%}]")

        # ── Force-close remaining positions at end of day ──
        for pos in open_positions[:]:
            sym = pos["symbol"]
            _, all_bars_1m = cached_bars[sym]
            fc_price = all_bars_1m[-1]["close"]
            fc_slip = getattr(config, "SLIPPAGE_FORCE_CLOSE_PCT", 0.01)
            fc_price = round(fc_price * (1 - fc_slip), 4)
            sale_proceeds = fc_price * pos["shares"]
            reg_fees = round(sale_proceeds * 0.000027 + pos["shares"] * 0.000166, 4)
            pnl = round((fc_price - pos["entry_price"]) * pos["shares"] - reg_fees, 2)
            result = TradeResult(
                symbol=sym, date=str(date_key),
                entry_price=pos["entry_price"], exit_price=fc_price,
                shares=pos["shares"], pnl=pnl,
                pnl_pct=round(pnl / (pos["entry_price"] * pos["shares"]), 4),
                exit_reason="force_close", open_price=pos["open_price"],
                stop_price=round(pos["entry_price"] * (1 - pos["stop_pct"]), 4),
                trailing_high=round(pos["highest"], 4),
                signal_type=pos["signal_type"],
            )
            all_trades.append(result)
            equity += pnl
            daily_loss += pnl
            daily_trades += 1
            entry_ts = _bar_ts_str(all_bars_1m, pos["entry_bar_idx"])
            exit_ts = _bar_ts_str(all_bars_1m, len(all_bars_1m) - 1)
            print(f"  {sym} FORCE_CLOSE entry=${pos['entry_price']:.4f}@{entry_ts} "
                  f"exit=${fc_price:.4f}@{exit_ts}, P&L=${pnl:+,.2f}")

            sym_key = f"{sym} ({date_key})"
            if sym_key not in chart_entries:
                chart_entries[sym_key] = {
                    "date": str(date_key),
                    "bars_1m": _bars_to_chart(cached_bars[sym][0]),
                    "events": [],
                    "entry_price": pos["entry_price"],
                    "open_price": pos["open_price"],
                    "signal": pos["signal_type"],
                }
            chart_entries[sym_key]["events"].append(
                {"ts": entry_ts, "type": "buy", "price": pos["entry_price"],
                 "label": f"BUY {pos['shares']}sh [{pos['signal_type']}]"})
            chart_entries[sym_key]["events"].append(
                {"ts": exit_ts, "type": "sell", "price": fc_price,
                 "label": f"FORCE_CLOSE {pos['shares']}sh"})
        open_positions.clear()

    print(f"\n{'=' * 70}")
    print(f"[rtg_7.0] Backtest complete. Final equity: ${equity:,.2f}")
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
