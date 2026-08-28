"""Backtesting engine — stonewang_daytrade_rtg_3.0: Multi-position Bar-by-bar RTG.

Key features:
  - Multi-position bar-by-bar simulation (up to MAX_POSITIONS concurrent)
  - RVOL-weighted position sizing (same as rtg_2.0)
  - Bar-based exits: green_to_red (1 red bar) / RVOL-adaptive stop / target
  - Daily profit protection (same as rtg_2.0)
  - RTG + Breakout entry signals (same as rtg_2.0)
  - Force close at end of day
"""

import json
import os
import re
import sys
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

# ── ETF filters ───────────────────────────────────────────────────
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


# ── Data helpers ───────────────────────────────────────────────────

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


# ── Entry signal detection (same as rtg_2.0) ─────

def find_rtg_entry(bars_list, open_price, start_idx=0, min_volume=None):
    if min_volume is None:
        min_volume = config.RTG_MIN_VOLUME
    entry_start = getattr(config, "ENTRY_WINDOW_START", "09:30")
    entry_end = getattr(config, "ENTRY_WINDOW_END", "15:55")
    min_gain_pct = getattr(config, "RTG_MIN_BAR_GAIN_PCT", 0.01)

    for i in range(max(start_idx, 1), len(bars_list)):
        bar = bars_list[i]
        prev = bars_list[i - 1]
        ts = bar["timestamp"]
        bar_time = ts.time()
        start_time = pd.Timestamp(f"{ts.date()} {entry_start}", tz="America/New_York").time()
        end_time = pd.Timestamp(f"{ts.date()} {entry_end}", tz="America/New_York").time()
        if not (start_time <= bar_time <= end_time):
            continue

        gain_pct = (bar["close"] / open_price) - 1.0 if open_price > 0 else 0
        if (gain_pct >= min_gain_pct
                and prev["volume"] > 0
                and bar["volume"] >= config.RTG_VOLUME_MULT * prev["volume"]
                and bar["volume"] >= min_volume):
            entry_price = round(bar["close"] * 1.001, 4)
            return entry_price, i, "rtg"

    return None


def find_breakout_entry(bars_list, open_price, start_idx=0, min_volume=None):
    if not getattr(config, "BREAKOUT_ENABLED", False):
        return None
    if min_volume is None:
        min_volume = config.RTG_MIN_VOLUME
    min_bars = getattr(config, "BREAKOUT_MIN_BARS", 5)
    vol_mult = getattr(config, "BREAKOUT_VOLUME_MULT", 1.5)
    entry_at_close = getattr(config, "BREAKOUT_ENTRY_AT_CLOSE", True)

    day_high = 0.0
    for i in range(max(start_idx, 1), len(bars_list)):
        bar = bars_list[i]
        prev = bars_list[i - 1]
        if i > 0:
            day_high = max(day_high, bars_list[i - 1]["high"])
        if i < min_bars:
            continue

        if (bar["close"] > day_high
                and prev["volume"] > 0
                and bar["volume"] >= vol_mult * prev["volume"]
                and bar["volume"] >= min_volume):
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


# ── Bar-based exit evaluation ─────────────────────────────────────

class BtResult:
    pass

def _get_rvol_exit_tier(rvol):
    """Get (stop_pct, target_pct) from RVOL_EXIT_TIERS based on RVOL."""
    tiers = getattr(config, "RVOL_EXIT_TIERS", [(0.0, 0.05, 0.30)])
    stop_pct = config.STOP_PCT
    target_pct = config.TARGET_PCT
    for rvol_min, s, t in tiers:
        if rvol >= rvol_min:
            stop_pct = s
            target_pct = t
    return stop_pct, target_pct


def evaluate_bar_exit(entry_price, shares, bars_after_entry, symbol="",
                      open_price=0.0, force_close_price=None, entry_bar_idx=0,
                      signal_type="", rvol=0.0):
    """Exit: green_to_red (1 red bar) / RVOL-adaptive stop / target / force_close."""
    if not bars_after_entry or entry_price <= 0 or shares <= 0:
        r = BtResult()
        r.symbol = symbol; r.entry_price = entry_price; r.exit_price = entry_price
        r.shares = shares; r.pnl = 0.0; r.pnl_pct = 0.0; r.exit_reason = "no_bars"
        r.open_price = open_price; r.stop_price = 0.0; r.target_price = 0.0
        r.trailing_high = 0.0; r.exit_bar_idx = -1; r.entry_bar_idx = entry_bar_idx
        r.signal_type = signal_type; r.date = ""
        return r

    # RVOL-adaptive stop/target
    stop_pct, target_pct = _get_rvol_exit_tier(rvol)
    stop_price = round(entry_price * (1 - stop_pct), 4)
    target_price = round(entry_price * (1 + target_pct), 4)
    slippage = getattr(config, "SLIPPAGE_EXIT_PCT", 0.0)
    highest = entry_price
    is_first_bar = True
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

        # Green-to-red: any red bar after the first bar → sell
        if not is_first_bar and is_red and getattr(config, "EXIT_ON_GREEN_TO_RED", True):
            g2r_consec = getattr(config, "GREEN_TO_RED_CONSEC_BARS", 1)
            # For 1-bar g2r, exit immediately on any red bar
            if g2r_consec <= 1:
                exit_price = bar_close; reason = "green_to_red"; exit_bi = bi; exit_triggered = True
            else:
                # Multi-bar g2r (if configured)
                red_count = 1
                for k in range(bi - 1, -1, -1):
                    if bars_after_entry[k]["close"] < bars_after_entry[k]["open"]:
                        red_count += 1
                    else:
                        break
                if red_count >= g2r_consec:
                    exit_price = bar_close; reason = "green_to_red"; exit_bi = bi; exit_triggered = True

        # RVOL-adaptive stop loss
        if not exit_triggered and bar_low <= stop_price:
            exit_price = stop_price; reason = "stop_loss"; exit_bi = bi; exit_triggered = True

        # Target
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

    if slippage > 0 and reason not in ("stop_loss", "green_to_red", "target"):
        exit_price = round(exit_price * (1 - slippage), 4)

    pnl = round((exit_price - entry_price) * shares, 2)
    pnl_pct = round(pnl / (entry_price * shares), 4) if entry_price > 0 else 0.0

    r = BtResult()
    r.symbol = symbol; r.date = ""; r.entry_price = round(entry_price, 4)
    r.exit_price = round(exit_price, 4); r.shares = shares; r.pnl = pnl
    r.pnl_pct = pnl_pct; r.exit_reason = reason
    r.open_price = round(open_price, 4) if open_price else 0.0
    r.stop_price = stop_price; r.target_price = target_price
    r.trailing_high = round(highest, 4); r.exit_bar_idx = exit_bi
    r.position_size = round(entry_price * shares, 2)
    r.entry_bar_idx = entry_bar_idx; r.signal_type = signal_type
    return r


# ── Event-driven simulation ───────────────────────────────────────

def save_backtest_charts(chart_entries, filepath="versions/chart_data_rtg3.json"):
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

    max_pos = getattr(config, "MAX_POSITIONS", 8)
    max_entry_att = getattr(config, "MAX_ENTRY_ATTEMPTS", 8)
    print(f"[rtg_3.0] Backtesting {len(trading_days)} trading days: "
          f"{trading_days[0].date()} to {trading_days[-1].date()}")
    print(f"Capital: ${config.INITIAL_CAPITAL:,.2f} | RVOL-weighted sizing | "
          f"Max {max_pos} concurrent positions")
    print(f"Exit: green_to_red (1 bar) / RVOL-adaptive stop / target / force_close")
    print(f"Min RVOL: {config.MIN_RVOL_TO_TRADE:.1f}x | Max entry attempts: {max_entry_att}")
    print(f"Scan interval: {getattr(config, 'SCAN_INTERVAL_SEC', 30)}s")

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

    all_trades = []
    equity = config.INITIAL_CAPITAL

    for date in trading_days:
        date_key = date.date()
        if date_key not in gap_data or gap_data[date_key].empty:
            continue

        candidates = gap_data[date_key]
        max_cands = getattr(config, "MAX_CANDIDATES", 40)
        candidates = candidates.head(max_cands)
        min_rvol = getattr(config, "MIN_RVOL_TO_TRADE", 3.0)
        qualified = candidates[candidates["rvol"] >= min_rvol]

        print(f"\n--- {date_key} ({len(candidates)} candidates, {len(qualified)} qualified by RVOL>={min_rvol:.1f}x, equity: ${equity:,.2f}) ---")
        for _, row in candidates.iterrows():
            sym = row["symbol"]; rvol = row.get("rvol", 0); gap_pct = row["gap_pct"]; open_p = row["open_price"]
            marker = " *" if rvol >= min_rvol else ""
            print(f"  {sym} gap={gap_pct:+.1%} RVOL={rvol:.1f}x open=${open_p:.2f}{marker}")

        if qualified.empty:
            print("  No qualified candidates, skipping day")
            continue

        cached_bars = {}
        for _, row in qualified.iterrows():
            symbol = row["symbol"]
            bars_1m = get_1min_bars(client, symbol, date)
            if bars_1m.empty or len(bars_1m) < 2:
                cached_bars[symbol] = None
                continue
            cached_bars[symbol] = _bars_to_list(bars_1m)

        # ── Multi-position bar-by-bar simulation ──
        force_close_str = getattr(config, "FORCE_CLOSE_TIME", "15:59")
        force_close_min_parts = force_close_str.split(":")
        force_close_min = int(force_close_min_parts[0]) * 60 + int(force_close_min_parts[1])
        entry_end_str = getattr(config, "ENTRY_WINDOW_END", "15:55")
        entry_end_parts = entry_end_str.split(":")
        entry_end_min = int(entry_end_parts[0]) * 60 + int(entry_end_parts[1])
        daily_loss = 0.0
        max_daily_loss = equity * config.MAX_DAILY_LOSS_PCT
        max_positions = getattr(config, "MAX_POSITIONS", 8)
        max_entry_attempts = getattr(config, "MAX_ENTRY_ATTEMPTS", 8)
        scan_interval_min = getattr(config, "SCAN_INTERVAL_SEC", 30) / 60.0
        max_daily_trades = getattr(config, "MAX_DAILY_TRADES", 0)
        daily_trades_count = 0
        max_daily_profit = 0.0
        profit_protect = getattr(config, "DAILY_PROFIT_PROTECT_ENABLED", False)
        profit_ratio = getattr(config, "DAILY_PROFIT_PROTECT_RATIO", 0.85)
        profit_min = getattr(config, "DAILY_PROFIT_PROTECT_MIN", 5.0)

        # Build unified timeline of all 1-min bar timestamps
        all_timestamps = set()
        for sym, bars in cached_bars.items():
            if bars:
                for bar in bars:
                    ts = bar["timestamp"]
                    bar_min = ts.hour * 60 + ts.minute
                    if bar_min <= force_close_min:
                        all_timestamps.add(ts)
        if not all_timestamps:
            continue
        sorted_ts = sorted(all_timestamps)

        # Index bars by (symbol, timestamp) for O(1) lookup
        bar_index = {}  # symbol -> {timestamp -> bar}
        for sym, bars in cached_bars.items():
            if bars:
                bar_index[sym] = {bar["timestamp"]: bar for bar in bars}

        # Candidate info for quick lookup
        cand_info = {}  # symbol -> {open_price, rvol, gap_pct}
        for _, row in qualified.iterrows():
            cand_info[row["symbol"]] = {
                "open_price": row["open_price"],
                "rvol": row.get("rvol", 0),
                "gap_pct": row["gap_pct"],
            }

        # Open positions: list of dicts
        # Each: {symbol, entry_price, shares, open_price, rvol, signal_type,
        #        entry_bar_idx, stop_price, target_price, highest,
        #        is_first_bar (for g2r logic), exited}
        open_positions = []
        # Track which symbols already have a position
        symbols_in_pos = set()
        # Per-symbol: next bar index to search for entry
        next_search_idx = {sym: 0 for sym in cached_bars}
        # Last exit minute (global cooldown)
        last_exit_bar_min = -999

        for ts_idx, ts in enumerate(sorted_ts):
            bar_min = ts.hour * 60 + ts.minute

            # ── 1. Check exits for all open positions ──
            positions_to_close = []
            for pos in open_positions:
                if pos.get("exited"):
                    continue
                sym = pos["symbol"]
                sym_bars = bar_index.get(sym, {})
                bar = sym_bars.get(ts)
                if bar is None:
                    continue

                bar_high = float(bar["high"])
                bar_low = float(bar["low"])
                bar_close = float(bar["close"])
                bar_open = float(bar["open"])
                if bar_high > pos["highest"]:
                    pos["highest"] = bar_high

                exit_triggered = False
                exit_price = 0.0
                reason = ""

                # Green-to-red exit (skip first bar after entry)
                if not pos["is_first_bar"]:
                    is_red = bar_close < bar_open
                    if is_red:
                        pos["consec_red"] = pos.get("consec_red", 0) + 1
                    else:
                        pos["consec_red"] = 0
                    g2r_consec = getattr(config, "GREEN_TO_RED_CONSEC_BARS", 1)
                    if pos["consec_red"] >= g2r_consec and getattr(config, "EXIT_ON_GREEN_TO_RED", True):
                        exit_price = bar_close; reason = "green_to_red"; exit_triggered = True

                # Trailing stop (activated after price moves +1% from entry)
                trail_act_pct = getattr(config, "TRAIL_ACTIVATE_PCT", 0.01)
                trail_pct = getattr(config, "TRAIL_PCT", 0.015)
                if not exit_triggered and trail_pct > 0:
                    gain_from_entry = (pos["highest"] / pos["entry_price"]) - 1.0
                    if gain_from_entry >= trail_act_pct:
                        trail_price = round(pos["highest"] * (1 - trail_pct), 4)
                        # Update max trail price seen
                        if trail_price > pos.get("trail_stop_price", 0):
                            pos["trail_stop_price"] = trail_price
                        if bar_low <= pos["trail_stop_price"]:
                            exit_price = pos["trail_stop_price"]; reason = "trail_stop"; exit_triggered = True

                # RVOL-adaptive stop loss
                if not exit_triggered and bar_low <= pos["stop_price"]:
                    exit_price = pos["stop_price"]; reason = "stop_loss"; exit_triggered = True

                # Target
                if not exit_triggered and bar_high >= pos["target_price"]:
                    exit_price = pos["target_price"]; reason = "target"; exit_triggered = True

                pos["is_first_bar"] = False

                # Force close at end of day
                if not exit_triggered and bar_min >= force_close_min:
                    exit_price = bar_close; reason = "force_close"; exit_triggered = True

                if exit_triggered:
                    slippage = getattr(config, "SLIPPAGE_EXIT_PCT", 0.0)
                    if slippage > 0 and reason not in ("stop_loss", "green_to_red", "target"):
                        exit_price = round(exit_price * (1 - slippage), 4)

                    pnl = round((exit_price - pos["entry_price"]) * pos["shares"], 2)
                    pnl_pct = round(pnl / (pos["entry_price"] * pos["shares"]), 4)

                    r = BtResult()
                    r.symbol = sym; r.date = str(date_key)
                    r.entry_price = pos["entry_price"]; r.exit_price = round(exit_price, 4)
                    r.shares = pos["shares"]; r.pnl = pnl; r.pnl_pct = pnl_pct
                    r.exit_reason = reason; r.open_price = pos["open_price"]
                    r.stop_price = pos["stop_price"]; r.target_price = pos["target_price"]
                    r.trailing_high = round(pos["highest"], 4)
                    r.entry_bar_idx = pos["entry_bar_idx"]; r.signal_type = pos["signal_type"]
                    r.position_size = round(pos["entry_price"] * pos["shares"], 2)

                    all_trades.append(r)
                    equity += pnl
                    daily_loss += pnl
                    if daily_loss > max_daily_profit:
                        max_daily_profit = daily_loss
                    daily_trades_count += 1
                    pos["exited"] = True
                    symbols_in_pos.discard(sym)
                    last_exit_bar_min = bar_min

                    # Update next_search_idx for this symbol
                    all_bars_sym = cached_bars.get(sym, [])
                    for k in range(pos["entry_bar_idx"] + 1, len(all_bars_sym)):
                        if all_bars_sym[k]["timestamp"] >= ts:
                            next_search_idx[sym] = k + 1
                            break

                    exit_ts_str = ts.strftime("%H:%M")
                    entry_ts_str = _bar_ts_str(all_bars_sym, pos["entry_bar_idx"])
                    print(f"  {sym} [{pos['signal_type']}] entry=${pos['entry_price']:.4f}@{entry_ts_str} "
                          f"exit=${r.exit_price:.4f}@{exit_ts_str} ({reason}), "
                          f"P&L=${pnl:+,.2f} ({pnl_pct:+.2%}) [RVOL={pos['rvol']:.1f}x]")

            # Remove exited positions
            open_positions = [p for p in open_positions if not p.get("exited")]

            # ── 2. Check daily loss / profit protection / trade limit ──
            if daily_loss <= -max_daily_loss:
                break
            if max_daily_trades > 0 and daily_trades_count >= max_daily_trades:
                break
            if profit_protect and max_daily_profit >= profit_min:
                if daily_loss <= -(max_daily_profit * (1 - profit_ratio)):
                    print(f"  [Profit Protect] daily P&L ${daily_loss:+,.2f} dropped below "
                          f"{profit_ratio:.0%} of max ${max_daily_profit:+,.2f}, stopping")
                    # Force close remaining positions
                    for pos in open_positions:
                        sym = pos["symbol"]
                        sym_bars = bar_index.get(sym, {})
                        bar = sym_bars.get(ts)
                        close_p = float(bar["close"]) if bar else pos["entry_price"]
                        pnl = round((close_p - pos["entry_price"]) * pos["shares"], 2)
                        pnl_pct = round(pnl / (pos["entry_price"] * pos["shares"]), 4)
                        r = BtResult()
                        r.symbol = sym; r.date = str(date_key)
                        r.entry_price = pos["entry_price"]; r.exit_price = round(close_p, 4)
                        r.shares = pos["shares"]; r.pnl = pnl; r.pnl_pct = pnl_pct
                        r.exit_reason = "profit_protect"; r.open_price = pos["open_price"]
                        r.stop_price = pos["stop_price"]; r.target_price = pos["target_price"]
                        r.trailing_high = round(pos["highest"], 4)
                        r.entry_bar_idx = pos["entry_bar_idx"]; r.signal_type = pos["signal_type"]
                        r.position_size = round(pos["entry_price"] * pos["shares"], 2)
                        all_trades.append(r)
                        equity += pnl; daily_loss += pnl
                        daily_trades_count += 1
                        print(f"  {sym} [profit_protect] close@${close_p:.4f}, P&L=${pnl:+,.2f}")
                    open_positions = []
                    break

            # ── 3. Check entries (if we have room) ──
            if bar_min > entry_end_min:
                continue  # Past entry window, only monitor exits
            if bar_min < last_exit_bar_min + scan_interval_min:
                continue  # Still in cooldown after last exit
            if len(open_positions) >= max_positions:
                continue  # No room for more positions

            attempts = max_entry_attempts
            for _, row in qualified.iterrows():
                if attempts <= 0 or len(open_positions) >= max_positions:
                    break

                symbol = row["symbol"]
                if symbol in symbols_in_pos:
                    continue  # Already have position in this symbol
                open_price = row["open_price"]
                rvol = row.get("rvol", 0)
                all_bars = cached_bars.get(symbol)
                if all_bars is None:
                    continue

                # Find entry signal from current position
                search_from = next_search_idx.get(symbol, 0)
                result = find_entry_signal(all_bars, open_price, start_idx=search_from)
                if result is None:
                    attempts -= 1
                    continue

                entry_price_signal, entry_bar_idx, signal_type = result

                # Entry must be at or before current timestamp
                entry_bar_ts = all_bars[entry_bar_idx]["timestamp"]
                entry_bar_min_val = entry_bar_ts.hour * 60 + entry_bar_ts.minute
                if entry_bar_ts > ts:
                    continue  # Signal is in the future, skip for now
                if entry_bar_min_val >= force_close_min - 1:
                    attempts -= 1
                    continue

                # Entry price with slippage
                entry_bar_close = all_bars[entry_bar_idx]["close"]
                entry_slippage = getattr(config, "SLIPPAGE_ENTRY_PCT", 0.005)
                entry_price_actual = round(entry_bar_close * (1 + entry_slippage), 4)

                # RVOL-weighted position sizing
                rvol_tiers = getattr(config, "RVOL_SIZING_TIERS", [(0.0, 0.20)])
                equity_ratio = 0.20
                for rvol_min, eq_pct in rvol_tiers:
                    if rvol >= rvol_min:
                        equity_ratio = eq_pct
                        break
                pos_size = equity * equity_ratio
                shares = int(pos_size / entry_price_actual)
                min_pos = getattr(config, "MIN_POSITION_SIZE", 40)
                max_pos = getattr(config, "MAX_POSITION_SIZE", 9999)
                if shares * entry_price_actual < min_pos:
                    attempts -= 1
                    continue
                if shares * entry_price_actual > max_pos:
                    shares = int(max_pos / entry_price_actual)
                if shares <= 0:
                    attempts -= 1
                    continue

                # RVOL-adaptive stop/target
                stop_pct, target_pct = _get_rvol_exit_tier(rvol)
                stop_price = round(entry_price_actual * (1 - stop_pct), 4)
                target_price = round(entry_price_actual * (1 + target_pct), 4)

                open_positions.append({
                    "symbol": symbol, "entry_price": entry_price_actual,
                    "shares": shares, "open_price": open_price,
                    "rvol": rvol, "signal_type": signal_type,
                    "entry_bar_idx": entry_bar_idx,
                    "stop_price": stop_price, "target_price": target_price,
                    "highest": entry_price_actual,
                    "trail_stop_price": 0.0,
                    "is_first_bar": True, "exited": False,
                })
                symbols_in_pos.add(symbol)
                next_search_idx[symbol] = entry_bar_idx + 1

                entry_ts_str = _bar_ts_str(all_bars, entry_bar_idx)
                print(f"  + {symbol} [{signal_type}] entry=${entry_price_actual:.4f}@{entry_ts_str} "
                      f"shares={shares} stop={stop_pct:.0%} target={target_pct:.0%} "
                      f"[RVOL={rvol:.1f}x, {equity_ratio:.0%} equity]")
                attempts -= 1

        # ── Force close any remaining open positions at end of day ──
        for pos in open_positions:
            sym = pos["symbol"]
            all_bars_sym = cached_bars.get(sym, [])
            if all_bars_sym:
                last_bar = all_bars_sym[-1]
                close_p = float(last_bar["close"])
            else:
                close_p = pos["entry_price"]
            slippage = getattr(config, "SLIPPAGE_FORCE_CLOSE_PCT", 0.01)
            close_p = round(close_p * (1 - slippage), 4)
            pnl = round((close_p - pos["entry_price"]) * pos["shares"], 2)
            pnl_pct = round(pnl / (pos["entry_price"] * pos["shares"]), 4)
            r = BtResult()
            r.symbol = sym; r.date = str(date_key)
            r.entry_price = pos["entry_price"]; r.exit_price = close_p
            r.shares = pos["shares"]; r.pnl = pnl; r.pnl_pct = pnl_pct
            r.exit_reason = "force_close_eod"; r.open_price = pos["open_price"]
            r.stop_price = pos["stop_price"]; r.target_price = pos["target_price"]
            r.trailing_high = round(pos["highest"], 4)
            r.entry_bar_idx = pos["entry_bar_idx"]; r.signal_type = pos["signal_type"]
            r.position_size = round(pos["entry_price"] * pos["shares"], 2)
            all_trades.append(r)
            equity += pnl; daily_loss += pnl
            daily_trades_count += 1
            print(f"  {sym} [force_close_eod] close@${close_p:.4f}, P&L=${pnl:+,.2f}")
        open_positions = []

    print(f"\n{'=' * 70}")
    print(f"[rtg_3.0] Backtest complete. Final equity: ${equity:,.2f}")
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

    return all_trades


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=config.BACKTEST_DAYS)
    parser.add_argument("--end", type=str, default=None)
    args = parser.parse_args()
    end = pd.Timestamp(args.end, tz="America/New_York") if args.end else None
    run_backtest(end_date=end, n_days=args.days)
