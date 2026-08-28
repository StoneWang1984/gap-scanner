"""Full gap system analysis — 5 analyses to diagnose why the system only makes +3.3%.

Analyses:
  1. Upside left on table (capture ratio)
  2. Zero-trade days (entry filter too strict)
  3. Trailing stop what-if (X = 3%, 5%, 8%, 10%)
  4. MAX_POSITIONS=3 what-if
  5. SCAN_INTERVAL_SEC=30 what-if
"""

import json
import os
import re
import sys
import importlib.util
from collections import Counter, defaultdict
from copy import deepcopy

# ── Load config from this version dir ────────────────────────────────
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

from scanner import get_data_client, get_tradable_symbols

# ── ETF filters (copied from backtest.py) ──────────────────────────
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


# ── Data helpers (from backtest.py) ────────────────────────────────

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
    """Scan for gap-up stocks across all trading days with RVOL."""
    start = trading_days[0] - pd.Timedelta(days=45)
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


# ── Entry signal detection (from backtest.py) ─────────────────────

def find_rtg_entry(bars_list, open_price, start_idx=0, min_volume=None):
    if min_volume is None:
        min_volume = config.RTG_MIN_VOLUME
    entry_start = getattr(config, "ENTRY_WINDOW_START", "09:30")
    entry_end = getattr(config, "ENTRY_WINDOW_END", "15:55")
    max_green = getattr(config, "MAX_GREEN_BARS_TO_ENTER", 2)
    min_gain_pct = getattr(config, "RTG_MIN_BAR_GAIN_PCT", 0.01)
    confirm_bars = getattr(config, "ENTRY_CONFIRM_BARS", 2)

    for i in range(max(start_idx, 1), len(bars_list)):
        bar = bars_list[i]
        prev = bars_list[i - 1]
        ts = bar["timestamp"]
        bar_time = ts.time()
        start_time = pd.Timestamp(f"{ts.date()} {entry_start}", tz="America/New_York").time()
        end_time = pd.Timestamp(f"{ts.date()} {entry_end}", tz="America/New_York").time()
        if not (start_time <= bar_time <= end_time):
            continue

        if (bar["close"] > open_price * (1 + min_gain_pct)
                and prev["volume"] > 0
                and bar["volume"] >= config.RTG_VOLUME_MULT * prev["volume"]
                and bar["volume"] >= min_volume):
            if confirm_bars >= 2 and i >= 2:
                prev2 = bars_list[i - 2]
                if not (prev["close"] > open_price
                        and prev2["volume"] > 0
                        and prev["volume"] >= config.RTG_VOLUME_MULT * prev2["volume"]
                        and prev["volume"] >= min_volume):
                    continue
            consecutive_green = 0
            for j in range(i - 1, -1, -1):
                if bars_list[j]["close"] > bars_list[j]["open"]:
                    consecutive_green += 1
                else:
                    break
            if consecutive_green >= max_green:
                continue
            entry_price = round(bar["close"] * 1.001, 4)
            return entry_price, i, "rtg"

    return None


def find_breakout_entry(bars_list, open_price, start_idx=0, min_volume=None):
    if not getattr(config, "BREAKOUT_ENABLED", False):
        return None
    if min_volume is None:
        min_volume = config.RTG_MIN_VOLUME
    min_bars = getattr(config, "BREAKOUT_MIN_BARS", 5)
    vol_mult = getattr(config, "BREAKOUT_VOLUME_MULT", 2.5)
    entry_at_close = getattr(config, "BREAKOUT_ENTRY_AT_CLOSE", True)
    max_green = getattr(config, "MAX_GREEN_BARS_TO_ENTER", 2)
    min_gain_pct = getattr(config, "RTG_MIN_BAR_GAIN_PCT", 0.01)
    confirm_bars = getattr(config, "ENTRY_CONFIRM_BARS", 2)

    day_high = 0.0
    for i in range(max(start_idx, 1), len(bars_list)):
        bar = bars_list[i]
        prev = bars_list[i - 1]
        if i > 0:
            day_high = max(day_high, bars_list[i - 1]["high"])
        if i < min_bars:
            continue

        if (bar["close"] > day_high
                and bar["close"] > open_price * (1 + min_gain_pct)
                and prev["volume"] > 0
                and bar["volume"] >= vol_mult * prev["volume"]
                and bar["volume"] >= min_volume):
            if confirm_bars >= 2 and i >= 2:
                prev2 = bars_list[i - 2]
                if not (prev["close"] > open_price
                        and prev2["volume"] > 0
                        and prev["volume"] >= vol_mult * prev2["volume"]
                        and prev["volume"] >= min_volume):
                    day_high = max(day_high, bar["high"])
                    continue
            consecutive_green = 0
            for j in range(i - 1, -1, -1):
                if bars_list[j]["close"] > bars_list[j]["open"]:
                    consecutive_green += 1
                else:
                    break
            if consecutive_green >= max_green:
                day_high = max(day_high, bar["high"])
                continue
            if entry_at_close:
                entry_price = round(bar["close"] * 1.001, 4)
            else:
                entry_price = round(bar["open"] * 1.001, 4)
            return entry_price, i, "breakout"

    return None


def find_entry_signal(bars_list, open_price, start_idx=0, min_volume=None):
    rtg = find_rtg_entry(bars_list, open_price, start_idx, min_volume)
    brk = find_breakout_entry(bars_list, open_price, start_idx, min_volume)
    if rtg is None and brk is None:
        return None
    if rtg is None:
        return brk
    if brk is None:
        return rtg
    if rtg[1] <= brk[1]:
        return rtg
    return brk


# ── Bar-based exit (from backtest.py) ──────────────────────────────

def evaluate_bar_exit(entry_price, shares, bars_after_entry, symbol="",
                      open_price=0.0, force_close_price=None, entry_bar_idx=0,
                      signal_type=""):
    if not bars_after_entry or entry_price <= 0 or shares <= 0:
        return {"exit_price": entry_price, "pnl": 0.0, "pnl_pct": 0.0,
                "exit_reason": "no_bars", "trailing_high": 0.0, "exit_bar_idx": -1}

    stop_price = round(entry_price * (1 - config.STOP_PCT), 4)
    target_price = round(entry_price * (1 + config.TARGET_PCT), 4)
    slippage = getattr(config, "SLIPPAGE_EXIT_PCT", 0.0)
    highest = entry_price
    is_first_bar = True
    green_bar_count = 0
    red_bar_count = 0
    g2r_consec = getattr(config, "GREEN_TO_RED_CONSEC_BARS", 2)
    exit_price = 0.0
    reason = ""
    exit_bi = 0

    for bi, bar in enumerate(bars_after_entry):
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bar_close = float(bar["close"])
        bar_open = float(bar["open"])
        if bar_high > highest:
            highest = bar_high
        is_green = bar_close >= bar_open
        is_red = not is_green
        exit_triggered = False

        if is_first_bar and is_red and getattr(config, "EXIT_ON_RED_BAR", True):
            exit_price = bar_close; reason = "red_bar_exit"; exit_bi = bi; exit_triggered = True
        if not exit_triggered and not is_first_bar and is_red and getattr(config, "EXIT_ON_GREEN_TO_RED", True):
            red_bar_count += 1
            if red_bar_count >= g2r_consec:
                exit_price = bar_close; reason = "green_to_red"; exit_bi = bi; exit_triggered = True
        elif not exit_triggered and not is_first_bar and is_green:
            red_bar_count = 0
        if not exit_triggered and is_green and getattr(config, "EXIT_ON_THREE_GREEN", True):
            green_bar_count += 1
            if green_bar_count >= 3:
                exit_price = bar_close; reason = "three_green_bars"; exit_bi = bi; exit_triggered = True
        elif not exit_triggered and is_red:
            green_bar_count = 0
        if not exit_triggered and bar_low <= stop_price:
            exit_price = stop_price; reason = "stop_loss"; exit_bi = bi; exit_triggered = True
        if not exit_triggered and bar_high >= target_price:
            exit_price = target_price; reason = "target"; exit_bi = bi; exit_triggered = True

        is_first_bar = False
        if exit_triggered:
            break
        exit_bi = bi
    else:
        if force_close_price is not None and force_close_price > 0:
            exit_price = force_close_price
        else:
            exit_price = float(bars_after_entry[-1]["close"])
        reason = "force_close"
        exit_bi = len(bars_after_entry) - 1

    if slippage > 0 and reason not in ("stop_loss", "red_bar_exit", "green_to_red", "three_green_bars", "target"):
        exit_price = round(exit_price * (1 - slippage), 4)

    pnl = round((exit_price - entry_price) * shares, 2)
    pnl_pct = round(pnl / (entry_price * shares), 4) if entry_price > 0 else 0.0

    return {
        "exit_price": round(exit_price, 4), "pnl": pnl, "pnl_pct": pnl_pct,
        "exit_reason": reason, "trailing_high": round(highest, 4),
        "exit_bar_idx": exit_bi,
    }


# ── Trailing stop exit ─────────────────────────────────────────────

def evaluate_trailing_stop_exit(entry_price, shares, bars_after_entry, trail_pct,
                                symbol="", open_price=0.0, force_close_price=None,
                                entry_bar_idx=0, signal_type=""):
    """Exit when price drops trail_pct from the highest price since entry."""
    if not bars_after_entry or entry_price <= 0 or shares <= 0:
        return {"exit_price": entry_price, "pnl": 0.0, "pnl_pct": 0.0,
                "exit_reason": "no_bars", "trailing_high": 0.0, "exit_bar_idx": -1,
                "holding_bars": 0}

    stop_price = round(entry_price * (1 - config.STOP_PCT), 4)  # 3% hard stop as backstop
    slippage = getattr(config, "SLIPPAGE_EXIT_PCT", 0.0)
    highest = entry_price
    trail_stop_price = entry_price * (1 - trail_pct)
    exit_price = 0.0
    reason = ""
    exit_bi = 0

    for bi, bar in enumerate(bars_after_entry):
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bar_close = float(bar["close"])

        # Update trailing high and trail stop
        if bar_high > highest:
            highest = bar_high
            trail_stop_price = highest * (1 - trail_pct)

        # Hard stop loss (3% backstop)
        if bar_low <= stop_price:
            exit_price = stop_price; reason = "stop_loss"; exit_bi = bi
            break

        # Trailing stop hit
        if bar_low <= trail_stop_price:
            exit_price = round(trail_stop_price, 4); reason = f"trail_{trail_pct:.0%}"; exit_bi = bi
            break

        exit_bi = bi
    else:
        # Force close at end of day
        if force_close_price is not None and force_close_price > 0:
            exit_price = force_close_price
        else:
            exit_price = float(bars_after_entry[-1]["close"])
        reason = "force_close"
        exit_bi = len(bars_after_entry) - 1

    if slippage > 0 and reason not in ("stop_loss",):
        exit_price = round(exit_price * (1 - slippage), 4)

    pnl = round((exit_price - entry_price) * shares, 2)
    pnl_pct = round(pnl / (entry_price * shares), 4) if entry_price > 0 else 0.0

    return {
        "exit_price": round(exit_price, 4), "pnl": pnl, "pnl_pct": pnl_pct,
        "exit_reason": reason, "trailing_high": round(highest, 4),
        "exit_bar_idx": exit_bi, "holding_bars": exit_bi + 1,
    }


# ══════════════════════════════════════════════════════════════════
#  DATA LOADING — shared across all analyses
# ══════════════════════════════════════════════════════════════════

def load_all_data(end_date=None, n_days=None):
    """Load all data needed for the 5 analyses. Returns a dict with
    trading_days, gap_data, cached_bars (per date/symbol)."""
    if n_days is None:
        n_days = config.BACKTEST_DAYS
    client = get_data_client()
    if end_date is None:
        end_date = pd.Timestamp.now(tz="America/New_York")

    trading_days = get_trading_days(client, end_date, n_days)
    if not trading_days:
        print("No trading days found.")
        return None

    print(f"Backtesting {len(trading_days)} trading days: "
          f"{trading_days[0].date()} to {trading_days[-1].date()}")

    print("\nLoading tradable symbols...")
    symbols = get_tradable_symbols()
    print(f"Found {len(symbols)} tradable symbols")
    symbols = [s for s in symbols if not is_leveraged_etf(s)]
    symbols = [s for s in symbols if not is_crypto_etf(s)]
    print(f"After ETF filters: {len(symbols)} symbols")

    print("\nBulk scanning for gaps (with RVOL)...")
    gap_data = bulk_scan_gaps(client, trading_days, symbols)
    total_candidates = sum(len(v) for v in gap_data.values())
    print(f"Found {total_candidates} gap entries across {len(gap_data)} days")

    # Pre-fetch 1-min bars for all qualified candidates
    print("\nPre-fetching 1-min bars for all qualified candidates...")
    min_rvol = getattr(config, "MIN_RVOL_TO_TRADE", 3.0)
    max_cands = getattr(config, "MAX_CANDIDATES", 40)
    cached_bars = {}  # (date_key, symbol) -> bars_list

    total_symbols = 0
    for date in trading_days:
        date_key = date.date()
        if date_key not in gap_data or gap_data[date_key].empty:
            continue
        candidates = gap_data[date_key].head(max_cands)
        for _, row in candidates.iterrows():
            symbol = row["symbol"]
            key = (date_key, symbol)
            if key in cached_bars:
                continue
            bars_1m = get_1min_bars(client, symbol, date)
            if bars_1m.empty or len(bars_1m) < 2:
                cached_bars[key] = None
            else:
                cached_bars[key] = _bars_to_list(bars_1m)
            total_symbols += 1
    print(f"Fetched 1-min bars for {total_symbols} date/symbol combos")

    return {
        "trading_days": trading_days,
        "gap_data": gap_data,
        "cached_bars": cached_bars,
    }


# ══════════════════════════════════════════════════════════════════
#  ANALYSIS 1: Upside left on table (capture ratio)
# ══════════════════════════════════════════════════════════════════

def analysis_1_capture_ratio(data):
    """For each trade, calculate actual P&L%, potential P&L% if held to day high,
    and capture ratio."""
    print("\n" + "=" * 90)
    print("ANALYSIS 1: UPSIDE LEFT ON TABLE (CAPTURE RATIO)")
    print("=" * 90)

    trading_days = data["trading_days"]
    gap_data = data["gap_data"]
    cached_bars = data["cached_bars"]

    min_rvol = getattr(config, "MIN_RVOL_TO_TRADE", 3.0)
    max_cands = getattr(config, "MAX_CANDIDATES", 40)
    all_in_ratio = getattr(config, "ALL_IN_BP_RATIO", 0.95)
    entry_slippage = getattr(config, "SLIPPAGE_ENTRY_PCT", 0.005)
    force_close_str = getattr(config, "FORCE_CLOSE_TIME", "15:59")
    force_close_min = int(force_close_str.split(":")[0]) * 60 + int(force_close_str.split(":")[1])
    scan_interval_min = getattr(config, "SCAN_INTERVAL_SEC", 300) / 60.0

    equity = config.INITIAL_CAPITAL
    trade_details = []

    for date in trading_days:
        date_key = date.date()
        if date_key not in gap_data or gap_data[date_key].empty:
            continue

        candidates = gap_data[date_key].head(max_cands)
        qualified = candidates[candidates["rvol"] >= min_rvol]
        if qualified.empty:
            continue

        next_search_idx = {row["symbol"]: 0 for _, row in qualified.iterrows()}
        last_exit_bar_min = 0
        daily_loss = 0.0
        max_daily_loss = equity * config.MAX_DAILY_LOSS_PCT

        while True:
            if daily_loss <= -max_daily_loss:
                break

            max_attempts = getattr(config, "MAX_ENTRY_ATTEMPTS", 3)
            entered = False

            for _, row in qualified.iterrows():
                if entered or max_attempts <= 0:
                    break

                symbol = row["symbol"]
                open_price = row["open_price"]
                rvol = row.get("rvol", 0)
                all_bars = cached_bars.get((date_key, symbol))
                if all_bars is None:
                    max_attempts -= 1
                    continue

                search_from = next_search_idx.get(symbol, 0)
                result = find_entry_signal(all_bars, open_price, start_idx=search_from)
                if result is None:
                    max_attempts -= 1
                    continue

                entry_price_signal, entry_bar_idx, signal_type = result

                entry_bar_ts = all_bars[entry_bar_idx]["timestamp"]
                entry_bar_min_val = entry_bar_ts.hour * 60 + entry_bar_ts.minute
                if entry_bar_min_val < last_exit_bar_min + scan_interval_min:
                    max_attempts -= 1
                    continue
                if entry_bar_min_val >= force_close_min - 1:
                    max_attempts -= 1
                    continue

                entry_bar_close = all_bars[entry_bar_idx]["close"]
                entry_price_actual = round(entry_bar_close * (1 + entry_slippage), 4)

                pos_size = equity * all_in_ratio
                shares = int(pos_size / entry_price_actual)
                if shares <= 0:
                    max_attempts -= 1
                    continue

                remaining_bars = all_bars[entry_bar_idx + 1:]
                cycle_bars = []
                force_close_price = remaining_bars[-1]["close"] if remaining_bars else entry_price_actual
                for bar in remaining_bars:
                    bar_min_val = bar["timestamp"].hour * 60 + bar["timestamp"].minute
                    cycle_bars.append(bar)
                    if bar_min_val >= force_close_min:
                        force_close_price = bar["close"]
                        break

                if not cycle_bars:
                    max_attempts -= 1
                    continue

                # Day's high after entry (across ALL remaining bars, not just until exit)
                day_high_after_entry = max(b["high"] for b in remaining_bars)

                trade_result = evaluate_bar_exit(
                    entry_price=entry_price_actual, shares=shares,
                    bars_after_entry=cycle_bars, symbol=symbol,
                    open_price=open_price, force_close_price=force_close_price,
                    entry_bar_idx=entry_bar_idx, signal_type=signal_type,
                )

                # Calculate capture ratio
                actual_pnl_pct = trade_result["pnl_pct"]
                potential_pnl_pct = (day_high_after_entry - entry_price_actual) / entry_price_actual
                capture_ratio = actual_pnl_pct / potential_pnl_pct if potential_pnl_pct > 0 else 0.0

                entry_ts = _bar_ts_str(all_bars, entry_bar_idx)

                trade_detail = {
                    "date": str(date_key),
                    "symbol": symbol,
                    "signal_type": signal_type,
                    "entry_time": entry_ts,
                    "entry_price": entry_price_actual,
                    "exit_price": trade_result["exit_price"],
                    "exit_reason": trade_result["exit_reason"],
                    "actual_pnl_pct": actual_pnl_pct,
                    "day_high_after_entry": day_high_after_entry,
                    "potential_pnl_pct": potential_pnl_pct,
                    "capture_ratio": capture_ratio,
                    "pnl_dollar": trade_result["pnl"],
                    "shares": shares,
                    "rvol": rvol,
                }
                trade_details.append(trade_detail)

                # Print per-trade detail
                print(f"  {date_key} {symbol} [{signal_type}] entry=${entry_price_actual:.4f}@{entry_ts} "
                      f"exit=${trade_result['exit_price']:.4f} ({trade_result['exit_reason']}) "
                      f"actual={actual_pnl_pct:+.2%} high=${day_high_after_entry:.4f} "
                      f"potential={potential_pnl_pct:+.2%} capture={capture_ratio:.1%}")

                equity += trade_result["pnl"]
                daily_loss += trade_result["pnl"]

                exit_bar_abs = entry_bar_idx + 1 + trade_result["exit_bar_idx"]
                next_search_idx[symbol] = exit_bar_abs + 1
                if exit_bar_abs < len(all_bars):
                    exit_ts = all_bars[exit_bar_abs]["timestamp"]
                    last_exit_bar_min = exit_ts.hour * 60 + exit_ts.minute
                else:
                    last_exit_bar_min = force_close_min

                entered = True
                break

            if not entered:
                break

    # Summary statistics
    if not trade_details:
        print("NO TRADES FOUND")
        return

    print(f"\n{'─' * 90}")
    print("CAPTURE RATIO SUMMARY")
    print(f"{'─' * 90}")

    total_actual = sum(t["actual_pnl_pct"] for t in trade_details)
    total_potential = sum(t["potential_pnl_pct"] for t in trade_details)
    avg_capture = sum(t["capture_ratio"] for t in trade_details) / len(trade_details)

    # Filter out trades where potential was <= 0 (shouldn't happen but defensive)
    positive_potential = [t for t in trade_details if t["potential_pnl_pct"] > 0]
    avg_capture_pos = (sum(t["capture_ratio"] for t in positive_potential) / len(positive_potential)
                       if positive_potential else 0)

    # By exit reason
    by_reason = defaultdict(list)
    for t in trade_details:
        by_reason[t["exit_reason"]].append(t)

    print(f"\nTotal trades: {len(trade_details)}")
    print(f"Total actual P&L%: {total_actual:+.2%}")
    print(f"Total potential P&L%: {total_potential:+.2%}")
    print(f"Upside left on table: {total_potential - total_actual:+.2%}")
    print(f"Average capture ratio: {avg_capture:.1%}")
    print(f"Average capture ratio (positive potential only): {avg_capture_pos:.1%}")

    print(f"\n{'Exit Reason':<20} {'Count':>6} {'AvgCapture':>10} {'AvgActual':>10} {'AvgPotential':>12} {'AvgLeft':>10}")
    print("-" * 70)
    for reason in sorted(by_reason.keys()):
        group = by_reason[reason]
        pos = [t for t in group if t["potential_pnl_pct"] > 0]
        avg_cap = sum(t["capture_ratio"] for t in pos) / len(pos) if pos else 0
        avg_act = sum(t["actual_pnl_pct"] for t in group) / len(group)
        avg_pot = sum(t["potential_pnl_pct"] for t in group) / len(group)
        avg_left = avg_pot - avg_act
        print(f"{reason:<20} {len(group):>6} {avg_cap:>9.1%} {avg_act:>+9.2%} {avg_pot:>+11.2%} {avg_left:>+9.2%}")

    # Top 10 most upside left
    sorted_by_left = sorted(trade_details, key=lambda t: t["potential_pnl_pct"] - t["actual_pnl_pct"], reverse=True)
    print(f"\nTop 10 trades with most upside left on table:")
    print(f"{'Date':<12} {'Symbol':<8} {'Entry':>8} {'Exit':>8} {'Reason':<18} {'Actual':>8} {'Pot':>8} {'Left':>8} {'Capture':>8}")
    print("-" * 88)
    for t in sorted_by_left[:10]:
        left = t["potential_pnl_pct"] - t["actual_pnl_pct"]
        print(f"{t['date']:<12} {t['symbol']:<8} ${t['entry_price']:>6.2f} ${t['exit_price']:>6.2f} "
              f"{t['exit_reason']:<18} {t['actual_pnl_pct']:>+7.2%} {t['potential_pnl_pct']:>+7.2%} {left:>+7.2%} "
              f"{t['capture_ratio']:>7.1%}")


# ══════════════════════════════════════════════════════════════════
#  ANALYSIS 2: Zero-trade days
# ══════════════════════════════════════════════════════════════════

def analysis_2_zero_trade_days(data):
    """Count days with qualified candidates but no entry signal fired."""
    print("\n" + "=" * 90)
    print("ANALYSIS 2: ZERO-TRADE DAYS (ENTRY FILTER TOO STRICT?)")
    print("=" * 90)

    trading_days = data["trading_days"]
    gap_data = data["gap_data"]
    cached_bars = data["cached_bars"]

    min_rvol = getattr(config, "MIN_RVOL_TO_TRADE", 3.0)
    max_cands = getattr(config, "MAX_CANDIDATES", 40)

    zero_trade_days = []
    trade_days = []
    no_candidate_days = []

    for date in trading_days:
        date_key = date.date()
        if date_key not in gap_data or gap_data[date_key].empty:
            no_candidate_days.append(date_key)
            continue

        candidates = gap_data[date_key].head(max_cands)
        qualified = candidates[candidates["rvol"] >= min_rvol]

        if qualified.empty:
            no_candidate_days.append(date_key)
            continue

        # Check if ANY qualified candidate has an entry signal
        has_signal = False
        signal_details = []
        for _, row in qualified.iterrows():
            symbol = row["symbol"]
            open_price = row["open_price"]
            rvol = row.get("rvol", 0)
            all_bars = cached_bars.get((date_key, symbol))
            if all_bars is None:
                signal_details.append(f"  {symbol} RVOL={rvol:.1f}x: no bars data")
                continue
            result = find_entry_signal(all_bars, open_price, start_idx=0)
            if result is not None:
                has_signal = True
                entry_price, entry_bar_idx, signal_type = result
                entry_ts = _bar_ts_str(all_bars, entry_bar_idx)
                signal_details.append(f"  {symbol} RVOL={rvol:.1f}x: SIGNAL at {entry_ts} ({signal_type})")
            else:
                signal_details.append(f"  {symbol} RVOL={rvol:.1f}x: NO signal (filters too strict)")

        if has_signal:
            trade_days.append(date_key)
        else:
            zero_trade_days.append({
                "date": date_key,
                "n_candidates": len(candidates),
                "n_qualified": len(qualified),
                "details": signal_details,
            })

    total_days = len(trading_days)
    print(f"\nTotal trading days: {total_days}")
    print(f"Days with no gap candidates at all: {len(no_candidate_days)}")
    print(f"Days with candidates but none qualified (RVOL < {min_rvol:.1f}x): {len(zero_trade_days) + len(trade_days) - total_days + len(no_candidate_days)}")
    print(f"Days with qualified candidates AND a signal: {len(trade_days)}")
    print(f"Days with qualified candidates BUT NO signal: {len(zero_trade_days)}")
    print(f"\nZero-trade rate: {len(zero_trade_days)}/{len(zero_trade_days) + len(trade_days)} "
          f"= {len(zero_trade_days)/(len(zero_trade_days) + len(trade_days)):.1%} of days with qualified candidates")

    if zero_trade_days:
        print(f"\nDetails of zero-trade days (qualified candidates but no entry signal):")
        for zt in zero_trade_days:
            print(f"\n  {zt['date']} — {zt['n_candidates']} candidates, {zt['n_qualified']} qualified:")
            for d in zt["details"]:
                print(f"    {d}")

    # Break down: how many candidates fail at each filter stage?
    print(f"\n{'─' * 90}")
    print("FILTER STAGE BREAKDOWN")
    print(f"{'─' * 90}")

    total_candidates = 0
    fail_2bar_confirm = 0
    fail_25x_volume = 0
    fail_1pct_gain = 0
    fail_green_chase = 0

    for date in trading_days:
        date_key = date.date()
        if date_key not in gap_data or gap_data[date_key].empty:
            continue
        candidates = gap_data[date_key].head(max_cands)
        qualified = candidates[candidates["rvol"] >= min_rvol]
        if qualified.empty:
            continue

        for _, row in qualified.iterrows():
            symbol = row["symbol"]
            open_price = row["open_price"]
            all_bars = cached_bars.get((date_key, symbol))
            if all_bars is None:
                continue
            total_candidates += 1

            # Check each filter independently
            min_gain_pct = getattr(config, "RTG_MIN_BAR_GAIN_PCT", 0.01)
            confirm_bars = getattr(config, "ENTRY_CONFIRM_BARS", 2)
            max_green = getattr(config, "MAX_GREEN_BARS_TO_ENTER", 2)

            for i in range(1, len(all_bars)):
                bar = all_bars[i]
                prev = all_bars[i - 1]
                ts = bar["timestamp"]
                bar_time = ts.time()
                start_time = pd.Timestamp(f"{ts.date()} {config.ENTRY_WINDOW_START}", tz="America/New_York").time()
                end_time = pd.Timestamp(f"{ts.date()} {config.ENTRY_WINDOW_END}", tz="America/New_York").time()
                if not (start_time <= bar_time <= end_time):
                    continue

                # Check D: 1% gain
                d_pass = bar["close"] > open_price * (1 + min_gain_pct)
                # Check B: 2.5x volume
                b_pass = (prev["volume"] > 0
                          and bar["volume"] >= config.RTG_VOLUME_MULT * prev["volume"]
                          and bar["volume"] >= config.RTG_MIN_VOLUME)

                if d_pass and b_pass:
                    # Check C: 2-bar confirmation
                    c_pass = True
                    if confirm_bars >= 2 and i >= 2:
                        prev2 = all_bars[i - 2]
                        c_pass = (prev["close"] > open_price
                                  and prev2["volume"] > 0
                                  and prev["volume"] >= config.RTG_VOLUME_MULT * prev2["volume"]
                                  and prev["volume"] >= config.RTG_MIN_VOLUME)

                    # Check green chase
                    consecutive_green = 0
                    for j in range(i - 1, -1, -1):
                        if all_bars[j]["close"] > all_bars[j]["open"]:
                            consecutive_green += 1
                        else:
                            break
                    chase_pass = consecutive_green < max_green

                    if not d_pass:
                        fail_1pct_gain += 1
                    elif not b_pass:
                        fail_25x_volume += 1
                    elif not c_pass:
                        fail_2bar_confirm += 1
                    elif not chase_pass:
                        fail_green_chase += 1

                    break  # Only check first potential signal

    print(f"Total qualified candidates checked: {total_candidates}")
    print(f"Note: These counts are for the first potential signal bar per candidate")


# ══════════════════════════════════════════════════════════════════
#  ANALYSIS 3: Trailing stop what-if
# ══════════════════════════════════════════════════════════════════

def _run_simulation(data, exit_fn, max_positions=1, scan_interval_sec=300, label=""):
    """Generic simulation engine. exit_fn(bars_after_entry, entry_price, shares, ...)
    returns trade result dict."""
    trading_days = data["trading_days"]
    gap_data = data["gap_data"]
    cached_bars = data["cached_bars"]

    min_rvol = getattr(config, "MIN_RVOL_TO_TRADE", 3.0)
    max_cands = getattr(config, "MAX_CANDIDATES", 40)
    all_in_ratio = getattr(config, "ALL_IN_BP_RATIO", 0.95)
    entry_slippage = getattr(config, "SLIPPAGE_ENTRY_PCT", 0.005)
    force_close_str = getattr(config, "FORCE_CLOSE_TIME", "15:59")
    force_close_min = int(force_close_str.split(":")[0]) * 60 + int(force_close_str.split(":")[1])
    scan_interval_min = scan_interval_sec / 60.0

    equity = config.INITIAL_CAPITAL
    all_trades = []
    daily_pnl = {}

    for date in trading_days:
        date_key = date.date()
        if date_key not in gap_data or gap_data[date_key].empty:
            continue

        candidates = gap_data[date_key].head(max_cands)
        qualified = candidates[candidates["rvol"] >= min_rvol]
        if qualified.empty:
            continue

        start_of_day_equity = equity

        if max_positions == 1:
            # Single position: event-driven loop
            next_search_idx = {row["symbol"]: 0 for _, row in qualified.iterrows()}
            last_exit_bar_min = 0
            daily_loss = 0.0
            max_daily_loss = equity * config.MAX_DAILY_LOSS_PCT

            while True:
                if daily_loss <= -max_daily_loss:
                    break

                max_attempts = getattr(config, "MAX_ENTRY_ATTEMPTS", 3)
                entered = False

                for _, row in qualified.iterrows():
                    if entered or max_attempts <= 0:
                        break

                    symbol = row["symbol"]
                    open_price = row["open_price"]
                    rvol = row.get("rvol", 0)
                    all_bars = cached_bars.get((date_key, symbol))
                    if all_bars is None:
                        max_attempts -= 1
                        continue

                    search_from = next_search_idx.get(symbol, 0)
                    result = find_entry_signal(all_bars, open_price, start_idx=search_from)
                    if result is None:
                        max_attempts -= 1
                        continue

                    entry_price_signal, entry_bar_idx, signal_type = result
                    entry_bar_ts = all_bars[entry_bar_idx]["timestamp"]
                    entry_bar_min_val = entry_bar_ts.hour * 60 + entry_bar_ts.minute

                    if entry_bar_min_val < last_exit_bar_min + scan_interval_min:
                        max_attempts -= 1
                        continue
                    if entry_bar_min_val >= force_close_min - 1:
                        max_attempts -= 1
                        continue

                    entry_bar_close = all_bars[entry_bar_idx]["close"]
                    entry_price_actual = round(entry_bar_close * (1 + entry_slippage), 4)

                    pos_size = equity * all_in_ratio
                    shares = int(pos_size / entry_price_actual)
                    if shares <= 0:
                        max_attempts -= 1
                        continue

                    remaining_bars = all_bars[entry_bar_idx + 1:]
                    cycle_bars = []
                    force_close_price = remaining_bars[-1]["close"] if remaining_bars else entry_price_actual
                    for bar in remaining_bars:
                        bar_min_val = bar["timestamp"].hour * 60 + bar["timestamp"].minute
                        cycle_bars.append(bar)
                        if bar_min_val >= force_close_min:
                            force_close_price = bar["close"]
                            break

                    if not cycle_bars:
                        max_attempts -= 1
                        continue

                    trade_result = exit_fn(
                        entry_price=entry_price_actual, shares=shares,
                        bars_after_entry=cycle_bars, symbol=symbol,
                        open_price=open_price, force_close_price=force_close_price,
                        entry_bar_idx=entry_bar_idx, signal_type=signal_type,
                    )

                    entry_ts = _bar_ts_str(all_bars, entry_bar_idx)

                    all_trades.append({
                        "date": str(date_key), "symbol": symbol,
                        "signal_type": signal_type, "entry_time": entry_ts,
                        "entry_price": entry_price_actual,
                        "exit_price": trade_result["exit_price"],
                        "exit_reason": trade_result["exit_reason"],
                        "pnl": trade_result["pnl"],
                        "pnl_pct": trade_result["pnl_pct"],
                    })

                    equity += trade_result["pnl"]
                    daily_loss += trade_result["pnl"]

                    exit_bar_abs = entry_bar_idx + 1 + trade_result["exit_bar_idx"]
                    next_search_idx[symbol] = exit_bar_abs + 1
                    if exit_bar_abs < len(all_bars):
                        exit_ts = all_bars[exit_bar_abs]["timestamp"]
                        last_exit_bar_min = exit_ts.hour * 60 + exit_ts.minute
                    else:
                        last_exit_bar_min = force_close_min

                    entered = True
                    break

                if not entered:
                    break

        else:
            # Multi-position mode
            positions = []  # list of {symbol, open_price, entry_bar_idx, entry_price, shares, signal_type, all_bars}
            used_symbols = set()
            daily_loss = 0.0
            max_daily_loss = equity * config.MAX_DAILY_LOSS_PCT
            capital_per_pos = equity / max_positions
            n_positions_today = 0

            # Try to enter positions (one pass, top candidates by RVOL)
            for _, row in qualified.iterrows():
                if n_positions_today >= max_positions:
                    break
                if daily_loss <= -max_daily_loss:
                    break

                symbol = row["symbol"]
                if symbol in used_symbols:
                    continue
                open_price = row["open_price"]
                rvol = row.get("rvol", 0)
                all_bars = cached_bars.get((date_key, symbol))
                if all_bars is None:
                    continue

                result = find_entry_signal(all_bars, open_price, start_idx=0)
                if result is None:
                    continue

                entry_price_signal, entry_bar_idx, signal_type = result
                entry_bar_ts = all_bars[entry_bar_idx]["timestamp"]
                entry_bar_min_val = entry_bar_ts.hour * 60 + entry_bar_ts.minute
                if entry_bar_min_val >= force_close_min - 1:
                    continue

                entry_bar_close = all_bars[entry_bar_idx]["close"]
                entry_price_actual = round(entry_bar_close * (1 + entry_slippage), 4)

                pos_size = capital_per_pos * all_in_ratio
                shares = int(pos_size / entry_price_actual)
                if shares <= 0:
                    continue

                positions.append({
                    "symbol": symbol, "open_price": open_price,
                    "entry_bar_idx": entry_bar_idx, "entry_price": entry_price_actual,
                    "shares": shares, "signal_type": signal_type,
                    "all_bars": all_bars,
                })
                used_symbols.add(symbol)
                n_positions_today += 1

            # Evaluate each position
            for pos in positions:
                symbol = pos["symbol"]
                entry_bar_idx = pos["entry_bar_idx"]
                entry_price_actual = pos["entry_price"]
                shares = pos["shares"]
                signal_type = pos["signal_type"]
                open_price = pos["open_price"]
                all_bars = pos["all_bars"]

                remaining_bars = all_bars[entry_bar_idx + 1:]
                cycle_bars = []
                force_close_price = remaining_bars[-1]["close"] if remaining_bars else entry_price_actual
                for bar in remaining_bars:
                    bar_min_val = bar["timestamp"].hour * 60 + bar["timestamp"].minute
                    cycle_bars.append(bar)
                    if bar_min_val >= force_close_min:
                        force_close_price = bar["close"]
                        break

                if not cycle_bars:
                    continue

                trade_result = exit_fn(
                    entry_price=entry_price_actual, shares=shares,
                    bars_after_entry=cycle_bars, symbol=symbol,
                    open_price=open_price, force_close_price=force_close_price,
                    entry_bar_idx=entry_bar_idx, signal_type=signal_type,
                )

                entry_ts = _bar_ts_str(all_bars, entry_bar_idx)

                all_trades.append({
                    "date": str(date_key), "symbol": symbol,
                    "signal_type": signal_type, "entry_time": entry_ts,
                    "entry_price": entry_price_actual,
                    "exit_price": trade_result["exit_price"],
                    "exit_reason": trade_result["exit_reason"],
                    "pnl": trade_result["pnl"],
                    "pnl_pct": trade_result["pnl_pct"],
                })

                equity += trade_result["pnl"]
                daily_loss += trade_result["pnl"]

        daily_pnl[str(date_key)] = equity - start_of_day_equity

    return all_trades, equity, daily_pnl


def analysis_3_trailing_stop(data):
    """Test trailing stop exits at X = 3%, 5%, 8%, 10%."""
    print("\n" + "=" * 90)
    print("ANALYSIS 3: TRAILING STOP WHAT-IF")
    print("=" * 90)

    # Baseline: current bar-based exit
    print("\n--- BASELINE: Bar-based exit (current system) ---")
    baseline_trades, baseline_equity, baseline_daily = _run_simulation(
        data, evaluate_bar_exit, max_positions=1, scan_interval_sec=300, label="baseline"
    )
    _print_summary("Baseline (bar-based exit)", baseline_trades, baseline_equity, baseline_daily)

    # Trailing stop variants
    for trail_pct in [0.03, 0.05, 0.08, 0.10]:
        print(f"\n--- TRAILING STOP {trail_pct:.0%} ---")

        def make_trail_exit(tp):
            def trail_exit(entry_price, shares, bars_after_entry, symbol="",
                           open_price=0.0, force_close_price=None,
                           entry_bar_idx=0, signal_type=""):
                return evaluate_trailing_stop_exit(
                    entry_price, shares, bars_after_entry, tp,
                    symbol=symbol, open_price=open_price,
                    force_close_price=force_close_price,
                    entry_bar_idx=entry_bar_idx, signal_type=signal_type,
                )
            return trail_exit

        trades, equity, daily_pnl = _run_simulation(
            data, make_trail_exit(trail_pct), max_positions=1, scan_interval_sec=300,
            label=f"trail_{trail_pct:.0%}"
        )
        _print_summary(f"Trailing stop {trail_pct:.0%}", trades, equity, daily_pnl)


def _print_summary(label, trades, final_equity, daily_pnl):
    """Print a summary for a set of trades."""
    initial = config.INITIAL_CAPITAL
    total_pnl = final_equity - initial
    total_return = total_pnl / initial

    if not trades:
        print(f"  {label}: NO TRADES")
        return

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / len(trades) if trades else 0
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0

    # Daily P&L
    daily_returns = []
    for d, pnl in daily_pnl.items():
        # Use equity at start of that day (approximate)
        daily_returns.append(pnl / initial)

    avg_daily_return = sum(daily_returns) / len(daily_returns) if daily_returns else 0
    best_day = max(daily_returns) if daily_returns else 0
    worst_day = min(daily_returns) if daily_returns else 0
    positive_days = sum(1 for r in daily_returns if r > 0)
    negative_days = sum(1 for r in daily_returns if r < 0)
    zero_days = sum(1 for r in daily_returns if r == 0)

    reasons = Counter(t["exit_reason"] for t in trades)

    print(f"  {label}:")
    print(f"    Trades: {len(trades)} | Win rate: {win_rate:.1%} ({len(wins)}W/{len(losses)}L)")
    print(f"    Avg win: ${avg_win:+.2f} | Avg loss: ${avg_loss:+.2f}")
    print(f"    Total P&L: ${total_pnl:+.2f} ({total_return:+.2%})")
    print(f"    Final equity: ${final_equity:.2f}")
    print(f"    Daily avg return: {avg_daily_return:+.2%} | Best: {best_day:+.2%} | Worst: {worst_day:+.2%}")
    print(f"    Days: {positive_days} positive, {negative_days} negative, {zero_days} zero")
    print(f"    Exit reasons: {dict(reasons.most_common())}")


# ══════════════════════════════════════════════════════════════════
#  ANALYSIS 4: MAX_POSITIONS=3
# ══════════════════════════════════════════════════════════════════

def analysis_4_max_positions_3(data):
    """Allow up to 3 concurrent positions, splitting capital equally."""
    print("\n" + "=" * 90)
    print("ANALYSIS 4: MAX_POSITIONS=3 WHAT-IF")
    print("=" * 90)

    # Baseline with bar-based exit, max_positions=1
    print("\n--- BASELINE: max_positions=1 ---")
    trades1, eq1, daily1 = _run_simulation(
        data, evaluate_bar_exit, max_positions=1, scan_interval_sec=300, label="max1"
    )
    _print_summary("MAX_POSITIONS=1", trades1, eq1, daily1)

    # MAX_POSITIONS=3 with bar-based exit
    print("\n--- WHAT-IF: max_positions=3 ---")
    trades3, eq3, daily3 = _run_simulation(
        data, evaluate_bar_exit, max_positions=3, scan_interval_sec=300, label="max3"
    )
    _print_summary("MAX_POSITIONS=3", trades3, eq3, daily3)

    # MAX_POSITIONS=3 with 5% trailing stop
    def trail_5pct_exit(entry_price, shares, bars_after_entry, symbol="",
                        open_price=0.0, force_close_price=None,
                        entry_bar_idx=0, signal_type=""):
        return evaluate_trailing_stop_exit(
            entry_price, shares, bars_after_entry, 0.05,
            symbol=symbol, open_price=open_price,
            force_close_price=force_close_price,
            entry_bar_idx=entry_bar_idx, signal_type=signal_type,
        )

    print("\n--- WHAT-IF: max_positions=3 + trailing 5% ---")
    trades3t, eq3t, daily3t = _run_simulation(
        data, trail_5pct_exit, max_positions=3, scan_interval_sec=300, label="max3_trail5"
    )
    _print_summary("MAX_POSITIONS=3 + trail 5%", trades3t, eq3t, daily3t)


# ══════════════════════════════════════════════════════════════════
#  ANALYSIS 5: SCAN_INTERVAL_SEC=30
# ══════════════════════════════════════════════════════════════════

def analysis_5_faster_scan(data):
    """Test SCAN_INTERVAL_SEC=30 instead of 300."""
    print("\n" + "=" * 90)
    print("ANALYSIS 5: SCAN_INTERVAL_SEC=30 WHAT-IF")
    print("=" * 90)

    # Baseline: scan_interval=300
    print("\n--- BASELINE: scan_interval=300s (5 min) ---")
    trades300, eq300, daily300 = _run_simulation(
        data, evaluate_bar_exit, max_positions=1, scan_interval_sec=300, label="scan300"
    )
    _print_summary("Scan 300s", trades300, eq300, daily300)

    # Faster: scan_interval=30
    print("\n--- WHAT-IF: scan_interval=30s ---")
    trades30, eq30, daily30 = _run_simulation(
        data, evaluate_bar_exit, max_positions=1, scan_interval_sec=30, label="scan30"
    )
    _print_summary("Scan 30s", trades30, eq30, daily30)

    # Also combine: scan=30 + trailing 5%
    def trail_5pct_exit(entry_price, shares, bars_after_entry, symbol="",
                        open_price=0.0, force_close_price=None,
                        entry_bar_idx=0, signal_type=""):
        return evaluate_trailing_stop_exit(
            entry_price, shares, bars_after_entry, 0.05,
            symbol=symbol, open_price=open_price,
            force_close_price=force_close_price,
            entry_bar_idx=entry_bar_idx, signal_type=signal_type,
        )

    print("\n--- WHAT-IF: scan_interval=30s + trailing 5% ---")
    trades30t, eq30t, daily30t = _run_simulation(
        data, trail_5pct_exit, max_positions=1, scan_interval_sec=30, label="scan30_trail5"
    )
    _print_summary("Scan 30s + trail 5%", trades30t, eq30t, daily30t)

    # Ultimate combo: scan=30 + trailing 5% + max_positions=3
    print("\n--- ULTIMATE: scan=30s + trail 5% + max_positions=3 ---")
    trades_ult, eq_ult, daily_ult = _run_simulation(
        data, trail_5pct_exit, max_positions=3, scan_interval_sec=30, label="ultimate"
    )
    _print_summary("Scan 30s + trail 5% + max 3 pos", trades_ult, eq_ult, daily_ult)


# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=config.BACKTEST_DAYS)
    parser.add_argument("--end", type=str, default=None)
    parser.add_argument("--analysis", type=int, default=0,
                        help="Run specific analysis (1-5), 0=all")
    args = parser.parse_args()

    end = pd.Timestamp(args.end, tz="America/New_York") if args.end else None

    print("Loading data...")
    data = load_all_data(end_date=end, n_days=args.days)
    if data is None:
        sys.exit(1)

    if args.analysis == 0 or args.analysis == 1:
        analysis_1_capture_ratio(data)
    if args.analysis == 0 or args.analysis == 2:
        analysis_2_zero_trade_days(data)
    if args.analysis == 0 or args.analysis == 3:
        analysis_3_trailing_stop(data)
    if args.analysis == 0 or args.analysis == 4:
        analysis_4_max_positions_3(data)
    if args.analysis == 0 or args.analysis == 5:
        analysis_5_faster_scan(data)

    # Final summary across all what-if scenarios
    print("\n" + "=" * 90)
    print("CROSS-ANALYSIS SUMMARY: Getting to 15-30% DAILY")
    print("=" * 90)

    initial = config.INITIAL_CAPITAL

    # Re-run all variants for a clean comparison table
    print(f"\nInitial capital: ${initial:.2f}")
    print(f"Target: 15-30% daily return = ${initial * 0.15:.2f} - ${initial * 0.30:.2f} per day")

    variants = [
        ("Baseline (bar exit, 1 pos, 5min scan)", evaluate_bar_exit, 1, 300, None),
    ]

    for tp in [0.03, 0.05, 0.08, 0.10]:
        def make_trail(tp_val):
            def fn(entry_price, shares, bars_after_entry, symbol="",
                    open_price=0.0, force_close_price=None,
                    entry_bar_idx=0, signal_type=""):
                return evaluate_trailing_stop_exit(
                    entry_price, shares, bars_after_entry, tp_val,
                    symbol=symbol, open_price=open_price,
                    force_close_price=force_close_price,
                    entry_bar_idx=entry_bar_idx, signal_type=signal_type,
                )
            return fn
        variants.append((f"Trail {tp:.0%}, 1 pos, 5min scan", make_trail(tp), 1, 300, None))

    # MAX_POSITIONS=3
    variants.append(("Bar exit, 3 pos, 5min scan", evaluate_bar_exit, 3, 300, None))

    # Scan=30
    variants.append(("Bar exit, 1 pos, 30s scan", evaluate_bar_exit, 1, 30, None))

    # Best combos
    def trail_5_fn(entry_price, shares, bars_after_entry, symbol="",
                    open_price=0.0, force_close_price=None,
                    entry_bar_idx=0, signal_type=""):
        return evaluate_trailing_stop_exit(
            entry_price, shares, bars_after_entry, 0.05,
            symbol=symbol, open_price=open_price,
            force_close_price=force_close_price,
            entry_bar_idx=entry_bar_idx, signal_type=signal_type,
        )
    variants.append(("Trail 5%, 1 pos, 30s scan", trail_5_fn, 1, 30, None))
    variants.append(("Trail 5%, 3 pos, 5min scan", trail_5_fn, 3, 300, None))
    variants.append(("Trail 5%, 3 pos, 30s scan", trail_5_fn, 3, 30, None))

    def trail_8_fn(entry_price, shares, bars_after_entry, symbol="",
                    open_price=0.0, force_close_price=None,
                    entry_bar_idx=0, signal_type=""):
        return evaluate_trailing_stop_exit(
            entry_price, shares, bars_after_entry, 0.08,
            symbol=symbol, open_price=open_price,
            force_close_price=force_close_price,
            entry_bar_idx=entry_bar_idx, signal_type=signal_type,
        )
    variants.append(("Trail 8%, 3 pos, 30s scan", trail_8_fn, 3, 30, None))

    print(f"\n{'Variant':<40} {'Trades':>6} {'TotalRet':>9} {'FinalEq':>10} {'AvgDaily':>9} {'BestDay':>9} {'WorstDay':>9}")
    print("-" * 100)

    for label, exit_fn, max_pos, scan_sec, _ in variants:
        trades, eq, daily = _run_simulation(data, exit_fn, max_positions=max_pos,
                                            scan_interval_sec=scan_sec, label=label)
        total_ret = (eq - initial) / initial
        daily_returns = [p / initial for p in daily.values()]
        avg_daily = sum(daily_returns) / len(daily_returns) if daily_returns else 0
        best_day = max(daily_returns) if daily_returns else 0
        worst_day = min(daily_returns) if daily_returns else 0
        print(f"{label:<40} {len(trades):>6} {total_ret:>+8.2%} ${eq:>9.2f} {avg_daily:>+8.3%} {best_day:>+8.2%} {worst_day:>+8.2%}")
