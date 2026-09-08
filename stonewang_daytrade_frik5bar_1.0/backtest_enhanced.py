"""Enhanced backtest — compare 3 strategy improvements for frik5bar.

Strategies compared:
  A) BASELINE: Current frik5bar (5-bar filter pass → entry at bar5 close, 3% stop, 10:25 exit)
  B) TOP-N RANKING: Rank by first5chg, only buy top 1-2 stocks per day
  C) TRAILING STOP: Replace fixed stop/exit with trailing stop (let winners run)
  D) OPEN ENTRY: Enter at open price, add position on 5-bar confirmation
  E) ALL COMBINED: Top-N + trailing stop + open entry

Data is fetched once and shared across all strategies.
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

# Load frik5bar backtest module (not parent backtest.py)
_bt_spec = importlib.util.spec_from_file_location("frik5bar_backtest", os.path.join(_ver_dir, "backtest.py"))
frik5bar_bt = importlib.util.module_from_spec(_bt_spec)
_bt_spec.loader.exec_module(frik5bar_bt)

is_leveraged_etf = frik5bar_bt.is_leveraged_etf
is_crypto_etf = frik5bar_bt.is_crypto_etf
get_trading_days = frik5bar_bt.get_trading_days
bulk_scan_gaps = frik5bar_bt.bulk_scan_gaps
get_1min_bars = frik5bar_bt.get_1min_bars
_bars_to_list = frik5bar_bt._bars_to_list
check_frik5bar_filter = frik5bar_bt.check_frik5bar_filter
get_rvol_sizing = frik5bar_bt.get_rvol_sizing
_get_rvol_tier = frik5bar_bt._get_rvol_tier


# ── Strategy execution functions ──────────────────────────────────────

def strategy_baseline(bars_1m, all_bars, open_price, gap_pct, rvol, equity, config_override=None):
    """A) BASELINE: Current system — 5-bar filter → entry at bar5 close, 3% stop, 10:25 exit."""
    n_bars = 5
    passed, reason = check_frik5bar_filter(bars_1m, open_price, gap_pct)
    if not passed:
        return None

    entry_bar_idx = n_bars - 1
    bar5 = bars_1m.iloc[entry_bar_idx]
    entry_price = float(bar5["close"])
    entry_price_actual = round(entry_price * 1.005, 4)

    stop_p = 0.03
    exit_h, exit_m = 10, 25
    stop_price = entry_price_actual * (1 - stop_p)

    remaining = all_bars[entry_bar_idx + 1:]
    if not remaining:
        return None

    pos_size = max(40, get_rvol_sizing(rvol, equity))
    shares = int(pos_size / entry_price_actual)
    if shares <= 0:
        return None

    exit_price, exit_reason = _exit_fixed_stop_time(remaining, stop_price, exit_h, exit_m)
    pnl = (exit_price - entry_price_actual) * shares
    entry_ts = all_bars[entry_bar_idx]["timestamp"].strftime("%H:%M")

    return {"symbol": None, "entry_price": entry_price_actual, "exit_price": round(exit_price, 4),
            "shares": shares, "pnl": round(pnl, 2), "pnl_pct": exit_price / entry_price_actual - 1,
            "exit_reason": exit_reason, "entry_ts": entry_ts, "signal": "baseline"}


def strategy_top_n(scored_entries, equity, top_n=2):
    """B) TOP-N RANKING: Only trade the top N stocks ranked by first5chg per day."""
    trades = []
    sorted_entries = sorted(scored_entries, key=lambda x: x["first5chg"], reverse=True)
    for entry in sorted_entries[:top_n]:
        result = strategy_baseline(
            entry["bars_1m"], entry["all_bars"], entry["open_price"],
            entry["gap_pct"], entry["rvol"], equity,
        )
        if result:
            result["symbol"] = entry["symbol"]
            result["signal"] = f"top{top_n}"
            trades.append(result)
    return trades


def strategy_trailing_stop(bars_1m, all_bars, open_price, gap_pct, rvol, equity,
                           stop_pct=0.03, trail_start=0.03, trail_pct=0.015):
    """C) TRAILING STOP: 5-bar filter → entry, but use trailing stop instead of fixed 10:25 exit.

    Logic:
    - Initial stop: entry * (1 - stop_pct)
    - Once profit reaches trail_start (e.g. +3%), switch to trailing stop at trail_pct
    - Trail: highest high since entry * (1 - trail_pct)
    - No fixed time exit — ride until trailing stop hit or 15:50 force close
    """
    passed, reason = check_frik5bar_filter(bars_1m, open_price, gap_pct)
    if not passed:
        return None

    n_bars = 5
    entry_bar_idx = n_bars - 1
    bar5 = bars_1m.iloc[entry_bar_idx]
    entry_price = float(bar5["close"])
    entry_price_actual = round(entry_price * 1.005, 4)

    remaining = all_bars[entry_bar_idx + 1:]
    if not remaining:
        return None

    pos_size = max(40, get_rvol_sizing(rvol, equity))
    shares = int(pos_size / entry_price_actual)
    if shares <= 0:
        return None

    stop_price = entry_price_actual * (1 - stop_pct)
    highest = entry_price_actual
    trailing_active = False

    for bi, rb in enumerate(remaining):
        bar_high = rb["high"]
        bar_low = rb["low"]
        bar_close = rb["close"]
        highest = max(highest, bar_high)

        if not trailing_active:
            if highest / entry_price_actual - 1 >= trail_start:
                trailing_active = True
                stop_price = highest * (1 - trail_pct)

        if trailing_active:
            new_stop = highest * (1 - trail_pct)
            stop_price = max(stop_price, new_stop)

        if bar_low <= stop_price:
            exit_price = stop_price
            exit_reason = "trail_stop" if trailing_active else "stop_loss"
            pnl = (exit_price - entry_price_actual) * shares
            entry_ts = all_bars[entry_bar_idx]["timestamp"].strftime("%H:%M")
            return {"symbol": None, "entry_price": entry_price_actual, "exit_price": round(exit_price, 4),
                    "shares": shares, "pnl": round(pnl, 2), "pnl_pct": exit_price / entry_price_actual - 1,
                    "exit_reason": exit_reason, "entry_ts": entry_ts, "signal": "trailing_stop"}

    # Force close at last bar (15:50 area)
    exit_price = remaining[-1]["close"]
    pnl = (exit_price - entry_price_actual) * shares
    entry_ts = all_bars[entry_bar_idx]["timestamp"].strftime("%H:%M")
    return {"symbol": None, "entry_price": entry_price_actual, "exit_price": round(exit_price, 4),
            "shares": shares, "pnl": round(pnl, 2), "pnl_pct": exit_price / entry_price_actual - 1,
            "exit_reason": "force_close", "entry_ts": entry_ts, "signal": "trailing_stop"}


def strategy_open_entry(bars_1m, all_bars, open_price, gap_pct, rvol, equity,
                        base_pct=0.30, add_pct=0.70, stop_pct=0.05, trail_pct=0.02):
    """D) OPEN ENTRY: Enter at open price (base position), add on 5-bar confirmation.

    Logic:
    - Enter 30% position at open price (09:30)
    - If 5-bar filter passes at 09:35, add 70% position at bar5 close
    - If 5-bar filter fails, hold base with wide stop (5%) or exit at 10:25
    - After add: trailing stop (2%) after +3% profit
    """
    entry_slippage = 1.005
    exit_slippage = 0.995

    # Base entry at open
    base_entry = round(open_price * entry_slippage, 4)
    pos_size = max(40, get_rvol_sizing(rvol, equity))
    base_shares = int((pos_size * base_pct) / base_entry)
    if base_shares <= 0:
        return None

    # Check 5-bar filter
    passed, reason = check_frik5bar_filter(bars_1m, open_price, gap_pct)

    if passed:
        # Add position at bar5 close
        n_bars = 5
        add_bar_idx = n_bars - 1
        add_price = float(bars_1m.iloc[add_bar_idx]["close"])
        add_entry = round(add_price * entry_slippage, 4)
        add_shares = int((pos_size * add_pct) / add_entry)

        # Weighted average entry
        total_cost = base_entry * base_shares + add_entry * add_shares
        total_shares = base_shares + add_shares
        avg_entry = total_cost / total_shares

        # Trailing stop logic from bar5 onwards
        remaining = all_bars[add_bar_idx + 1:]
        if not remaining:
            return None

        stop_price = avg_entry * (1 - stop_pct)
        highest = avg_entry
        trailing_active = False

        for bi, rb in enumerate(remaining):
            bar_high = rb["high"]
            bar_low = rb["low"]
            bar_close = rb["close"]
            highest = max(highest, bar_high)

            if not trailing_active and highest / avg_entry - 1 >= 0.03:
                trailing_active = True
                stop_price = highest * (1 - trail_pct)

            if trailing_active:
                stop_price = max(stop_price, highest * (1 - trail_pct))

            if bar_low <= stop_price:
                exit_price = stop_price
                exit_reason = "trail_stop" if trailing_active else "stop_loss"
                pnl = (exit_price * exit_slippage - avg_entry) * total_shares
                return {"symbol": None, "entry_price": avg_entry, "exit_price": round(exit_price, 4),
                        "shares": total_shares, "pnl": round(pnl, 2),
                        "pnl_pct": (exit_price * exit_slippage / avg_entry) - 1,
                        "exit_reason": exit_reason, "entry_ts": "09:30",
                        "signal": "open_entry_added"}

        exit_price = remaining[-1]["close"] * exit_slippage
        pnl = (exit_price - avg_entry) * total_shares
        return {"symbol": None, "entry_price": avg_entry, "exit_price": round(exit_price, 4),
                "shares": total_shares, "pnl": round(pnl, 2),
                "pnl_pct": exit_price / avg_entry - 1,
                "exit_reason": "force_close", "entry_ts": "09:30",
                "signal": "open_entry_added"}

    else:
        # Filter failed — hold base with wide stop, exit at 10:25
        remaining = all_bars[1:]
        if not remaining:
            return None

        stop_price = base_entry * (1 - stop_pct)
        exit_price = 0.0
        exit_reason = "force_close"

        for bi, rb in enumerate(remaining):
            rb_ts = rb["timestamp"].strftime("%H:%M")
            rb_h, rb_m = (int(x) for x in rb_ts.split(":"))
            if rb["low"] <= stop_price:
                exit_price = stop_price
                exit_reason = "stop_loss"
                break
            if rb_h > 10 or (rb_h == 10 and rb_m >= 25):
                exit_price = rb["close"] * exit_slippage
                exit_reason = "10:25_exit"
                break

        if exit_price == 0.0:
            exit_price = remaining[-1]["close"] * exit_slippage

        pnl = (exit_price - base_entry) * base_shares
        return {"symbol": None, "entry_price": base_entry, "exit_price": round(exit_price, 4),
                "shares": base_shares, "pnl": round(pnl, 2),
                "pnl_pct": exit_price / base_entry - 1,
                "exit_reason": exit_reason, "entry_ts": "09:30",
                "signal": "open_entry_nofilter"}


def strategy_combined(scored_entries, equity, top_n=2, stop_pct=0.03,
                      trail_start=0.03, trail_pct=0.015):
    """E) ALL COMBINED: Top-N ranking + trailing stop + open entry."""
    trades = []
    sorted_entries = sorted(scored_entries, key=lambda x: x["first5chg"], reverse=True)
    for entry in sorted_entries[:top_n]:
        result = strategy_open_entry(
            entry["bars_1m"], entry["all_bars"], entry["open_price"],
            entry["gap_pct"], entry["rvol"], equity,
            base_pct=0.30, add_pct=0.70, stop_pct=stop_pct, trail_pct=trail_pct,
        )
        if result:
            result["symbol"] = entry["symbol"]
            result["signal"] = f"combined_top{top_n}"
            trades.append(result)
    return trades


# ── Helper ────────────────────────────────────────────────────────────

def _exit_fixed_stop_time(remaining, stop_price, exit_h, exit_m):
    for rb in remaining:
        rb_ts = rb["timestamp"].strftime("%H:%M")
        rb_h, rb_m = (int(x) for x in rb_ts.split(":"))
        if rb["low"] <= stop_price:
            return stop_price, "stop_loss"
        if rb_h > exit_h or (rb_h == exit_h and rb_m >= exit_m):
            return rb["close"], "10:25_exit"
    return remaining[-1]["close"], "force_close"


def _print_strategy_result(name, trades, initial_capital):
    if not trades:
        print(f"  {name}: No trades")
        return
    equity = initial_capital
    for t in trades:
        equity += t["pnl"]
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / len(trades) if trades else 0
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    total_pnl = sum(t["pnl"] for t in trades)
    rr_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    reasons = Counter(t["exit_reason"] for t in trades)

    print(f"  {name}:")
    print(f"    Equity: ${initial_capital:,.2f} → ${equity:,.2f} ({total_pnl/initial_capital:+.1%})")
    print(f"    Trades: {len(trades)} | WR: {win_rate:.1%} ({len(wins)}W/{len(losses)}L)")
    print(f"    Avg win: ${avg_win:+,.2f} | Avg loss: ${avg_loss:+,.2f} | R/R: {rr_ratio:.1f}:1")
    print(f"    Exits: {dict(reasons)}")


# ── Main ──────────────────────────────────────────────────────────────

def run_enhanced_backtest(end_date=None, n_days=30):
    client = get_data_client()
    if end_date is None:
        end_date = pd.Timestamp.now(tz="America/New_York")

    trading_days = get_trading_days(client, end_date, n_days)
    if not trading_days:
        print("No trading days found.")
        return

    print(f"[Enhanced Backtest] {len(trading_days)} trading days: "
          f"{trading_days[0].date()} to {trading_days[-1].date()}")
    print(f"Capital: ${config.INITIAL_CAPITAL:,.2f}")

    print("\nLoading tradable symbols...")
    symbols = get_tradable_symbols()
    symbols = [s for s in symbols if not is_leveraged_etf(s)]
    symbols = [s for s in symbols if not is_crypto_etf(s)]
    print(f"Using {len(symbols)} symbols (after ETF filters)")

    print("\nBulk scanning for gaps...")
    gap_data = bulk_scan_gaps(client, trading_days, symbols)
    total_candidates = sum(len(v) for v in gap_data.values())
    print(f"Found {total_candidates} gap entries across {len(gap_data)} days")

    # ── Run all strategies ─────────────────────────────────────────────
    strat_trades = {"A_baseline": [], "B_top1": [], "B_top2": [], "B_top3": [],
                    "C_trail": [], "C_trail_wide": [],
                    "D_open_entry": [], "E_combined_top2": [], "E_combined_top1": []}
    strat_equity = {k: config.INITIAL_CAPITAL for k in strat_trades}

    for date in trading_days:
        date_key = date.date()
        if date_key not in gap_data or gap_data[date_key].empty:
            continue

        candidates = gap_data[date_key]
        candidates = candidates.head(config.MAX_CANDIDATES)

        print(f"\n--- {date_key} ({len(candidates)} candidates) ---")

        # Fetch all bars for this day's candidates
        cached_bars = {}
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
            cached_bars[symbol] = (bars_1m, all_bars)

            # Compute first5chg for ranking
            bar5_close = float(bars_1m.iloc[4]["close"])
            first5_chg = (bar5_close / open_price) - 1.0

            scored_entries.append({
                "symbol": symbol, "open_price": open_price, "gap_pct": gap_pct,
                "rvol": rvol, "first5chg": first5_chg,
                "bars_1m": bars_1m, "all_bars": all_bars,
            })

        if not scored_entries:
            continue

        # A) Baseline — all filter-pass entries
        for entry in scored_entries:
            eq = strat_equity["A_baseline"]
            r = strategy_baseline(entry["bars_1m"], entry["all_bars"],
                                  entry["open_price"], entry["gap_pct"], entry["rvol"], eq)
            if r:
                r["symbol"] = entry["symbol"]
                strat_trades["A_baseline"].append(r)
                strat_equity["A_baseline"] += r["pnl"]

        # B) Top-N ranking
        for top_n, key in [(1, "B_top1"), (2, "B_top2"), (3, "B_top3")]:
            trades = strategy_top_n(scored_entries, strat_equity[key], top_n=top_n)
            for t in trades:
                strat_trades[key].append(t)
                strat_equity[key] += t["pnl"]

        # C) Trailing stop (standard: 3% stop, trail at 1.5% after +3%)
        for entry in scored_entries:
            eq = strat_equity["C_trail"]
            r = strategy_trailing_stop(entry["bars_1m"], entry["all_bars"],
                                       entry["open_price"], entry["gap_pct"], entry["rvol"], eq,
                                       stop_pct=0.03, trail_start=0.03, trail_pct=0.015)
            if r:
                r["symbol"] = entry["symbol"]
                strat_trades["C_trail"].append(r)
                strat_equity["C_trail"] += r["pnl"]

        # C) Trailing stop (wide: 5% stop, trail at 2% after +3%)
        for entry in scored_entries:
            eq = strat_equity["C_trail_wide"]
            r = strategy_trailing_stop(entry["bars_1m"], entry["all_bars"],
                                       entry["open_price"], entry["gap_pct"], entry["rvol"], eq,
                                       stop_pct=0.05, trail_start=0.03, trail_pct=0.02)
            if r:
                r["symbol"] = entry["symbol"]
                strat_trades["C_trail_wide"].append(r)
                strat_equity["C_trail_wide"] += r["pnl"]

        # D) Open entry (all candidates)
        for entry in scored_entries:
            eq = strat_equity["D_open_entry"]
            r = strategy_open_entry(entry["bars_1m"], entry["all_bars"],
                                    entry["open_price"], entry["gap_pct"], entry["rvol"], eq)
            if r:
                r["symbol"] = entry["symbol"]
                strat_trades["D_open_entry"].append(r)
                strat_equity["D_open_entry"] += r["pnl"]

        # E) Combined: Top-N + trailing stop + open entry
        for top_n, key in [(2, "E_combined_top2"), (1, "E_combined_top1")]:
            trades = strategy_combined(scored_entries, strat_equity[key], top_n=top_n)
            for t in trades:
                strat_trades[key].append(t)
                strat_equity[key] += t["pnl"]

    # ── Print results ──────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print(f"ENHANCED BACKTEST RESULTS ({n_days} days)")
    print(f"{'=' * 70}")
    ic = config.INITIAL_CAPITAL
    _print_strategy_result("A) Baseline (current frik5bar)", strat_trades["A_baseline"], ic)
    _print_strategy_result("B) Top-1 by first5chg", strat_trades["B_top1"], ic)
    _print_strategy_result("B) Top-2 by first5chg", strat_trades["B_top2"], ic)
    _print_strategy_result("B) Top-3 by first5chg", strat_trades["B_top3"], ic)
    _print_strategy_result("C) Trailing stop (3%/1.5%)", strat_trades["C_trail"], ic)
    _print_strategy_result("C) Trailing stop (5%/2%)", strat_trades["C_trail_wide"], ic)
    _print_strategy_result("D) Open entry + 5-bar add", strat_trades["D_open_entry"], ic)
    _print_strategy_result("E) Combined Top-2", strat_trades["E_combined_top2"], ic)
    _print_strategy_result("E) Combined Top-1", strat_trades["E_combined_top1"], ic)

    # ── Summary table ──────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print("SUMMARY TABLE")
    print(f"{'=' * 70}")
    print(f"{'Strategy':<30} {'Return':>8} {'Trades':>7} {'WR':>6} {'R/R':>6} {'AvgWin':>9} {'AvgLoss':>9}")
    print("-" * 75)

    labels = [
        ("A) Baseline", "A_baseline"),
        ("B) Top-1", "B_top1"),
        ("B) Top-2", "B_top2"),
        ("B) Top-3", "B_top3"),
        ("C) Trail 3%/1.5%", "C_trail"),
        ("C) Trail 5%/2%", "C_trail_wide"),
        ("D) Open+Add", "D_open_entry"),
        ("E) Comb Top-2", "E_combined_top2"),
        ("E) Comb Top-1", "E_combined_top1"),
    ]
    for label, key in labels:
        trades = strat_trades[key]
        if not trades:
            print(f"{label:<30} {'N/A':>8}")
            continue
        equity = ic + sum(t["pnl"] for t in trades)
        ret = (equity - ic) / ic
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        wr = len(wins) / len(trades)
        aw = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        al = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        rr = abs(aw / al) if al != 0 else 0
        print(f"{label:<30} {ret:>+7.1%} {len(trades):>7} {wr:>5.1%} {rr:>5.1f} {aw:>+8.2f} {al:>+8.2f}")

    return strat_trades


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--end", type=str, default=None)
    args = parser.parse_args()
    end = pd.Timestamp(args.end, tz="America/New_York") if args.end else None
    run_enhanced_backtest(end_date=end, n_days=args.days)
