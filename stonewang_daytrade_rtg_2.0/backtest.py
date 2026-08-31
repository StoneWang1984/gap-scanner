"""Backtesting engine — stonewang_daytrade_rtg_2.0: RTG + Profit Protection + Progressive Trailing.

Key differences from rtg_1.0 backtest:
  1. Daily profit protection: when today's profit drops to X% of max, force close all
  2. Progressive trailing stop: trail_pct tightens as profit grows
  3. Bar-by-bar concurrent simulation (all positions evaluated each bar,
     so daily profit protection can trigger across all open positions)

Entry detection (1-min bars):
  Signal A (Red-to-Green):
    - bar[i].close > open_price (crossed back above open)
    - bar[i].volume >= RTG_VOLUME_MULT × bar[i-1].volume (volume spike)
    - bar[i].volume >= RTG_MIN_VOLUME (liquidity floor)
  Signal B (Gap-and-Go): DISABLED
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

                    # Calculate 14-day ATR from pre-gap daily bars
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
                        "volume": volume, "dollar_volume": dollar_volume,
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
    """Find RTG or Gap-and-Go entry on 1-min bars."""
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

        bar_high = float(bar["high"])
        bar_close = float(bar["close"])
        bar_vol = int(bar["volume"])
        prev_vol = int(prev_bar["volume"])
        prev_high = float(prev_bar["high"])
        prev_open = float(prev_bar["open"])
        prev_close = float(prev_bar["close"])

        if (bar_close > open_price
                and prev_vol > 0
                and bar_vol >= config.RTG_VOLUME_MULT * prev_vol
                and bar_vol >= min_volume):
            entry_at_open = round(open_price * 1.001, 4)
            entry_at_close = round(bar_close * 1.001, 4)
            return entry_at_open, entry_at_close, i, True, "rtg"

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
    _, pct = _get_rvol_tier(rvol)
    split_pct = pct / max(same_tier_count, 1)
    return round(equity * split_pct, 2)


def get_rvol_exit_params(rvol):
    tiers = getattr(config, "RVOL_EXIT_TIERS", [
        (10.0, 0.07, 0.30, 0.05, 0.03),
        (5.0,  0.05, 0.20, 0.05, 0.03),
        (0.0,  0.03, 0.10, 0.04, 0.02),
    ])
    for rvol_min, stop, target, trail_act, trail in tiers:
        if rvol >= rvol_min:
            return stop, target, trail_act, trail
    return 0.05, 0.20, 0.05, 0.03


def get_atr_stop_params(rvol, atr, entry_price):
    """Get stop/target/trail based on ATR and RVOL tier."""
    atr_mult = 2.0
    for rvol_min, mult in getattr(config, "ATR_MULT_TIERS", [(10.0, 3.0), (5.0, 2.5), (0.0, 2.0)]):
        if rvol >= rvol_min:
            atr_mult = mult
            break
    stop_pct = (atr_mult * atr) / entry_price
    stop_pct = max(getattr(config, "ATR_STOP_MIN_PCT", 0.02),
                   min(getattr(config, "ATR_STOP_MAX_PCT", 0.10), stop_pct))
    trail_pct = max(0.005, min(0.05, (getattr(config, "ATR_TRAIL_MULT", 1.5) * atr) / entry_price))
    target_pct = max(0.05, min(0.50, (getattr(config, "ATR_TARGET_MULT", 5.0) * atr) / entry_price))
    trail_activate_pct = min(stop_pct * 1.5, 0.10)
    return stop_pct, target_pct, trail_activate_pct, trail_pct


# ── Open position tracker for concurrent bar-by-bar simulation ────

class OpenPosition:
    """Tracks a single open position during bar-by-bar simulation."""
    def __init__(self, symbol, shares, entry_price, entry_bar_idx, open_price,
                 rvol, atr, stop_pct, target_pct, trail_activate_pct, trail_pct, signal_type):
        self.symbol = symbol
        self.shares = shares
        self.entry_price = entry_price
        self.entry_bar_idx = entry_bar_idx
        self.open_price = open_price
        self.rvol = rvol
        self.atr = atr
        self.stop_pct = stop_pct
        self.target_pct = target_pct
        self.trail_activate_pct = trail_activate_pct
        self.base_trail_pct = trail_pct
        self.signal_type = signal_type

        self.stop_price = round(entry_price * (1 - stop_pct), 4)
        self.target_price = round(entry_price * (1 + target_pct), 4)
        self.trail_activate = entry_price * (1 + trail_activate_pct)

        self.highest = entry_price
        self.trail_active = False
        self.trail_stop = 0.0
        self.current_trail_pct = trail_pct
        self.closed = False
        self.exit_price = 0.0
        self.exit_reason = ""
        self.exit_bar_idx = -1

    def _get_progressive_trail(self, profit_pct):
        tiers = getattr(config, "PROGRESSIVE_TRAIL_TIERS", [])
        if not tiers:
            return self.base_trail_pct
        for threshold, trail in tiers:
            if profit_pct >= threshold:
                return trail
        return self.base_trail_pct

    def evaluate_bar(self, bar, bar_idx):
        """Evaluate a single bar. Returns True if position exited this bar."""
        if self.closed:
            return False

        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bar_close = float(bar["close"])

        if bar_high > self.highest:
            self.highest = bar_high

        # Progressive trailing: adjust trail_pct based on current profit
        if self.highest > self.entry_price:
            profit_pct = (self.highest - self.entry_price) / self.entry_price
            self.current_trail_pct = self._get_progressive_trail(profit_pct)

        # 1. Hard stop
        if bar_low <= self.stop_price:
            self.exit_price = self.stop_price
            self.exit_reason = "stop_loss"
            self.exit_bar_idx = bar_idx
            self.closed = True
            return True

        # 2. Trailing stop (with progressive tightening)
        if not self.trail_active and self.highest >= self.trail_activate:
            self.trail_active = True
            self.trail_stop = round(self.highest * (1 - self.current_trail_pct), 4)
        if self.trail_active:
            new_trail = round(self.highest * (1 - self.current_trail_pct), 4)
            if new_trail > self.trail_stop:
                self.trail_stop = new_trail
            if bar_low <= self.trail_stop:
                self.exit_price = self.trail_stop
                self.exit_reason = "trail_stop"
                self.exit_bar_idx = bar_idx
                self.closed = True
                return True

        # 3. Target
        if bar_high >= self.target_price:
            self.exit_price = self.target_price
            self.exit_reason = "target"
            self.exit_bar_idx = bar_idx
            self.closed = True
            return True

        return False

    def force_close(self, price, bar_idx, reason="force_close"):
        """Force close at given price (profit protect or EOD)."""
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
        return round((self.exit_price - self.entry_price) * self.shares, 2)

    @property
    def pnl_pct(self):
        if not self.closed or self.entry_price <= 0:
            return 0.0
        return round(self.pnl / (self.entry_price * self.shares), 4)


def save_backtest_charts(chart_entries, filepath="versions/chart_data_rtg2.json"):
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

    profit_protect = getattr(config, "DAILY_PROFIT_PROTECT_ENABLED", False)
    profit_ratio = getattr(config, "DAILY_PROFIT_PROTECT_RATIO", 0.85)
    profit_min = getattr(config, "DAILY_PROFIT_PROTECT_MIN", 5.0)
    progressive_tiers = getattr(config, "PROGRESSIVE_TRAIL_TIERS", [])

    print(f"[rtg_2.0] Backtesting {len(trading_days)} trading days: "
          f"{trading_days[0].date()} to {trading_days[-1].date()}")
    print(f"Capital: ${config.INITIAL_CAPITAL:,.2f} | RVOL-weighted sizing | "
          f"Max concurrent: {config.MAX_POSITIONS} | Max daily trades: {config.MAX_DAILY_TRADES}")
    print(f"Entry window: {config.ENTRY_WINDOW_START}-{config.ENTRY_WINDOW_END} EST")
    print(f"  Signal A (RTG): close > open AND vol >= {config.RTG_VOLUME_MULT}× prior AND vol >= {config.RTG_MIN_VOLUME:,}")
    print(f"  Signal B (GapGo): DISABLED")
    sizing_tiers = getattr(config, "RVOL_SIZING_TIERS", [])
    if sizing_tiers:
        print(f"  Sizing tiers: " + ", ".join(f"RVOL>{r:.0f}×→{p:.0%}" for r, p in sizing_tiers))
    atr_mult_tiers = getattr(config, "ATR_MULT_TIERS", [])
    if atr_mult_tiers:
        print(f"  Stop: ATR-based (" + ", ".join(f"RVOL>{r:.0f}×→{m:.1f}×ATR" for r, m in atr_mult_tiers) + ")"
              f" | clamp {getattr(config, 'ATR_STOP_MIN_PCT', 0.02):.0%}-{getattr(config, 'ATR_STOP_MAX_PCT', 0.10):.0%}")
        print(f"  Trail: {getattr(config, 'ATR_TRAIL_MULT', 1.5):.1f}×ATR | Target: {getattr(config, 'ATR_TARGET_MULT', 5.0):.1f}×ATR")
    else:
        exit_tiers = getattr(config, "RVOL_EXIT_TIERS", [])
        if exit_tiers:
            print(f"  Exit tiers: " + ", ".join(
                f"RVOL>{r:.0f}×→stop{s:.0%}/tgt{t:.0%}/trail{a:.0%}/{tr:.0%}"
                for r, s, t, a, tr in exit_tiers))
    print(f"  Re-entry: {'ON (max ' + str(config.RTG_REENTRY_MAX) + ')' if getattr(config, 'RTG_REENTRY_ALLOWED', False) else 'OFF'}")
    print(f"  Profit Protect: {'ON (ratio=' + f'{profit_ratio:.0%}' + ', min=$' + f'{profit_min:.0f}' + ')' if profit_protect else 'OFF'}")
    if progressive_tiers:
        print(f"  Progressive Trail: " + ", ".join(f"profit>{t:.0%}→trail{p:.1%}" for t, p in progressive_tiers))
    else:
        print(f"  Progressive Trail: OFF")

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
        max_cands = getattr(config, "MAX_CANDIDATES", 5)
        candidates = candidates.head(max_cands)

        print(f"\n--- {date_key} ({len(candidates)} candidates by RVOL, equity: ${equity:,.2f}) ---")
        for _, row in candidates.iterrows():
            sym = row["symbol"]
            rvol = row.get("rvol", 0)
            gap_pct = row["gap_pct"]
            open_p = row["open_price"]
            atr_v = row.get("atr", 0)
            atr_str = f" ATR=${atr_v:.3f}" if atr_v > 0 else ""
            print(f"  {sym} gap={gap_pct:+.1%} RVOL={rvol:.1f}× open=${open_p:.2f}{atr_str}")

        # Pre-fetch all 1-min bars and find entry signals
        cached_bars = {}  # symbol -> bars_list
        entry_info = {}   # symbol -> (entry_price, entry_bar_idx, signal_type)
        candidate_atrs = {}  # symbol -> atr

        tier_counts = {}
        for _, r in candidates.iterrows():
            rvol_r = r.get("rvol", 0)
            tier_key = _get_rvol_tier(rvol_r)[0]
            tier_counts[tier_key] = tier_counts.get(tier_key, 0) + 1
            atr_v = r.get("atr", 0)
            if atr_v > 0:
                candidate_atrs[r["symbol"]] = atr_v

        for _, row in candidates.iterrows():
            symbol = row["symbol"]
            open_price = row["open_price"]
            rvol = row.get("rvol", 0)

            bars_1m = get_1min_bars(client, symbol, date)
            if bars_1m.empty or len(bars_1m) < 2:
                continue
            bars_list = _bars_to_list(bars_1m)
            cached_bars[symbol] = bars_list

            # RVOL-adaptive min volume
            min_vol = config.RTG_MIN_VOLUME
            if rvol >= 10:
                min_vol = max(config.RTG_MIN_VOLUME // 3, 5000)
            elif rvol >= 5:
                min_vol = max(config.RTG_MIN_VOLUME // 2, 10000)

            # Find first RTG entry
            entry_at_open, entry_at_close, entry_bar_idx, confirmed, signal_type = find_rtg_entry_1min(
                bars_1m, open_price, min_volume=min_vol)
            if confirmed and entry_at_open > 0:
                entry_info[symbol] = (entry_at_open, entry_bar_idx, signal_type, rvol, open_price)

        if not entry_info:
            continue

        # ── Bar-by-bar concurrent simulation ──
        # Build timeline: union of all bar timestamps
        # Each symbol enters at its RTG signal, then we track all open positions bar by bar
        max_daily_loss = equity * config.MAX_DAILY_LOSS_PCT
        daily_realized_pnl = 0.0
        max_daily_pnl = 0.0
        profit_protect_triggered = False

        # Sort entries by bar index (earliest entry first for sizing)
        sorted_entries = sorted(entry_info.items(), key=lambda x: x[1][1])

        open_positions = []
        closed_trades = []
        entered_symbols = set()
        entry_slippage = getattr(config, "SLIPPAGE_ENTRY_PCT", 0.005)

        for symbol, (entry_price, entry_bar_idx, signal_type, rvol, open_price) in sorted_entries:
            if symbol in entered_symbols:
                continue
            if len(entered_symbols) >= config.MAX_POSITIONS:
                break

            same_tier = tier_counts.get(_get_rvol_tier(rvol)[0], 1)
            atr = candidate_atrs.get(symbol, 0)
            if atr > 0:
                stop_p, target_p, trail_act_p, trail_p = get_atr_stop_params(rvol, atr, entry_price)
            else:
                stop_p, target_p, trail_act_p, trail_p = get_rvol_exit_params(rvol)

            entry_price_actual = round(entry_price * (1 + entry_slippage), 4)
            pos_size = get_rvol_sizing(rvol, equity, same_tier_count=same_tier)
            pos_size = max(config.MIN_POSITION_SIZE, pos_size)
            shares = int(pos_size / entry_price_actual)
            if shares <= 0:
                continue

            pos = OpenPosition(
                symbol=symbol, shares=shares, entry_price=entry_price_actual,
                entry_bar_idx=entry_bar_idx, open_price=open_price, rvol=rvol,
                atr=atr,
                stop_pct=stop_p, target_pct=target_p,
                trail_activate_pct=trail_act_p, trail_pct=trail_p,
                signal_type=signal_type,
            )
            open_positions.append(pos)
            entered_symbols.add(symbol)

        # Build global bar timeline from all symbols' bars
        all_bar_times = {}  # timestamp -> {symbol: bar}
        for symbol in entered_symbols:
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

            # Evaluate each open position with this bar
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

            # Check daily profit protection
            if profit_protect and not profit_protect_triggered:
                # Calculate total P&L: realized + unrealized
                realized = sum(p.pnl for p in closed_trades)
                unrealized = sum(
                    (b["close"] - p.entry_price) * p.shares
                    for p in open_positions if not p.closed
                    for b in [bars_this_min.get(p.symbol, {})]
                    if "close" in b
                )
                total_pnl = realized + unrealized
                if total_pnl > max_daily_pnl:
                    max_daily_pnl = total_pnl

                if max_daily_pnl >= profit_min and total_pnl <= max_daily_pnl * profit_ratio:
                    # Trigger profit protect: force close all open positions
                    for p in open_positions:
                        if not p.closed:
                            bar = bars_this_min.get(p.symbol, {})
                            if "close" in bar:
                                p.force_close(bar["close"], bar_idx or 0, "profit_protect")
                                closed_trades.append(p)
                    profit_protect_triggered = True
                    daily_realized_pnl = sum(p.pnl for p in closed_trades)
                    print(f"  *** PROFIT PROTECT triggered at {ts.strftime('%H:%M')}: "
                          f"max_pnl=${max_daily_pnl:+,.2f}, cur_pnl=${total_pnl:+,.2f}, "
                          f"ratio={total_pnl/max_daily_pnl:.0%} ***")

            # Check daily loss limit
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

        # Force close any remaining open positions at end of day
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
                target_price=pos.target_price,
                trailing_high=round(pos.highest, 4),
                exit_bar_idx=pos.exit_bar_idx,
                entry_bar_idx=pos.entry_bar_idx,
                position_size=round(pos.entry_price * pos.shares, 2),
                signal_type=pos.signal_type,
            )

            bars = cached_bars.get(pos.symbol, [])
            entry_ts = _bar_ts_str(bars, pos.entry_bar_idx)
            exit_ts = _bar_ts_str(bars, min(pos.exit_bar_idx, len(bars) - 1))

            atr_info = f" ATR=${pos.atr:.3f}" if pos.atr > 0 else ""
            print(f"  {pos.symbol} [{pos.signal_type}] entry=${pos.entry_price:.4f}@{entry_ts} "
                  f"exit=${pos.exit_price:.4f}@{exit_ts} ({pos.exit_reason}), "
                  f"P&L=${pos.pnl:+,.2f} ({pos.pnl_pct:+.2%}) "
                  f"[RVOL={pos.rvol:.1f}× stop={pos.stop_pct:.1%}{atr_info} tgt={pos.target_pct:.0%}]")

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
                    "stop_price": pos.stop_price, "target_price": pos.target_price,
                    "pnl": pos.pnl,
                    "open_price": pos.open_price, "signal": pos.signal_type,
                }

    print(f"\n{'=' * 70}")
    print(f"[rtg_2.0] Backtest complete. Final equity: ${equity:,.2f}")
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
