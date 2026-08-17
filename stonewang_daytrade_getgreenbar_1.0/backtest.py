"""Backtesting engine — stonewang_daytrade_getgreenbar_1.0: 连续绿bar骑乘

Entry detection (1-min bars, approximates live trade-print bar detection):
  GreenBar signal:
    - bar[i-1].close < bar[i-1].open (prior bar red)
    - bar[i].close > bar[i].open (current bar green)
    - bar[i].volume >= GBAR_VOLUME_MULT × bar[i-1].volume (volume spike)
    - bar[i].volume >= GBAR_MIN_VOLUME (liquidity floor)
    - bar[i].close > open_price (above day's open)

Entry window: 09:30 - 15:30 EST (full day)
Re-entry allowed: up to MAX_DAILY_ENTRIES_PER_SYMBOL per stock per day.

Exit (evaluate_trade_greenbar):
  Priority: stop_loss > bar_turned_red > trail_stop > target > force_close
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

import config

from scanner import get_data_client, get_tradable_symbols

_strat_spec = importlib.util.spec_from_file_location("strategy", os.path.join(_ver_dir, "strategy.py"))
strategy = importlib.util.module_from_spec(_strat_spec)
_strat_spec.loader.exec_module(strategy)
sys.modules["strategy"] = strategy
evaluate_trade_greenbar = strategy.evaluate_trade_greenbar
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


def find_greenbar_entry(bars_1m, open_price, min_volume=None, search_from=0):
    """Find green bar sequence start entry on 1-min bars.

    Approximates live GreenBarDetector.should_enter():
    - bar[i-1] is red (close < open)
    - bar[i] is green (close > open)
    - bar[i].volume >= GBAR_VOLUME_MULT × bar[i-1].volume
    - bar[i].volume >= GBAR_MIN_VOLUME
    - bar[i].close > open_price

    Returns (entry_price, entry_bar_idx, "greenbar") or (0, -1, "").
    """
    if bars_1m.empty or len(bars_1m) < 2:
        return 0.0, -1, ""
    if min_volume is None:
        min_volume = config.GBAR_MIN_VOLUME

    entry_start_str = getattr(config, "ENTRY_WINDOW_START", "09:30")
    entry_end_str = getattr(config, "ENTRY_WINDOW_END", "15:30")

    for i in range(max(search_from, 1), len(bars_1m)):
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
        bar_close = float(bar["close"])
        bar_vol = int(bar["volume"])
        prev_open = float(prev_bar["open"])
        prev_close = float(prev_bar["close"])
        prev_vol = int(prev_bar["volume"])

        # Red → Green transition
        prev_red = prev_close < prev_open
        cur_green = bar_close > bar_open
        vol_spike = prev_vol > 0 and bar_vol >= config.GBAR_VOLUME_MULT * prev_vol
        vol_min = bar_vol >= min_volume
        above_open = bar_close > open_price

        if prev_red and cur_green and vol_spike and vol_min and above_open:
            entry_price = round(bar_close * 1.001, 4)  # signal bar close + slippage
            return entry_price, i, "greenbar"

    return 0.0, -1, ""


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

    print(f"[getgreenbar_1.0] Backtesting {len(trading_days)} trading days: "
          f"{trading_days[0].date()} to {trading_days[-1].date()}")
    print(f"Capital: ${config.INITIAL_CAPITAL:,.2f} | RVOL-weighted sizing | "
          f"Max concurrent: {config.MAX_POSITIONS} | Max daily trades: {config.MAX_DAILY_TRADES}")
    print(f"Entry window: {config.ENTRY_WINDOW_START}-{config.ENTRY_WINDOW_END} EST")
    print(f"  Signal: red→green bar transition + vol >= {config.GBAR_VOLUME_MULT}× prior + vol >= {config.GBAR_MIN_VOLUME:,}")
    print(f"  Exit: stop_loss > bar_turned_red > trail_stop > target")
    print(f"  Re-entry: ON (max {config.MAX_DAILY_ENTRIES_PER_SYMBOL}/symbol/day, cooldown {config.GBAR_REENTRY_COOLDOWN_SEC}s)")
    sizing_tiers = getattr(config, "RVOL_SIZING_TIERS", [])
    if sizing_tiers:
        print(f"  Sizing tiers: " + ", ".join(f"RVOL>{r:.0f}×→{p:.0%}" for r, p in sizing_tiers))
    exit_tiers = getattr(config, "RVOL_EXIT_TIERS", [])
    if exit_tiers:
        print(f"  Exit tiers: " + ", ".join(
            f"RVOL>{r:.0f}×→stop{s:.0%}/tgt{t:.0%}/trail{a:.0%}/{tr:.0%}"
            for r, s, t, a, tr in exit_tiers))

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
        entry_count = {}  # symbol → count
        cached_bars = {}
        bt_max_daily_trades = max(config.MAX_DAILY_TRADES, len(candidates) * 20)

        tier_counts = {}
        for _, r in candidates.iterrows():
            rvol_r = r.get("rvol", 0)
            tier_key = _get_rvol_tier(rvol_r)[0]
            tier_counts[tier_key] = tier_counts.get(tier_key, 0) + 1

        for _, row in candidates.iterrows():
            symbol = row["symbol"]
            open_price = row["open_price"]
            rvol = row.get("rvol", 0)

            if symbol not in cached_bars:
                bars_1m = get_1min_bars(client, symbol, date)
                if bars_1m.empty or len(bars_1m) < 2:
                    cached_bars[symbol] = (None, None)
                    continue
                cached_bars[symbol] = (bars_1m, _bars_to_list(bars_1m))
            bars_1m, all_bars_1m = cached_bars[symbol]
            if bars_1m is None:
                continue

            min_vol = config.GBAR_MIN_VOLUME
            if rvol >= 10:
                min_vol = max(config.GBAR_MIN_VOLUME // 3, 5000)
            elif rvol >= 5:
                min_vol = max(config.GBAR_MIN_VOLUME // 2, 10000)

            entry_slippage = getattr(config, "SLIPPAGE_ENTRY_PCT", 0.005)
            same_tier = tier_counts.get(_get_rvol_tier(rvol)[0], 1)
            stop_p, target_p, trail_act_p, trail_p = get_rvol_exit_params(rvol)
            sym_key = f"{symbol} ({date_key})"
            chart_bars = _bars_to_chart(bars_1m)
            events = chart_entries.get(sym_key, {}).get("events", [])

            # Bar-by-bar: scan for greenbar signals, enter, exit, continue scanning
            search_start = 0
            last_exit_bar = -999
            last_entry_price = 0.0
            while True:
                if daily_trades >= bt_max_daily_trades:
                    break
                if max_daily_loss > 0 and daily_loss <= -max_daily_loss:
                    print(f"  Daily loss ${daily_loss:,.2f} exceeded limit, stopping for day")
                    break
                max_entries = getattr(config, "MAX_DAILY_ENTRIES_PER_SYMBOL", 6)
                if entry_count.get(symbol, 0) >= max_entries:
                    break

                # Find next greenbar entry signal
                entry_price, entry_bar_idx, signal_type = find_greenbar_entry(
                    bars_1m, open_price, min_volume=min_vol, search_from=search_start)
                if entry_bar_idx < 0 or entry_price <= 0:
                    break

                # Cooldown: at least 1 bar after exit
                if entry_bar_idx <= last_exit_bar + 1:
                    search_start = entry_bar_idx + 1
                    continue

                entries_for_sym_pre = entry_count.get(symbol, 0)
                is_reentry = entries_for_sym_pre > 0
                entry_price_actual = round(entry_price * (1 + entry_slippage), 4)

                # RVOL-weighted position sizing
                pos_size = get_rvol_sizing(rvol, equity, same_tier_count=same_tier)
                if is_reentry:
                    pos_size *= 0.70  # re-entry at 70% of full size
                pos_size = max(config.MIN_POSITION_SIZE, pos_size)
                shares = int(pos_size / entry_price_actual)
                if shares <= 0:
                    search_start = entry_bar_idx + 1
                    continue

                remaining_list = all_bars_1m[entry_bar_idx + 1:]
                force_close_price = remaining_list[-1]["close"] if remaining_list else entry_price_actual

                result = evaluate_trade_greenbar(
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
                    exit_on_red_bar=getattr(config, "GBAR_EXIT_ON_RED_BAR", True),
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
                entry_count[symbol] = entries_for_sym_pre + 1

                events.append({"ts": entry_ts, "type": "buy", "price": entry_price_actual,
                               "label": f"BUY {shares}sh [{label}]"})
                events.append({"ts": exit_ts, "type": "sell", "price": result.exit_price,
                               "label": f"{result.exit_reason.upper()} {shares}sh"})

                search_start = exit_bar + 1
                last_exit_bar = exit_bar
                last_entry_price = entry_price_actual

            if events:
                chart_entries[sym_key] = {
                    "date": str(date_key), "bars_1m": chart_bars, "events": events,
                    "entry_price": last_entry_price,
                    "pnl": sum(t.pnl for t in all_trades if t.symbol == symbol and t.date == str(date_key)),
                    "open_price": open_price, "signal": "greenbar",
                }

    print(f"\n{'=' * 70}")
    print(f"[getgreenbar_1.0] Backtest complete. Final equity: ${equity:,.2f}")
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
