"""Backtesting engine — stonewang_daytrade_rtg_3.0: Event-driven All-in RTG with Bar-based Exits.

Key differences from rtg_1.0 backtest:
  - Event-driven: scan → buy → monitor (bar-based exit) → sell → immediately scan again
  - Single position at a time (MAX_POSITIONS=1)
  - All-in sizing (95% of equity)
  - Bar-based exits: red_bar_exit / green_to_red / three_green_bars / 3% hard stop
  - Entry restriction: skip if >=2 consecutive green bars before entry
  - RTG + Breakout entry signals
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


# ── Entry signal detection (with consecutive green bar check) ─────

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

        # B: 2.5x volume multiplier (from config)
        # D: bar must gain >=1% vs open_price
        if (bar["close"] > open_price * (1 + min_gain_pct)
                and prev["volume"] > 0
                and bar["volume"] >= config.RTG_VOLUME_MULT * prev["volume"]
                and bar["volume"] >= min_volume):
            # C: Require previous bar also confirms (close > open_price with volume)
            if confirm_bars >= 2 and i >= 2:
                prev2 = bars_list[i - 2]
                if not (prev["close"] > open_price
                        and prev2["volume"] > 0
                        and prev["volume"] >= config.RTG_VOLUME_MULT * prev2["volume"]
                        and prev["volume"] >= min_volume):
                    continue
            # Check consecutive green bars before entry (don't chase)
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
            # C: Require previous bar also confirms
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


# ── Bar-based exit evaluation ─────────────────────────────────────

class BtResult:
    pass

def evaluate_bar_exit(entry_price, shares, bars_after_entry, symbol="",
                      open_price=0.0, force_close_price=None, entry_bar_idx=0,
                      signal_type=""):
    """Bar-based exit: red_bar_exit / green_to_red / three_green_bars / stop / target / force_close."""
    if not bars_after_entry or entry_price <= 0 or shares <= 0:
        r = BtResult()
        r.symbol = symbol; r.entry_price = entry_price; r.exit_price = entry_price
        r.shares = shares; r.pnl = 0.0; r.pnl_pct = 0.0; r.exit_reason = "no_bars"
        r.open_price = open_price; r.stop_price = 0.0; r.target_price = 0.0
        r.trailing_high = 0.0; r.exit_bar_idx = -1; r.entry_bar_idx = entry_bar_idx
        r.signal_type = signal_type; r.date = ""
        return r

    stop_price = round(entry_price * (1 - config.STOP_PCT), 4)
    target_price = round(entry_price * (1 + config.TARGET_PCT), 4)
    slippage = getattr(config, "SLIPPAGE_EXIT_PCT", 0.0)
    highest = entry_price
    is_first_bar = True
    green_bar_count = 0
    red_bar_count = 0  # E: consecutive red bar counter for green_to_red
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
        # E: green_to_red requires 2 consecutive red bars
        if not exit_triggered and not is_first_bar and is_red and getattr(config, "EXIT_ON_GREEN_TO_RED", True):
            red_bar_count += 1
            if red_bar_count >= g2r_consec:
                exit_price = bar_close; reason = "green_to_red"; exit_bi = bi; exit_triggered = True
        elif not exit_triggered and not is_first_bar and is_green:
            red_bar_count = 0  # Reset on green bar
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

    max_green = getattr(config, "MAX_GREEN_BARS_TO_ENTER", 2)
    print(f"[rtg_3.0] Backtesting {len(trading_days)} trading days: "
          f"{trading_days[0].date()} to {trading_days[-1].date()}")
    print(f"Capital: ${config.INITIAL_CAPITAL:,.2f} | ALL-IN ({config.ALL_IN_BP_RATIO:.0%} BP) | "
          f"Event-driven | Max 1 position")
    print(f"Entry: RTG + Breakout | Max {max_green} consecutive green bars to enter")
    print(f"Exit: red_bar / green_to_red / 3_green_bars / {config.STOP_PCT:.0%} stop / {config.TARGET_PCT:.0%} target")
    print(f"Min RVOL: {config.MIN_RVOL_TO_TRADE:.1f}x | Max entry attempts: {config.MAX_ENTRY_ATTEMPTS}")

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

        # ── Event-driven simulation ──
        force_close_str = getattr(config, "FORCE_CLOSE_TIME", "15:59")
        force_close_min_parts = force_close_str.split(":")
        force_close_min = int(force_close_min_parts[0]) * 60 + int(force_close_min_parts[1])
        daily_loss = 0.0
        max_daily_loss = equity * config.MAX_DAILY_LOSS_PCT
        # Track earliest bar index we can search from (per symbol)
        # After a trade exits, next entry must be after SCAN_INTERVAL_SEC
        next_search_idx = {row["symbol"]: 0 for _, row in qualified.iterrows()}
        # Global: minimum bar timestamp for next entry (simulates scan wait)
        last_exit_bar_min = 0  # in minutes since midnight
        scan_interval_min = getattr(config, "SCAN_INTERVAL_SEC", 300) / 60.0
        scan_idx = 0
        daily_trades_count = 0
        max_daily_trades = getattr(config, "MAX_DAILY_TRADES", 0)

        while True:
            scan_idx += 1
            if daily_loss <= -max_daily_loss:
                break
            if max_daily_trades > 0 and daily_trades_count >= max_daily_trades:
                break

            # Try top candidates (by RVOL)
            max_attempts = getattr(config, "MAX_ENTRY_ATTEMPTS", 3)
            entered = False

            for _, row in qualified.iterrows():
                if entered or max_attempts <= 0:
                    break

                symbol = row["symbol"]
                open_price = row["open_price"]
                rvol = row.get("rvol", 0)
                all_bars = cached_bars.get(symbol)
                if all_bars is None:
                    max_attempts -= 1
                    continue

                # Find entry signal starting after previous exit for this symbol
                search_from = next_search_idx.get(symbol, 0)
                result = find_entry_signal(all_bars, open_price, start_idx=search_from)
                if result is None:
                    max_attempts -= 1
                    continue

                entry_price_signal, entry_bar_idx, signal_type = result

                # Check entry bar is after scan cooldown from last exit
                entry_bar_ts = all_bars[entry_bar_idx]["timestamp"]
                entry_bar_min_val = entry_bar_ts.hour * 60 + entry_bar_ts.minute
                if entry_bar_min_val < last_exit_bar_min + scan_interval_min:
                    max_attempts -= 1
                    continue

                # Check entry is before force close
                if entry_bar_min_val >= force_close_min - 1:
                    max_attempts -= 1
                    continue

                # Entry price: use the signal bar's close (not open_price*1.001)
                # In live, we buy at market when signal triggers
                entry_bar_close = all_bars[entry_bar_idx]["close"]
                entry_slippage = getattr(config, "SLIPPAGE_ENTRY_PCT", 0.005)
                entry_price_actual = round(entry_bar_close * (1 + entry_slippage), 4)

                # All-in sizing
                all_in_ratio = getattr(config, "ALL_IN_BP_RATIO", 0.95)
                pos_size = equity * all_in_ratio
                shares = int(pos_size / entry_price_actual)
                if shares <= 0:
                    max_attempts -= 1
                    continue

                # Bars after entry, trimmed to force_close
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

                # Evaluate with bar-based exit
                trade_result = evaluate_bar_exit(
                    entry_price=entry_price_actual, shares=shares,
                    bars_after_entry=cycle_bars, symbol=symbol,
                    open_price=open_price, force_close_price=force_close_price,
                    entry_bar_idx=entry_bar_idx, signal_type=signal_type,
                )
                trade_result.date = str(date_key)
                trade_result.open_price = open_price

                # Update next search index and exit time
                exit_bar_abs = entry_bar_idx + 1 + trade_result.exit_bar_idx
                next_search_idx[symbol] = exit_bar_abs + 1
                # Set cooldown: next entry must be after exit + scan interval
                if exit_bar_abs < len(all_bars):
                    exit_ts = all_bars[exit_bar_abs]["timestamp"]
                    last_exit_bar_min = exit_ts.hour * 60 + exit_ts.minute
                else:
                    last_exit_bar_min = force_close_min

                entry_ts = _bar_ts_str(all_bars, entry_bar_idx)
                exit_bar = min(exit_bar_abs, len(all_bars) - 1)
                exit_ts_str = _bar_ts_str(all_bars, exit_bar)

                print(f"  {symbol} [{signal_type}] entry=${entry_price_actual:.4f}@{entry_ts} "
                      f"exit=${trade_result.exit_price:.4f}@{exit_ts_str} ({trade_result.exit_reason}), "
                      f"P&L=${trade_result.pnl:+,.2f} ({trade_result.pnl_pct:+.2%}) "
                      f"[RVOL={rvol:.1f}x]")

                all_trades.append(trade_result)
                equity += trade_result.pnl
                daily_loss += trade_result.pnl
                daily_trades_count += 1
                entered = True
                break  # One position at a time

            if not entered:
                break  # No more entries possible

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
