"""Strategy — stonewang_daytrade_momentum_surge_1.0

Exit: 5% hard stop + trailing stop (3% after +5% activation) + 10-min time limit + EOD force close.
Entry signals: RTG (gap-up), momentum (intraday breakout), reversal (gap-down reversal).
"""

import json
import os
import re
import sys
import datetime as _dt
import importlib.util

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


# ── Helper functions (same as rtg_2mins_all) ────────────────────────

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
                    prev_vol = int(prev["volume"])
                    gap_day_vol = int(curr["volume"])
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
                    lookback_start = max(0, i - config.RVOL_LOOKBACK_DAYS - 1)
                    prior_vols = [int(sym_df.iloc[j]["volume"]) for j in range(lookback_start, i)]
                    avg_vol_20d = sum(prior_vols) / len(prior_vols) if prior_vols else 0
                    rvol = gap_day_vol / avg_vol_20d if avg_vol_20d > 0 else 0

                    atr_period = getattr(config, "ATR_PERIOD", 14)
                    atr_lookback_start = max(0, i - atr_period - 1)
                    true_ranges = []
                    for j in range(atr_lookback_start + 1, i):
                        if j < 1:
                            continue
                        bar_j = sym_df.iloc[j]
                        bar_jm1 = sym_df.iloc[j - 1]
                        tr = max(
                            float(bar_j["high"]) - float(bar_j["low"]),
                            abs(float(bar_j["high"]) - float(bar_jm1["close"])),
                            abs(float(bar_j["low"]) - float(bar_jm1["close"])),
                        )
                        true_ranges.append(tr)
                    atr = sum(true_ranges) / len(true_ranges) if true_ranges else 0

                    if symbol not in symbol_data:
                        symbol_data[symbol] = []
                    symbol_data[symbol].append({
                        "date": curr_date, "open_price": open_price,
                        "prev_close": prev_close, "gap_pct": gap_pct,
                        "volume": gap_day_vol, "dollar_volume": dollar_volume,
                        "rvol": rvol, "atr": atr,
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


def bulk_scan_gaps_down(client, trading_days, symbols):
    """Scan for gap-down stocks (-3% to -10%) with high RVOL for reversal entries."""
    start = trading_days[0] - pd.Timedelta(days=45)
    end = trading_days[-1] + pd.Timedelta(days=1)
    all_dates_set = {d.date() for d in trading_days}

    gap_min = getattr(config, "REVERSAL_GAP_MIN", -0.10)
    gap_max = getattr(config, "REVERSAL_GAP_MAX", -0.03)
    min_rvol = getattr(config, "REVERSAL_MIN_RVOL", 3.0)
    max_cands = getattr(config, "REVERSAL_MAX_CANDIDATES", 20)
    price_min = getattr(config, "REVERSAL_PRICE_MIN", 3.0)
    price_max = getattr(config, "REVERSAL_PRICE_MAX", 20.0)

    batch_size = 500
    symbol_data = {}
    total_batches = (len(symbols) + batch_size - 1) // batch_size

    for batch_idx in range(total_batches):
        batch = symbols[batch_idx * batch_size: (batch_idx + 1) * batch_size]
        if batch_idx % 5 == 0:
            print(f"  Gap-down scan batch {batch_idx + 1}/{total_batches}...")

        request = StockBarsRequest(
            symbol_or_symbols=batch, timeframe=TimeFrame.Day,
            start=start, end=end, adjustment=Adjustment.RAW,
            feed=getattr(config, "DATA_FEED_OBJ", DataFeed.IEX),
        )
        try:
            bars = client.get_stock_bars(request)
        except Exception as e:
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
                    prev_vol = int(prev["volume"])
                    gap_day_vol = int(curr["volume"])
                    if prev_close <= 0:
                        continue
                    gap_pct = (open_price / prev_close) - 1.0
                    if not (gap_min <= gap_pct <= gap_max):
                        continue
                    if not (price_min <= open_price <= price_max):
                        continue
                    if prev_vol < config.MIN_VOLUME:
                        continue
                    lookback_start = max(0, i - config.RVOL_LOOKBACK_DAYS - 1)
                    prior_vols = [int(sym_df.iloc[j]["volume"]) for j in range(lookback_start, i)]
                    avg_vol_20d = sum(prior_vols) / len(prior_vols) if prior_vols else 0
                    rvol = gap_day_vol / avg_vol_20d if avg_vol_20d > 0 else 0
                    if rvol < min_rvol:
                        continue

                    atr_period = getattr(config, "ATR_PERIOD", 14)
                    atr_lookback_start = max(0, i - atr_period - 1)
                    true_ranges = []
                    for j in range(atr_lookback_start + 1, i):
                        if j < 1:
                            continue
                        bar_j = sym_df.iloc[j]
                        bar_jm1 = sym_df.iloc[j - 1]
                        tr = max(
                            float(bar_j["high"]) - float(bar_j["low"]),
                            abs(float(bar_j["high"]) - float(bar_jm1["close"])),
                            abs(float(bar_j["low"]) - float(bar_jm1["close"])),
                        )
                        true_ranges.append(tr)
                    atr = sum(true_ranges) / len(true_ranges) if true_ranges else 0

                    if symbol not in symbol_data:
                        symbol_data[symbol] = []
                    symbol_data[symbol].append({
                        "date": curr_date, "open_price": open_price,
                        "prev_close": prev_close, "gap_pct": gap_pct,
                        "volume": gap_day_vol, "dollar_volume": prev_close * prev_vol,
                        "rvol": rvol, "atr": atr,
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
    """Find RTG entry on 1-min bars (full scan)."""
    if bars_1m.empty or len(bars_1m) < 2:
        return 0.0, -1, False, ""
    if min_volume is None:
        min_volume = config.RTG_MIN_VOLUME

    entry_start_str = getattr(config, "ENTRY_WINDOW_START", "09:30")
    entry_end_str = getattr(config, "ENTRY_WINDOW_END", "15:30")

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

        bar_close = float(bar["close"])
        bar_vol = int(bar["volume"])
        prev_vol = int(prev_bar["volume"])

        if (bar_close > open_price
                and prev_vol > 0
                and bar_vol >= config.RTG_VOLUME_MULT * prev_vol
                and bar_vol >= min_volume):
            entry_at_open = round(open_price * 1.001, 4)
            entry_at_close = round(bar_close * 1.001, 4)
            return entry_at_open, entry_at_close, i, True, "rtg"

    return 0.0, 0.0, -1, False, ""


def find_rtg_entry_instant_1min(bars_1m, open_price, min_volume=None, require_above_open=True):
    """Find RTG entry on the last 3 bars only."""
    if bars_1m.empty or len(bars_1m) < 2:
        return 0.0, 0.0, -1, False, ""
    if min_volume is None:
        min_volume = config.RTG_MIN_VOLUME

    entry_start_str = getattr(config, "ENTRY_WINDOW_START", "09:30")
    entry_end_str = getattr(config, "ENTRY_WINDOW_END", "15:30")

    start_idx = max(1, len(bars_1m) - 3)
    for i in range(start_idx, len(bars_1m)):
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

        bar_close = float(bar["close"])
        bar_open = float(bar["open"])
        bar_vol = int(bar["volume"])
        prev_vol = int(prev_bar["volume"])

        price_ok = (bar_close > open_price) if require_above_open else (bar_close > bar_open)
        if (price_ok
                and prev_vol > 0
                and bar_vol >= config.RTG_VOLUME_MULT * prev_vol
                and bar_vol >= min_volume):
            entry_at_open = round(open_price * 1.001, 4)
            entry_at_close = round(bar_close * 1.001, 4)
            return entry_at_open, entry_at_close, i, True, "rtg"

    return 0.0, 0.0, -1, False, ""


def find_reversal_entry_1min(bars_1m, open_price, min_volume=None):
    """Find low-open reversal entry: close crosses above open_price + volume spike."""
    if bars_1m.empty or len(bars_1m) < 2:
        return 0.0, 0.0, -1, False, ""
    if min_volume is None:
        min_volume = config.RTG_MIN_VOLUME

    entry_start_str = getattr(config, "ENTRY_WINDOW_START", "09:30")
    entry_end_str = getattr(config, "ENTRY_WINDOW_END", "15:30")

    start_idx = max(1, len(bars_1m) - 3)
    for i in range(start_idx, len(bars_1m)):
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

        bc = float(bar["close"])
        bo = float(bar["open"])
        bv = int(bar["volume"])
        pv = int(prev_bar["volume"])

        # Must be bullish, cross above open_price, volume spike
        if bc <= bo:
            continue
        if bc <= open_price:
            continue
        if pv <= 0 or bv < config.RTG_VOLUME_MULT * pv:
            continue
        if bv < min_volume:
            continue

        entry_at_open = round(open_price * 1.001, 4)
        entry_at_close = round(bc * 1.001, 4)
        return entry_at_open, entry_at_close, i, True, "reversal"

    return 0.0, 0.0, -1, False, ""


def _list_to_bars_1m(bars_list):
    """Convert bars_list back to DataFrame."""
    if not bars_list:
        return pd.DataFrame()
    data = {
        "open": [b["open"] for b in bars_list],
        "high": [b["high"] for b in bars_list],
        "low": [b["low"] for b in bars_list],
        "close": [b["close"] for b in bars_list],
        "volume": [b["volume"] for b in bars_list],
    }
    index = pd.DatetimeIndex([b["timestamp"] for b in bars_list], name="timestamp")
    return pd.DataFrame(data, index=index)


def _get_rvol_tier(rvol):
    rvol = min(rvol, getattr(config, "RVOL_SIZING_CAP", 10.0))
    tiers = getattr(config, "RVOL_SIZING_TIERS", [(10.0, 0.50), (5.0, 0.30), (0.0, 0.15)])
    for rvol_min, pct in tiers:
        if rvol >= rvol_min:
            return rvol_min, pct
    return 0.0, 0.15


def get_rvol_sizing(rvol, equity, same_tier_count=1):
    _, pct = _get_rvol_tier(rvol)
    split_pct = pct / max(same_tier_count, 1)
    return round(equity * split_pct, 2)


# ── OpenPosition with trailing stop ──────────────────────────────────

class OpenPosition:
    """Tracks a single open position during bar-by-bar simulation.

    Exit: 5% hard stop + trailing stop (3% after +5% activation) + 10-min time limit.
    """
    def __init__(self, symbol, shares, entry_price, entry_bar_idx, open_price,
                 rvol, atr, stop_pct, signal_type,
                 trail_activate_pct=0.05, trail_pct=0.03):
        self.symbol = symbol
        self.shares = shares
        self.entry_price = entry_price
        self.entry_bar_idx = entry_bar_idx
        self.open_price = open_price
        self.rvol = rvol
        self.atr = atr
        self.stop_pct = stop_pct
        self.signal_type = signal_type

        self.stop_price = round(entry_price * (1 - stop_pct), 4)
        self.highest = entry_price
        self.trail_active = False
        self.trail_activate_pct = trail_activate_pct
        self.trail_pct = trail_pct

        self.closed = False
        self.exit_price = 0.0
        self.exit_reason = ""
        self.exit_bar_idx = -1

    def evaluate_bar(self, bar, bar_idx):
        """Evaluate a single bar. Returns True if position exited."""
        if self.closed:
            return False

        bar_low = float(bar["low"])
        bar_high = float(bar["high"])
        bar_close = float(bar["close"])
        bar_open = float(bar["open"])

        exit_slippage = getattr(config, "SLIPPAGE_EXIT_PCT", 0.0)

        # Update highest
        if bar_high > self.highest:
            self.highest = bar_high

        # 1. Hard stop (with gap-through model)
        if bar_low <= self.stop_price:
            if bar_open < self.stop_price:
                self.exit_price = round(bar_open * (1 - exit_slippage), 4)
            else:
                self.exit_price = self.stop_price
            self.exit_reason = "stop_loss"
            self.exit_bar_idx = bar_idx
            self.closed = True
            return True

        # 2. Trailing stop
        if getattr(config, "TRAILING_STOP_ENABLED", False):
            if not self.trail_active and self.highest >= self.entry_price * (1 + self.trail_activate_pct):
                self.trail_active = True

            if self.trail_active:
                trail_stop_price = round(self.highest * (1 - self.trail_pct), 4)
                if bar_low <= trail_stop_price:
                    if bar_open < trail_stop_price:
                        self.exit_price = round(bar_open * (1 - exit_slippage), 4)
                    else:
                        self.exit_price = trail_stop_price
                    self.exit_reason = "trailing_stop"
                    self.exit_bar_idx = bar_idx
                    self.closed = True
                    return True

        # 3. Time limit
        time_limit_sec = getattr(config, "RTG_TIME_LIMIT_SEC", 600)
        time_limit_bars = max(1, time_limit_sec // 60)
        if bar_idx - self.entry_bar_idx >= time_limit_bars:
            self.exit_price = round(bar_close * (1 - exit_slippage), 4)
            self.exit_reason = "time_limit"
            self.exit_bar_idx = bar_idx
            self.closed = True
            return True

        return False

    def force_close(self, price, bar_idx, reason="force_close"):
        if reason == "force_close":
            slippage = getattr(config, "SLIPPAGE_FORCE_CLOSE_PCT", 0.01)
        else:
            slippage = getattr(config, "SLIPPAGE_EXIT_PCT", 0.0)
        if slippage > 0:
            price = round(price * (1 - slippage), 4)
        self.exit_price = price
        self.exit_reason = reason
        self.exit_bar_idx = bar_idx
        self.closed = True

    @property
    def unrealized_pnl(self):
        if self.closed:
            return round((self.exit_price - self.entry_price) * self.shares, 2)
        return 0.0

    @property
    def pnl(self):
        if not self.closed:
            return 0.0
        sale_proceeds = self.exit_price * self.shares
        reg_fees = round(sale_proceeds * 0.000027 + self.shares * 0.000166, 4)
        return round((self.exit_price - self.entry_price) * self.shares - reg_fees, 2)

    @property
    def pnl_pct(self):
        if not self.closed or self.entry_price <= 0:
            return 0.0
        return round(self.pnl / (self.entry_price * self.shares), 4)


def save_backtest_charts(chart_entries, filepath="versions/chart_data_momentum_surge.json"):
    date_parts = sorted(set(v["date"] for v in chart_entries.values()))
    date_range = f"{date_parts[0]} to {date_parts[-1]}" if len(date_parts) > 1 else date_parts[0]
    output = {"date": date_range, "symbols": chart_entries}
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Chart data saved to {filepath} ({len(chart_entries)} symbols)")


# ── TradeResult ──────────────────────────────────────────────────────

class TradeResult:
    def __init__(self, symbol, date, entry_price, exit_price, shares,
                 pnl, pnl_pct, exit_reason, open_price, stop_price,
                 target_price, trailing_high, exit_bar_idx, entry_bar_idx,
                 position_size, signal_type=""):
        self.symbol = symbol
        self.date = date
        self.entry_price = entry_price
        self.exit_price = exit_price
        self.shares = shares
        self.pnl = pnl
        self.pnl_pct = pnl_pct
        self.exit_reason = exit_reason
        self.open_price = open_price
        self.stop_price = stop_price
        self.target_price = target_price
        self.trailing_high = trailing_high
        self.exit_bar_idx = exit_bar_idx
        self.entry_bar_idx = entry_bar_idx
        self.position_size = position_size
        self.signal_type = signal_type


# ── Backtest ─────────────────────────────────────────────────────────

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

    print(f"[momentum_surge] Backtesting {len(trading_days)} trading days: "
          f"{trading_days[0].date()} to {trading_days[-1].date()}")
    print(f"Capital: ${config.INITIAL_CAPITAL:,.2f} | Max concurrent: {config.MAX_POSITIONS}")
    print(f"Entry: Prong1 RTG (gap>={config.GAP_THRESHOLD:.0%}) + Prong2 Momentum + Prong3 Reversal")
    print(f"Exit: {config.RTG_STOP_PCT:.0%} stop + trailing {config.TRAILING_ACTIVATE_PCT:.0%}/{config.TRAILING_PCT:.0%} + {config.RTG_TIME_LIMIT_SEC//60}min limit")
    print(f"Daily loss breaker: {config.MAX_DAILY_LOSS_PCT:.0%}")

    print("\nLoading tradable symbols...")
    symbols = get_tradable_symbols()
    print(f"Found {len(symbols)} tradable symbols")

    symbols = [s for s in symbols if not is_leveraged_etf(s)]
    symbols = [s for s in symbols if not is_crypto_etf(s)]
    print(f"After ETF filters: {len(symbols)} symbols")

    print("\nBulk scanning for gap-up and gap-down...")
    gap_data = bulk_scan_gaps(client, trading_days, symbols)

    reversal_data = {}
    if getattr(config, "REVERSAL_SCAN_ENABLED", False):
        reversal_data = bulk_scan_gaps_down(client, trading_days, symbols)

    total_gap = sum(len(v) for v in gap_data.values())
    total_rev = sum(len(v) for v in reversal_data.values())
    print(f"Found {total_gap} gap-up + {total_rev} gap-down entries")

    all_trades = []
    equity = config.INITIAL_CAPITAL
    chart_entries = {}

    for date in trading_days:
        date_key = date.date()
        gap_cands = gap_data.get(date_key, pd.DataFrame())
        rev_cands = reversal_data.get(date_key, pd.DataFrame())

        # Combine candidates
        candidates = pd.DataFrame()
        if not gap_cands.empty and not rev_cands.empty:
            candidates = pd.concat([gap_cands, rev_cands], ignore_index=True)
            candidates = candidates.sort_values("rvol", ascending=False).reset_index(drop=True)
        elif not gap_cands.empty:
            candidates = gap_cands
        elif not rev_cands.empty:
            candidates = rev_cands

        if candidates.empty:
            continue

        max_cands = getattr(config, "MAX_CANDIDATES", 100)
        candidates = candidates.head(max_cands)

        n_cands = len(candidates)
        n_rev = len(rev_cands[rev_cands["gap_pct"] < 0]) if not rev_cands.empty else 0
        n_gap = n_cands - n_rev
        print(f"\n--- {date_key} ({n_gap} gap-up + {n_rev} reversal, equity: ${equity:,.2f}) ---")

        for _, row in candidates.iterrows():
            sym = row["symbol"]
            rvol = row.get("rvol", 0)
            gap_pct = row["gap_pct"]
            open_p = row["open_price"]
            sig = "reversal" if gap_pct < 0 else "rtg"
            print(f"  [{sig.upper()}] {sym} gap={gap_pct:+.1%} RVOL={rvol:.1f}× open=${open_p:.2f}")

        # Pre-fetch 1-min bars
        cached_bars = {}
        entry_info = {}
        candidate_atrs = {}
        tier_counts = {}

        for _, row in candidates.iterrows():
            symbol = row["symbol"]
            open_price = row["open_price"]
            rvol = row.get("rvol", 0)
            if rvol >= 5.0:
                tier_key = _get_rvol_tier(rvol)[0]
                tier_counts[tier_key] = tier_counts.get(tier_key, 0) + 1

            if symbol in cached_bars:
                continue
            bars_1m = get_1min_bars(client, symbol, date)
            if bars_1m.empty or len(bars_1m) < 2:
                continue
            bars_list = _bars_to_list(bars_1m)
            cached_bars[symbol] = bars_list
            atr_v = row.get("atr", 0)
            if atr_v > 0:
                candidate_atrs[symbol] = atr_v

        # Find initial entry signals
        for _, row in candidates.iterrows():
            symbol = row["symbol"]
            open_price = row["open_price"]
            rvol = row.get("rvol", 0)
            gap_pct = row["gap_pct"]

            if symbol not in cached_bars:
                continue

            min_vol = config.RTG_MIN_VOLUME
            if rvol >= 10:
                min_vol = max(config.RTG_MIN_VOLUME // 3, 5000)
            elif rvol >= 5:
                min_vol = max(config.RTG_MIN_VOLUME // 2, 10000)

            bars_1m_df = _list_to_bars_1m(cached_bars[symbol])

            # Dispatch entry by signal type
            if gap_pct < 0:
                # Prong 3: reversal entry
                entry_at_open, entry_at_close, entry_bar_idx, confirmed, signal_type = find_reversal_entry_1min(
                    bars_1m_df, open_price, min_volume=min_vol)
            else:
                # Prong 1: RTG entry
                entry_at_open, entry_at_close, entry_bar_idx, confirmed, signal_type = find_rtg_entry_1min(
                    bars_1m_df, open_price, min_volume=min_vol)

            if confirmed and entry_at_open > 0:
                entry_info[symbol] = (entry_at_open, entry_at_close, entry_bar_idx, signal_type, rvol, open_price, abs(gap_pct))

        if not entry_info and not cached_bars:
            continue

        # Bar-by-bar simulation
        daily_start_equity = equity
        max_daily_loss = equity * config.MAX_DAILY_LOSS_PCT
        daily_realized_pnl = 0.0

        sorted_entries = sorted(entry_info.items(), key=lambda x: x[1][2])

        open_positions = []
        closed_trades = []
        entered_symbols = set()
        entry_slippage = getattr(config, "SLIPPAGE_ENTRY_PCT", 0.005)
        entry_count = {}
        last_exit_bar = {}
        stop_exit_bar = {}

        for symbol, (entry_at_open, entry_at_close, entry_bar_idx, signal_type, rvol, open_price, gap_p) in sorted_entries:
            if symbol in entered_symbols:
                continue
            if rvol < getattr(config, "RTG_MIN_RVOL", 0):
                continue
            if len(entered_symbols) >= config.MAX_POSITIONS:
                break

            same_tier = tier_counts.get(_get_rvol_tier(rvol)[0], 1)
            atr = candidate_atrs.get(symbol, 0)

            stop_p = config.RTG_STOP_PCT
            trail_act = config.TRAILING_ACTIVATE_PCT if getattr(config, "TRAILING_STOP_ENABLED", False) else 0
            trail_p = config.TRAILING_PCT if getattr(config, "TRAILING_STOP_ENABLED", False) else 0

            current_equity = daily_start_equity + sum(p.pnl for p in closed_trades)
            entry_price_actual = round(entry_at_open * (1 + entry_slippage), 4)
            sizing_price = round(entry_at_close * (1 + entry_slippage), 4)
            if config.MAX_POSITIONS <= 1:
                pos_size = max(config.MIN_POSITION_SIZE, current_equity)
            else:
                pos_size = get_rvol_sizing(rvol, current_equity, same_tier_count=same_tier)
                pos_size = max(config.MIN_POSITION_SIZE, pos_size)
            pos_size = min(pos_size, current_equity * 0.95)
            shares = int(pos_size / sizing_price)
            if shares <= 0:
                continue

            pos = OpenPosition(
                symbol=symbol, shares=shares, entry_price=entry_price_actual,
                entry_bar_idx=entry_bar_idx, open_price=open_price, rvol=rvol,
                atr=atr, stop_pct=stop_p, signal_type=signal_type,
                trail_activate_pct=trail_act, trail_pct=trail_p,
            )
            open_positions.append(pos)
            entered_symbols.add(symbol)
            entry_count[symbol] = entry_count.get(symbol, 0) + 1
            entry_ts_str = _bar_ts_str(cached_bars[symbol], entry_bar_idx)
            print(f"  {symbol} [{signal_type}] entry=${entry_price_actual:.4f}@{entry_ts_str} "
                  f"{shares}sh [RVOL={rvol:.1f}× stop={stop_p:.0%} trail={trail_act:.0%}/{trail_p:.0%}]")

        # Build global bar timeline
        all_bar_times = {}
        all_tracked_symbols = set(entered_symbols) | set(cached_bars.keys())
        for symbol in all_tracked_symbols:
            bars = cached_bars.get(symbol, [])
            for bar in bars:
                ts = bar["timestamp"]
                if ts not in all_bar_times:
                    all_bar_times[ts] = {}
                all_bar_times[ts][symbol] = bar

        sorted_times = sorted(all_bar_times.keys())

        # Simulate bar by bar
        for ts in sorted_times:
            bars_this_min = all_bar_times[ts]

            # Evaluate each open position
            for pos in open_positions:
                if pos.closed:
                    continue
                if pos.symbol not in bars_this_min:
                    continue
                bar = bars_this_min[pos.symbol]
                bar_idx = None
                bars = cached_bars.get(pos.symbol, [])
                for bi, b in enumerate(bars):
                    if b["timestamp"] == ts:
                        bar_idx = bi
                        break
                if bar_idx is None:
                    continue

                exited = pos.evaluate_bar(bar, bar_idx)
                if exited:
                    closed_trades.append(pos)
                    last_exit_bar[pos.symbol] = bar_idx
                    if pos.exit_reason == "stop_loss":
                        stop_exit_bar[pos.symbol] = bar_idx
                    if getattr(config, "RTG_REENTRY_ALLOWED", False):
                        entered_symbols.discard(pos.symbol)

            # Daily loss circuit breaker
            realized_so_far = sum(p.pnl for p in closed_trades)
            if max_daily_loss > 0 and realized_so_far <= -max_daily_loss:
                for p in open_positions:
                    if not p.closed:
                        bar = bars_this_min.get(p.symbol, {})
                        if "close" in bar:
                            p.force_close(bar["close"], bar_idx or 0, "daily_loss_limit")
                            closed_trades.append(p)
                print(f"  Daily loss ${realized_so_far:,.2f} exceeded limit, stopping")
                break

            # Re-entry: after position exits, check for new entry
            n_open = sum(1 for p in open_positions if not p.closed)
            if n_open < config.MAX_POSITIONS and len(closed_trades) > 0:
                bar_time = ts.time() if hasattr(ts, 'time') else None
                entry_end_str = getattr(config, "ENTRY_WINDOW_END", "15:30")
                entry_end_h, entry_end_m = (int(x) for x in entry_end_str.split(":"))
                entry_end_time = _dt.time(entry_end_h, entry_end_m)
                mkt_open_time = _dt.time(9, 30)

                if bar_time and mkt_open_time <= bar_time <= entry_end_time:
                    for _, row in candidates.iterrows():
                        if n_open >= config.MAX_POSITIONS:
                            break
                        sym = row["symbol"]
                        rvol = row.get("rvol", 0)
                        if rvol < getattr(config, "RTG_MIN_RVOL", 0):
                            continue
                        if sym in entered_symbols:
                            continue
                        # Re-entry checks
                        is_reentry = sym in last_exit_bar
                        if is_reentry and not getattr(config, "RTG_REENTRY_ALLOWED", False):
                            continue
                        if is_reentry and getattr(config, "RTG_REENTRY_MAX", 0) > 0 and entry_count.get(sym, 0) >= getattr(config, "RTG_REENTRY_MAX", 3):
                            continue
                        if is_reentry and sym in last_exit_bar:
                            bars_since_exit = bar_idx - last_exit_bar[sym]
                            reentry_cd_bars = max(1, getattr(config, "REENTRY_COOLDOWN_SEC", 60) // 60)
                            if sym in stop_exit_bar and stop_exit_bar[sym] == last_exit_bar[sym]:
                                reentry_cd_bars = max(1, getattr(config, "REENTRY_STOP_COOLDOWN_SEC", 120) // 60)
                            if bars_since_exit < reentry_cd_bars:
                                continue
                        if is_reentry:
                            max_reentry_price = row["open_price"] * getattr(config, "REENTRY_MAX_PRICE_VS_OPEN", 1.15)
                        if sym not in cached_bars:
                            continue
                        bars_1m_sym = _list_to_bars_1m(cached_bars[sym])
                        min_vol = config.RTG_MIN_VOLUME
                        if rvol >= 10:
                            min_vol = max(config.RTG_MIN_VOLUME // 3, 5000)
                        elif rvol >= 5:
                            min_vol = max(config.RTG_MIN_VOLUME // 2, 10000)

                        # Dispatch entry by signal type
                        gap_pct = row["gap_pct"]
                        is_morning = bar_time < _dt.time(10, 30)
                        if gap_pct < 0:
                            entry_at_open, entry_at_close, entry_bar_idx, confirmed, signal_type = find_reversal_entry_1min(
                                bars_1m_sym, row["open_price"], min_volume=min_vol)
                        else:
                            entry_at_open, entry_at_close, entry_bar_idx, confirmed, signal_type = find_rtg_entry_instant_1min(
                                bars_1m_sym, row["open_price"], min_volume=min_vol, require_above_open=is_morning)
                            if confirmed:
                                signal_type = signal_type  # keep as rtg

                        if not confirmed or entry_at_open <= 0:
                            continue
                        if is_reentry:
                            max_reentry_price = row["open_price"] * getattr(config, "REENTRY_MAX_PRICE_VS_OPEN", 1.15)
                            if entry_at_open > max_reentry_price:
                                continue

                        current_equity = daily_start_equity + sum(p.pnl for p in closed_trades)
                        entry_price_actual = round(entry_at_open * (1 + entry_slippage), 4)
                        sizing_price = round(entry_at_close * (1 + entry_slippage), 4)
                        if config.MAX_POSITIONS <= 1:
                            pos_size = max(config.MIN_POSITION_SIZE, current_equity)
                        else:
                            same_tier = tier_counts.get(_get_rvol_tier(rvol)[0], 1)
                            pos_size = max(config.MIN_POSITION_SIZE, get_rvol_sizing(rvol, current_equity, same_tier_count=same_tier))
                        pos_size = min(pos_size, current_equity * 0.95)
                        shares = int(pos_size / sizing_price)
                        if shares <= 0:
                            continue
                        atr = candidate_atrs.get(sym, 0)
                        stop_p = config.RTG_STOP_PCT
                        trail_act = config.TRAILING_ACTIVATE_PCT if getattr(config, "TRAILING_STOP_ENABLED", False) else 0
                        trail_p = config.TRAILING_PCT if getattr(config, "TRAILING_STOP_ENABLED", False) else 0
                        pos = OpenPosition(
                            symbol=sym, shares=shares, entry_price=entry_price_actual,
                            entry_bar_idx=entry_bar_idx, open_price=row["open_price"], rvol=rvol,
                            atr=atr, stop_pct=stop_p, signal_type=signal_type,
                            trail_activate_pct=trail_act, trail_pct=trail_p,
                        )
                        open_positions.append(pos)
                        entered_symbols.add(sym)
                        entry_count[sym] = entry_count.get(sym, 0) + 1
                        n_open += 1
                        entry_ts_str = _bar_ts_str(cached_bars[sym], entry_bar_idx)
                        print(f"  {sym} [{signal_type}] entry=${entry_price_actual:.4f}@{entry_ts_str} "
                              f"{shares}sh [RVOL={rvol:.1f}×]")

        # Force close remaining positions at EOD
        last_bar_idx = len(sorted_times) - 1 if sorted_times else 0
        for pos in open_positions:
            if not pos.closed:
                bars = cached_bars.get(pos.symbol, [])
                if bars:
                    last_bar = bars[-1]
                    pos.force_close(last_bar["close"], len(bars) - 1, "force_close")
                    closed_trades.append(pos)

        # Record results
        for pos in closed_trades:
            result = TradeResult(
                symbol=pos.symbol,
                date=str(date_key),
                entry_price=pos.entry_price,
                exit_price=pos.exit_price,
                shares=pos.shares,
                pnl=pos.pnl,
                pnl_pct=pos.pnl_pct,
                exit_reason=pos.exit_reason,
                open_price=pos.open_price,
                stop_price=pos.stop_price,
                target_price=0,
                trailing_high=pos.highest,
                exit_bar_idx=pos.exit_bar_idx,
                entry_bar_idx=pos.entry_bar_idx,
                position_size=round(pos.entry_price * pos.shares, 2),
                signal_type=pos.signal_type,
            )

            bars = cached_bars.get(pos.symbol, [])
            entry_ts = _bar_ts_str(bars, pos.entry_bar_idx)
            exit_ts = _bar_ts_str(bars, min(pos.exit_bar_idx, len(bars) - 1))

            print(f"  {pos.symbol} [{pos.signal_type}] entry=${pos.entry_price:.4f}@{entry_ts} "
                  f"exit=${pos.exit_price:.4f}@{exit_ts} ({pos.exit_reason}), "
                  f"P&L=${pos.pnl:+,.2f} ({pos.pnl_pct:+.2%}) "
                  f"[RVOL={pos.rvol:.1f}× highest=${pos.highest:.4f}]")

            all_trades.append(result)
            equity += result.pnl

        # Save chart data
        for pos in closed_trades:
            sym_key = f"{pos.symbol} ({date_key})"
            bars_1m_df = get_1min_bars(client, pos.symbol, date)
            if not bars_1m_df.empty:
                chart_bars = _bars_to_chart(bars_1m_df)
                bars = cached_bars.get(pos.symbol, [])
                entry_ts = _bar_ts_str(bars, pos.entry_bar_idx)
                exit_ts = _bar_ts_str(bars, min(pos.exit_bar_idx, len(bars) - 1))
                chart_entries[sym_key] = {
                    "date": str(date_key), "bars_1m": chart_bars,
                    "events": [
                        {"ts": entry_ts, "type": "buy", "price": pos.entry_price,
                         "label": f"BUY {pos.shares}sh [{pos.signal_type}]"},
                        {"ts": exit_ts, "type": "sell", "price": pos.exit_price,
                         "label": f"{pos.exit_reason.upper()} {pos.shares}sh"},
                    ],
                    "entry_price": pos.entry_price,
                    "stop_price": pos.stop_price, "target_price": 0,
                    "pnl": pos.pnl,
                    "open_price": pos.open_price, "signal": pos.signal_type,
                }

    print(f"\n{'=' * 70}")
    print(f"[momentum_surge] Backtest complete. Final equity: ${equity:,.2f}")
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
        from collections import Counter
        reasons = Counter(t.exit_reason for t in all_trades)
        print(f"Exit reasons: {dict(reasons)}")
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
