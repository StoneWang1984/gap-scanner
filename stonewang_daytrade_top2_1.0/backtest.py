"""Backtesting engine — stonewang_daytrade_top2_1.0: Top-2 Ranking + Open Entry + 5-Bar Add + Trailing Stop.

Strategy per trading day:
  1. Scan gap-up candidates, rank by RVOL → top 40
  2. Compute first5chg for each candidate using 1-min bars
  3. Rank by first5chg descending, select top N (default 2)
  4. For each selected stock:
     a. Enter base position (30%) at open price (09:30)
     b. At 09:35, check 5-bar filter:
        - PASS: Add 70% position at bar5 close, use trailing stop (2% after +3%)
        - FAIL: Hold base with 5% stop, exit at 10:25
  5. Exit: trailing stop, initial stop loss, or force close at 15:50
"""

import json
import os
import re
import sys
import importlib.util
from collections import Counter

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

# Load strategy
_strat_spec = importlib.util.spec_from_file_location("strategy", os.path.join(_ver_dir, "strategy.py"))
strategy = importlib.util.module_from_spec(_strat_spec)
_strat_spec.loader.exec_module(strategy)
sys.modules["strategy"] = strategy
evaluate_trade_top2 = strategy.evaluate_trade_top2
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


# ── 5-Bar entry filter ───────────────────────────────────────────────
def check_frik5bar_filter(bars_1m, open_price, gap_pct):
    """5-bar filter: first bar bullish + first 5-min change > threshold + gap cap."""
    n_bars = getattr(config, "FRIK5BAR_BARS", 5)
    if len(bars_1m) < n_bars:
        return False, f"insufficient_bars({len(bars_1m)}<{n_bars})"

    if getattr(config, "FRIK5BAR_BAR1_BULLISH", True):
        bar1_close = float(bars_1m.iloc[0]["close"])
        if bar1_close <= open_price:
            return False, "bar1_bearish"

    min_chg = getattr(config, "FRIK5BAR_MIN_5MIN_CHG", 0.02)
    bar5_close = float(bars_1m.iloc[n_bars - 1]["close"])
    first5_chg = (bar5_close / open_price) - 1.0
    if first5_chg < min_chg:
        return False, f"first5chg_{first5_chg:.2%}"

    max_gap = getattr(config, "FRIK5BAR_MAX_GAP", 0.25)
    if max_gap > 0 and gap_pct >= max_gap:
        return False, f"gap_{gap_pct:.1%}"

    return True, "pass"


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


def save_backtest_charts(chart_entries, filepath="versions/chart_data_top2.json"):
    date_parts = sorted(set(v["date"] for v in chart_entries.values()))
    date_range = f"{date_parts[0]} to {date_parts[-1]}" if len(date_parts) > 1 else date_parts[0]
    output = {"date": date_range, "symbols": chart_entries}
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Chart data saved to {filepath} ({len(chart_entries)} symbols)")


def _simulate_trade(bars_1m, all_bars, open_price, gap_pct, rvol, equity):
    """Simulate one trade: open entry → 5-bar check → add or hold → trailing stop/exit."""
    entry_slippage = getattr(config, "SLIPPAGE_ENTRY_PCT", 0.005)
    exit_slippage = getattr(config, "SLIPPAGE_EXIT_PCT", 0.005)
    base_pct = getattr(config, "OPEN_ENTRY_BASE_PCT", 0.30)
    add_pct = getattr(config, "OPEN_ENTRY_ADD_PCT", 0.70)
    stop_p = getattr(config, "STOP_LOSS_PCT", 0.03)
    base_stop_p = getattr(config, "BASE_STOP_PCT", 0.05)
    trail_start = getattr(config, "TRAIL_START", 0.03)
    trail_pct = getattr(config, "TRAIL_PCT", 0.02)

    # Base entry at open price (bar 0)
    base_entry = round(open_price * (1 + entry_slippage), 4)
    pos_size = max(config.MIN_POSITION_SIZE, get_rvol_sizing(rvol, equity))
    base_shares = int((pos_size * base_pct) / base_entry)
    if base_shares <= 0:
        return None

    # Check 5-bar filter
    passed, reason = check_frik5bar_filter(bars_1m, open_price, gap_pct)

    if passed:
        # Add position at bar5 close
        n_bars = getattr(config, "FRIK5BAR_BARS", 5)
        add_bar_idx = n_bars - 1
        add_price = float(bars_1m.iloc[add_bar_idx]["close"])
        add_entry = round(add_price * (1 + entry_slippage), 4)
        add_shares = int((pos_size * add_pct) / add_entry)

        total_cost = base_entry * base_shares + add_entry * add_shares
        total_shares = base_shares + add_shares
        avg_entry = total_cost / total_shares

        # Trailing stop from bar5 onwards
        remaining = all_bars[add_bar_idx + 1:]
        if not remaining:
            return None

        stop_price = avg_entry * (1 - stop_p)
        highest = avg_entry
        trailing_active = False

        for bi, rb in enumerate(remaining):
            bar_high = rb["high"]
            bar_low = rb["low"]
            bar_close = rb["close"]
            highest = max(highest, bar_high)

            if not trailing_active and highest / avg_entry - 1 >= trail_start:
                trailing_active = True
                stop_price = highest * (1 - trail_pct)

            if trailing_active:
                stop_price = max(stop_price, highest * (1 - trail_pct))

            if bar_low <= stop_price:
                exit_price = stop_price
                exit_reason = "trail_stop" if trailing_active else "stop_loss"
                pnl = (exit_price - avg_entry) * total_shares
                entry_ts = "09:30"
                exit_ts = rb["timestamp"].strftime("%H:%M")
                return {
                    "symbol": None, "entry_price": avg_entry, "exit_price": round(exit_price, 4),
                    "shares": total_shares, "pnl": round(pnl, 2),
                    "pnl_pct": exit_price / avg_entry - 1,
                    "exit_reason": exit_reason, "entry_ts": entry_ts, "exit_ts": exit_ts,
                    "signal": "top2_added", "filter_result": "pass",
                }

        # Force close at last bar
        exit_price = remaining[-1]["close"] * (1 - exit_slippage)
        pnl = (exit_price - avg_entry) * total_shares
        exit_ts = remaining[-1]["timestamp"].strftime("%H:%M")
        return {
            "symbol": None, "entry_price": avg_entry, "exit_price": round(exit_price, 4),
            "shares": total_shares, "pnl": round(pnl, 2),
            "pnl_pct": exit_price / avg_entry - 1,
            "exit_reason": "force_close", "entry_ts": "09:30", "exit_ts": exit_ts,
            "signal": "top2_added", "filter_result": "pass",
        }

    else:
        # Filter failed — hold base with wide stop, exit at 10:25
        remaining = all_bars[1:]
        if not remaining:
            return None

        stop_price = base_entry * (1 - base_stop_p)
        exit_h, exit_m = 10, 25
        exit_price = 0.0
        exit_reason = "force_close"
        exit_ts = "15:50"

        for bi, rb in enumerate(remaining):
            rb_ts = rb["timestamp"].strftime("%H:%M")
            rb_h, rb_m = (int(x) for x in rb_ts.split(":"))
            if rb["low"] <= stop_price:
                exit_price = stop_price
                exit_reason = "stop_loss"
                exit_ts = rb_ts
                break
            if rb_h > exit_h or (rb_h == exit_h and rb_m >= exit_m):
                exit_price = rb["close"] * (1 - exit_slippage)
                exit_reason = "10:25_exit"
                exit_ts = rb_ts
                break

        if exit_price == 0.0:
            exit_price = remaining[-1]["close"] * (1 - exit_slippage)

        pnl = (exit_price - base_entry) * base_shares
        return {
            "symbol": None, "entry_price": base_entry, "exit_price": round(exit_price, 4),
            "shares": base_shares, "pnl": round(pnl, 2),
            "pnl_pct": exit_price / base_entry - 1,
            "exit_reason": exit_reason, "entry_ts": "09:30", "exit_ts": exit_ts,
            "signal": "top2_base_only", "filter_result": reason,
        }


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

    top_n = getattr(config, "TOP_N", 2)
    print(f"[top2_1.0] Backtesting {len(trading_days)} trading days: "
          f"{trading_days[0].date()} to {trading_days[-1].date()}")
    print(f"Capital: ${config.INITIAL_CAPITAL:,.2f} | Top-{top_n} by first5chg | "
          f"Max concurrent: {config.MAX_POSITIONS}")
    print(f"Entry: open price (30%) → 5-bar check → add (70%) if pass")
    print(f"Exit: trailing stop ({config.TRAIL_PCT:.0%} after +{config.TRAIL_START:.0%}) or "
          f"{config.STOP_LOSS_PCT:.0%} stop | base fail: {config.BASE_STOP_PCT:.0%} stop / 10:25 exit")
    if getattr(config, "FRIK5BAR_ENABLED", False):
        filter_parts = []
        if getattr(config, "FRIK5BAR_BAR1_BULLISH", True):
            filter_parts.append("bar1_bullish")
        filter_parts.append(f"first5chg>{getattr(config, 'FRIK5BAR_MIN_5MIN_CHG', 0.02):.0%}")
        max_gap = getattr(config, "FRIK5BAR_MAX_GAP", 0.25)
        if max_gap > 0:
            filter_parts.append(f"gap<{max_gap:.0%}")
        print(f"5-Bar filter: {' + '.join(filter_parts)}")

    print("\nLoading tradable symbols...")
    symbols = get_tradable_symbols()
    symbols = [s for s in symbols if not is_leveraged_etf(s)]
    symbols = [s for s in symbols if not is_crypto_etf(s)]
    print(f"Using {len(symbols)} symbols")

    print("\nBulk scanning for gaps...")
    gap_data = bulk_scan_gaps(client, trading_days, symbols)
    total_candidates = sum(len(v) for v in gap_data.values())
    print(f"Found {total_candidates} gap entries across {len(gap_data)} days")

    all_trades = []
    equity = config.INITIAL_CAPITAL
    chart_entries = {}
    filter_stats = {"total_candidates": 0, "filter_pass": 0, "filter_fail_bar1": 0,
                    "filter_fail_5min": 0, "filter_fail_gap": 0, "filter_fail_bars": 0}

    for date in trading_days:
        date_key = date.date()
        if date_key not in gap_data or gap_data[date_key].empty:
            continue

        candidates = gap_data[date_key]
        candidates = candidates.head(config.MAX_CANDIDATES)

        print(f"\n--- {date_key} ({len(candidates)} candidates, equity: ${equity:,.2f}) ---")

        # Fetch all bars and compute first5chg for ranking
        scored_entries = []
        for _, row in candidates.iterrows():
            symbol = row["symbol"]
            open_price = row["open_price"]
            rvol = row.get("rvol", 0)
            gap_pct = row["gap_pct"]

            bars_1m = get_1min_bars(client, symbol, date)
            if bars_1m.empty or len(bars_1m) < 5:
                continue
            all_bars = _bars_to_list(bars_1m)

            bar5_close = float(bars_1m.iloc[4]["close"])
            first5_chg = (bar5_close / open_price) - 1.0

            scored_entries.append({
                "symbol": symbol, "open_price": open_price, "gap_pct": gap_pct,
                "rvol": rvol, "first5chg": first5_chg,
                "bars_1m": bars_1m, "all_bars": all_bars,
            })

        # Rank by first5chg and select top N
        scored_entries.sort(key=lambda x: x["first5chg"], reverse=True)
        selected = scored_entries[:top_n]

        if not selected:
            continue

        for entry in selected:
            sym = entry["symbol"]
            open_price = entry["open_price"]
            gap_pct = entry["gap_pct"]
            rvol = entry["rvol"]
            first5_chg = entry["first5chg"]

            filter_stats["total_candidates"] += 1

            result = _simulate_trade(
                entry["bars_1m"], entry["all_bars"], open_price, gap_pct, rvol, equity,
            )
            if result is None:
                continue

            result["symbol"] = sym
            result["date"] = str(date_key)
            result["open_price"] = open_price
            result["first5chg"] = first5_chg

            # Track filter stats
            if result["filter_result"] == "pass":
                filter_stats["filter_pass"] += 1
            else:
                fr = result["filter_result"]
                if "bar1_bearish" in fr:
                    filter_stats["filter_fail_bar1"] += 1
                elif "first5chg" in fr:
                    filter_stats["filter_fail_5min"] += 1
                elif "gap" in fr:
                    filter_stats["filter_fail_gap"] += 1

            print(f"  {sym} [top2] first5chg={first5_chg:+.2%} "
                  f"entry=${result['entry_price']:.4f}@{result['entry_ts']} "
                  f"exit=${result['exit_price']:.4f}@{result['exit_ts']} "
                  f"({result['exit_reason']}) "
                  f"P&L=${result['pnl']:+,.2f} ({result['pnl_pct']:+.2%}) "
                  f"[filter={result['filter_result']}]")

            all_trades.append(result)
            equity += result["pnl"]

            # Chart data
            sym_key = f"{sym} ({date_key})"
            chart_bars = _bars_to_chart(entry["bars_1m"])
            events = [
                {"ts": result["entry_ts"], "type": "buy", "price": result["entry_price"],
                 "label": f"BUY {result['shares']}sh [{result['signal']}]"},
                {"ts": result["exit_ts"], "type": "sell", "price": result["exit_price"],
                 "label": f"{result['exit_reason'].upper()} {result['shares']}sh"},
            ]
            chart_entries[sym_key] = {
                "date": str(date_key), "bars_1m": chart_bars, "events": events,
                "entry_price": result["entry_price"], "pnl": result["pnl"],
                "open_price": open_price, "signal": result["signal"],
            }

    print(f"\n{'=' * 70}")
    print(f"[top2_1.0] Backtest complete. Final equity: ${equity:,.2f}")
    print(f"Total trades: {len(all_trades)}")
    if all_trades:
        wins = [t for t in all_trades if t["pnl"] > 0]
        losses = [t for t in all_trades if t["pnl"] <= 0]
        win_rate = len(wins) / len(all_trades)
        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        total_pnl = sum(t["pnl"] for t in all_trades)
        rr = abs(avg_win / avg_loss) if avg_loss != 0 else 0
        print(f"Win rate: {win_rate:.1%} ({len(wins)}W / {len(losses)}L)")
        print(f"Avg win: ${avg_win:+,.2f} | Avg loss: ${avg_loss:+,.2f} | R/R: {rr:.1f}:1")
        print(f"Total P&L: ${total_pnl:+,.2f} ({total_pnl/config.INITIAL_CAPITAL:+.1%})")
        reasons = Counter(t["exit_reason"] for t in all_trades)
        print(f"Exit reasons: {dict(reasons)}")
        signals = Counter(t["signal"] for t in all_trades)
        print(f"Signal types: {dict(signals)}")

    # Filter stats
    if filter_stats["total_candidates"] > 0:
        print(f"\n5-Bar Filter Stats (Top-{top_n} selected):")
        print(f"  Total selected: {filter_stats['total_candidates']}")
        print(f"  Filter passed (added): {filter_stats['filter_pass']}")
        print(f"  Filter failed (base only): {filter_stats['total_candidates'] - filter_stats['filter_pass']}")
        if filter_stats["filter_fail_bar1"] > 0:
            print(f"    - bar1 bearish: {filter_stats['filter_fail_bar1']}")
        if filter_stats["filter_fail_5min"] > 0:
            print(f"    - first5chg too low: {filter_stats['filter_fail_5min']}")
        if filter_stats["filter_fail_gap"] > 0:
            print(f"    - gap too large: {filter_stats['filter_fail_gap']}")

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
