"""Backtesting engine — rossway_daytrade_0.1: simple OCO exit, fixed reward/risk 2.5:1."""

import json
import os
import re

import pandas as pd
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import Adjustment, DataFeed

import config
from scanner import get_data_client, get_tradable_symbols
from strategy import calc_atr

# ── Leveraged ETF filter ──
_LEV_PATTERN = re.compile(r'(2X|3X|BULL|BEAR)$', re.IGNORECASE)
_LEV_SUFFIXES = ("BULL", "BEAR")
_LEV_PREFIXES = (
    "TQQQ", "SQQQ", "UPRO", "SPXU", "TNA", "TZA",
    "MSTU", "MSTZ", "CONL", "NAIL", "WEBL", "FNGU",
    "FNGD", "SOXL", "SOXS", "TECL", "TECS", "UDOW",
    "SDOW", "UMDD", "SMDD", "TQQ", "SQQ", "YINN",
    "YANG", "CURE", "LABD", "LABU", "DRN", "DRV",
    "DGP", "DGZ", "BOIL", "KOLD", "NUGT", "DUST",
    "JNUG", "JDST", "GLL", "UGL",
)


def is_leveraged_etf(symbol: str) -> bool:
    if _LEV_PATTERN.search(symbol):
        return True
    if any(symbol.endswith(s) for s in _LEV_SUFFIXES):
        return True
    if any(symbol.startswith(p) for p in _LEV_PREFIXES):
        return True
    return False


# ── Stop/Target (tiered by price) ──
def calc_stop_and_target(entry_price: float) -> tuple[float, float]:
    tiers = getattr(config, "STOP_TIERS", None)
    if tiers:
        for min_p, max_p, stop_pct, target_pct in tiers:
            if min_p <= entry_price < max_p:
                stop_price = round(entry_price * (1 - stop_pct), 2)
                target_price = round(entry_price * (1 + target_pct), 2)
                return stop_price, target_price
    # Fallback for prices outside tiers
    stop_price = round(entry_price * 0.97, 2)
    target_price = round(entry_price * 1.03, 2)
    return stop_price, target_price


# ── Data helpers ──
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
    """Bulk scan gaps for all trading days. Returns {date_key: DataFrame}."""
    start = trading_days[0] - pd.Timedelta(days=7)
    end = trading_days[-1] + pd.Timedelta(days=1)
    all_dates_set = {d.date() for d in trading_days}

    batch_size = 500
    symbol_data = {}

    total_batches = (len(symbols) + batch_size - 1) // batch_size
    for batch_idx in range(total_batches):
        batch = symbols[batch_idx * batch_size : (batch_idx + 1) * batch_size]
        if batch_idx % 10 == 0:
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
                    idx = sym_df.index[i]
                    ts = idx if not isinstance(idx, tuple) else idx[1]
                    date_key = pd.Timestamp(ts).date()
                    if date_key not in all_dates_set:
                        continue
                    prev_close = float(prev["close"])
                    open_price = float(curr["open"])
                    if prev_close <= 0:
                        continue
                    gap_pct = (open_price - prev_close) / prev_close
                    if gap_pct >= config.GAP_THRESHOLD:
                        if symbol not in symbol_data:
                            symbol_data[symbol] = []
                        symbol_data[symbol].append({
                            "date": date_key, "symbol": symbol,
                            "prev_close": prev_close, "open_price": open_price,
                            "gap_pct": gap_pct, "prev_volume": int(prev["volume"]),
                        })
            except Exception:
                continue

    # Group by date
    result = {}
    for symbol, entries in symbol_data.items():
        for entry in entries:
            dk = entry["date"]
            if dk not in result:
                result[dk] = []
            result[dk].append(entry)

    # Convert to DataFrames
    final = {}
    for dk, entries in result.items():
        final[dk] = pd.DataFrame(entries).sort_values("gap_pct", ascending=False)
    return final


def _filter_rth(df):
    """Keep only regular trading hours bars (9:30-16:00 ET)."""
    if df.empty:
        return df
    keep = []
    for i in range(len(df)):
        idx = df.index[i]
        ts = pd.Timestamp(idx if not isinstance(idx, tuple) else idx[1])
        if ts.tzinfo is None:
            ts = ts.tz_localize('UTC')
        ts_et = ts.tz_convert('America/New_York')
        if ts_et.hour == 9 and ts_et.minute >= 30:
            keep.append(i)
        elif ts_et.hour >= 10 and ts_et.hour < 16:
            keep.append(i)
        elif ts_et.hour == 16 and ts_et.minute == 0:
            keep.append(i)
    return df.iloc[keep] if keep else df.iloc[:0]


def get_1min_bars(client, symbol, date):
    start = date - pd.Timedelta(days=1)
    end = date + pd.Timedelta(days=1)
    request = StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=start, end=end, adjustment=Adjustment.RAW,
        feed=getattr(config, "DATA_FEED_OBJ", DataFeed.IEX),
    )
    try:
        bars = client.get_stock_bars(request)
        return _filter_rth(bars.df)
    except Exception:
        return pd.DataFrame()


def get_5min_bars(client, symbol, date):
    start = date - pd.Timedelta(days=1)
    end = date + pd.Timedelta(days=1)
    request = StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=start, end=end, adjustment=Adjustment.RAW,
        feed=getattr(config, "DATA_FEED_OBJ", DataFeed.IEX),
    )
    try:
        bars = client.get_stock_bars(request)
        return _filter_rth(bars.df)
    except Exception:
        return pd.DataFrame()


# ── Entry detection: 3-bar confirmation ──
def find_entry_3bar(bars_1m, open_price, is_reentry=False):
    """3-bar pullback: bottom bar (low < open) + 3 confirm bars (low>bottom, close>bottom, >=1 bullish).
    is_reentry=True: find any local pullback (not necessarily below open_price)."""
    if bars_1m.empty or len(bars_1m) < 4:
        return 0, -1, False

    if is_reentry:
        pullback_idx = 0
        pullback_price = bars_1m.iloc[0]["low"]
        for i in range(1, len(bars_1m)):
            if bars_1m.iloc[i]["low"] < pullback_price:
                pullback_idx = i
                pullback_price = bars_1m.iloc[i]["low"]
        if pullback_idx >= len(bars_1m) - 3:
            return 0, -1, False
    else:
        pullback_idx = -1
        pullback_price = 0.0
        for i in range(len(bars_1m)):
            if bars_1m.iloc[i]["low"] < open_price:
                pullback_idx = i
                pullback_price = bars_1m.iloc[i]["low"]
                break
        if pullback_idx < 0:
            return 0, -1, False

    if not config.ENTRY_CONFIRMATION:
        return pullback_price, pullback_idx, True

    confirm_count = 0
    bullish_count = 0
    last_confirm_idx = -1
    for i in range(pullback_idx + 1, len(bars_1m)):
        bar_low = bars_1m.iloc[i]["low"]
        bar_close = bars_1m.iloc[i]["close"]
        bar_open = bars_1m.iloc[i]["open"]

        if is_reentry:
            if bar_low < pullback_price:
                pullback_idx = i
                pullback_price = bar_low
                confirm_count = 0
                bullish_count = 0
                last_confirm_idx = -1
                continue
        else:
            if bar_low < open_price and bar_low < pullback_price:
                pullback_idx = i
                pullback_price = bar_low
                confirm_count = 0
                bullish_count = 0
                last_confirm_idx = -1
                continue

        if bar_low <= pullback_price or bar_close <= pullback_price:
            continue

        confirm_count += 1
        last_confirm_idx = i
        if bar_close > bar_open:
            bullish_count += 1

        if confirm_count >= 3 and bullish_count >= 1:
            return pullback_price, last_confirm_idx, True

    return 0, -1, False


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
            ts = ts.tz_localize('UTC')
        ts = ts.tz_convert('America/New_York')
        result.append({
            "high": bar["high"], "low": bar["low"], "close": bar["close"],
            "open": bar["open"], "volume": int(bar["volume"]),
            "timestamp": ts,
        })
    return result


# ── Simple OCO simulation ──
def simulate_oco_trade(entry_price, stop_price, target_price, bars_list, shares=1):
    """Simulate a simple OCO trade: exit at first bar where high>=target or low<=stop.
    Returns (exit_price, exit_reason, exit_bar_idx, highest)."""
    highest = entry_price
    for i, bar in enumerate(bars_list):
        high = bar["high"]
        low = bar["low"]
        close = bar["close"]
        if high > highest:
            highest = high

        # Check stop first (protect capital)
        if low <= stop_price:
            # Bar gaps through both — check which is closer
            if high >= target_price:
                # Both triggered in same bar — if open > target, take profit; else stop
                if bar["open"] >= target_price:
                    return target_price, "take_profit", i, highest
            return stop_price, "stop_loss", i, highest

        # Check target
        if high >= target_price:
            return target_price, "take_profit", i, highest

    # No exit triggered — force close at last bar
    if bars_list:
        return bars_list[-1]["close"], "force_close", len(bars_list) - 1, highest
    return entry_price, "no_data", -1, highest


# ── Main backtest ──
def run_backtest(end_date=None, n_days=30):
    client = get_data_client()
    if end_date is None:
        end_date = pd.Timestamp.now(tz="America/New_York")

    trading_days = get_trading_days(client, end_date, n_days)
    if not trading_days:
        print("No trading days found.")
        return

    print(f"[rossway 0.1] Backtesting {len(trading_days)} days: {trading_days[0].date()} to {trading_days[-1].date()}")
    tiers = getattr(config, "STOP_TIERS", None)
    if tiers:
        print(f"Stop/Target: tiered ({len(tiers)} price tiers) | Max positions: {config.MAX_POSITIONS}")
    else:
        print(f"Stop/Target: default 3%/3% | Max positions: {config.MAX_POSITIONS}")

    print("\nLoading tradable symbols...")
    symbols = get_tradable_symbols()
    symbols = [s for s in symbols if not is_leveraged_etf(s)]
    print(f"After filter: {len(symbols)} symbols")

    print("\nBulk scanning for gaps...")
    gap_data = bulk_scan_gaps(client, trading_days, symbols)
    total_candidates = sum(len(v) for v in gap_data.values())
    print(f"Found {total_candidates} gap entries across {len(gap_data)} days")

    equity = getattr(config, "INITIAL_CAPITAL", 500)
    all_trades = []
    chart_entries = {}

    for date in trading_days:
        date_key = date.date()
        if date_key not in gap_data or gap_data[date_key].empty:
            continue

        candidates = gap_data[date_key]
        max_positions = config.MAX_POSITIONS
        pos_per_stock = min(equity / max(len(candidates), 1), config.MAX_POSITION_SIZE)
        pos_per_stock = max(pos_per_stock, config.MIN_POSITION_SIZE)

        print(f"\n--- {date_key} ({len(candidates)} candidates, equity: ${equity:,.0f}, "
              f"per-stock: ${pos_per_stock:,.0f}) ---")

        daily_trades = 0
        daily_pnl = 0.0
        daily_loss_limit = equity * config.MAX_DAILY_LOSS_PCT
        active_positions = 0
        entry_checked = set()
        tp_exited_syms = set()  # symbols that exited via take_profit (eligible for re-entry with local pullback)

        for _, row in candidates.iterrows():
            if active_positions >= max_positions:
                break
            if daily_pnl <= -daily_loss_limit:
                print(f"  Circuit breaker: daily PnL ${daily_pnl:.2f}")
                break

            symbol = row["symbol"]
            open_price = row["open_price"]

            bars_1m = get_1min_bars(client, symbol, date)
            bars_5m = get_5min_bars(client, symbol, date)
            if bars_5m.empty or len(bars_5m) < 3:
                continue

            # Entry detection
            if bars_1m.empty or len(bars_1m) < 4:
                continue
            is_reentry = symbol in tp_exited_syms
            pullback, entry_idx_1m, confirmed = find_entry_3bar(bars_1m, open_price, is_reentry=is_reentry)
            if not confirmed or pullback <= 0:
                print(f"  {symbol}: no confirmed entry, skipping")
                continue

            # Entry time check (re-entry has no time restriction)
            entry_ts_1m = bars_1m.index[entry_idx_1m]
            if isinstance(entry_ts_1m, tuple):
                entry_ts_1m = entry_ts_1m[1]
            entry_ts = pd.Timestamp(entry_ts_1m)
            if entry_ts.tzinfo is None:
                entry_ts = entry_ts.tz_localize('UTC')
            entry_ts = entry_ts.tz_convert('America/New_York')
            cutoff = pd.Timestamp(f"{date_key} 10:00", tz="America/New_York")
            if not is_reentry and entry_ts > cutoff:
                print(f"  {symbol}: entry after 10:00, skipping")
                continue

            # Entry price with slippage
            gap_pct = (open_price - pullback) / open_price if open_price > 0 else 0
            slippage = min(0.05, 0.01 + gap_pct * 0.15)
            entry_price = round(pullback * (1 + slippage), 2)

            stop_price, target_price = calc_stop_and_target(entry_price)

            if entry_price <= 0:
                continue
            shares = int(pos_per_stock / entry_price)
            if shares <= 0:
                continue

            # Map entry to 5-min bar index for remaining bars
            entry_minute = entry_ts.minute
            entry_bucket = (entry_minute // 5) * 5
            entry_bar_5m = -1
            for j in range(len(bars_5m)):
                idx = bars_5m.index[j]
                ts = pd.Timestamp(idx if not isinstance(idx, tuple) else idx[1])
                if ts.tzinfo is None:
                    ts = ts.tz_localize('UTC').tz_convert('America/New_York')
                else:
                    ts = ts.tz_convert('America/New_York')
                if ts.minute // 5 * 5 == entry_bucket and ts.hour == entry_ts.hour:
                    entry_bar_5m = j
                    break
            if entry_bar_5m < 0:
                entry_bar_5m = 0

            all_bars_5m = _bars_to_list(bars_5m)
            remaining = all_bars_5m[entry_bar_5m + 1:]

            # Simulate OCO trade
            exit_price, exit_reason, exit0, highest = simulate_oco_trade(
                entry_price, stop_price, target_price, remaining, shares)

            pnl = (exit_price - entry_price) * shares
            pnl_pct = (exit_price - entry_price) / entry_price

            tag = "[RE]" if is_reentry else "[1st]"
            print(f"  {symbol} {tag} entry=${entry_price:.4f} exit=${exit_price:.4f} ({exit_reason}), "
                  f"P&L=${pnl:,.2f} ({pnl_pct:.2%}), stop=${stop_price:.2f} target=${target_price:.2f}, high=${highest:.4f}")

            all_trades.append({
                "symbol": symbol, "date": str(date_key),
                "entry": entry_price, "exit": exit_price,
                "shares": shares, "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 4),
                "exit_reason": exit_reason,
                "stop": stop_price, "target": target_price,
                "highest": highest,
            })

            equity += pnl
            daily_pnl += pnl
            daily_trades += 1
            active_positions += 1

            # After exit, slot freed — allow re-entry for same symbol
            # (In rossway, re-entry is just another entry when slot is available)
            if exit_reason == "stop_loss":
                entry_checked.add(symbol)  # Stop loss → no re-entry for this symbol today
            else:
                # Take profit → slot freed, can re-enter with local pullback
                active_positions -= 1
                tp_exited_syms.add(symbol)

        # End of day: force close any remaining positions
        if active_positions > 0:
            active_positions = 0

    # Summary
    print("\n" + "=" * 60)
    total_pnl = sum(t["pnl"] for t in all_trades)
    wins = [t for t in all_trades if t["pnl"] > 0]
    losses = [t for t in all_trades if t["pnl"] <= 0]
    print(f"[rossway 0.1] Backtest complete. Final equity: ${equity:,.2f}")
    print(f"Total trades: {len(all_trades)} | Wins: {len(wins)} | Losses: {len(losses)}")
    if all_trades:
        win_rate = len(wins) / len(all_trades) * 100
        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        print(f"Win rate: {win_rate:.1f}% | Avg win: ${avg_win:.2f} | Avg loss: ${avg_loss:.2f}")
    print(f"Total P&L: ${total_pnl:,.2f}")
    print("=" * 60)

    # Save chart data
    chart_path = os.path.join(os.path.dirname(__file__), "chart_data.json")
    try:
        with open(chart_path, "w") as f:
            json.dump({"trades": all_trades, "final_equity": equity}, f, indent=2)
        print(f"Results saved to {chart_path}")
    except Exception:
        pass

    return all_trades


if __name__ == "__main__":
    # Load config from same directory
    import importlib.util, sys
    _dir = os.path.dirname(os.path.abspath(__file__))
    _spec = importlib.util.spec_from_file_location("config", os.path.join(_dir, "config.py"))
    config = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(config)
    sys.modules["config"] = config

    run_backtest(n_days=5)
