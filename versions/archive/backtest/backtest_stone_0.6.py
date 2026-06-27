"""Backtesting engine — Stone 0.6: Fusion strategies combining Stone 0.4 + 0.5.

0.6.1: 0.4 entry + three-tier exit + MACD sell early exit + MACD re-entry
0.6.2: MACD entry + three-tier exit (no re-entry)
0.6.3: Dual entry (pullback OR MACD) + mixed exit + MACD re-entry
0.6.4: 0.4 entry + pure three-tier exit + MACD buy re-entry
"""

import pandas as pd
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import Adjustment, DataFeed

import config
from scanner import get_data_client, get_tradable_symbols
from strategy import (
    build_trade_plan, calc_atr, calc_stop_price, calc_position_size,
    calc_price_at_retracement, TradePlan, TradeResult, evaluate_trade_stone,
    find_reentry_point, evaluate_reentry_trade,
)
from strategy_05 import (
    calc_macd, check_macd_buy_signal, check_macd_sell_signal,
)
from strategy_06 import (
    TradeResult06, evaluate_trade_three_tier_plus_macd, evaluate_reentry_macd,
)
from backtest import get_trading_days, bulk_scan_gaps, find_entry_with_confirmation
from backtest_05 import get_5min_bars_with_warmup


def _bars_to_today_list(bars_df: pd.DataFrame, date_key) -> list[dict]:
    """Convert 5-min bar DataFrame to list of dicts, filtered to a specific date."""
    target_date = pd.Timestamp(date_key).date() if not isinstance(date_key, pd.Timestamp) else date_key.date()
    result = []
    for idx, row in bars_df.iterrows():
        ts = idx[1] if isinstance(idx, tuple) else idx
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC").tz_convert("America/New_York")
        elif str(ts.tzinfo) != "America/New_York":
            ts = ts.tz_convert("America/New_York")
        if ts.date() != target_date:
            continue
        result.append({
            "timestamp": ts,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": int(row["volume"]),
        })
    return result


def _all_closes_from_df(bars_df: pd.DataFrame) -> list[float]:
    closes = []
    for idx, row in bars_df.iterrows():
        closes.append(float(row["close"]))
    return closes


def _all_lows_from_df(bars_df: pd.DataFrame) -> list[float]:
    lows = []
    for idx, row in bars_df.iterrows():
        lows.append(float(row["low"]))
    return lows


def _today_bars_as_df(bars_df: pd.DataFrame, date_key) -> pd.DataFrame:
    """Filter warmup-inclusive DataFrame to today's bars, return as DataFrame for find_entry_with_confirmation."""
    target_date = pd.Timestamp(date_key).date() if not isinstance(date_key, pd.Timestamp) else date_key.date()
    rows = []
    for idx, row in bars_df.iterrows():
        ts = idx[1] if isinstance(idx, tuple) else idx
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC").tz_convert("America/New_York")
        elif str(ts.tzinfo) != "America/New_York":
            ts = ts.tz_convert("America/New_York")
        if ts.date() != target_date:
            continue
        rows.append({
            "timestamp": ts,
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": int(row["volume"]),
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("timestamp")


def _trade_result_to_06(result: TradeResult, variant: str = "", entry_method: str = "") -> TradeResult06:
    """Convert Stone 0.4 TradeResult to TradeResult06."""
    return TradeResult06(
        symbol=result.symbol, date=result.date,
        entry_price=result.entry_price, exit_price=result.exit_price,
        shares=result.shares, pnl=result.pnl, pnl_pct=result.pnl_pct,
        exit_reason=result.exit_reason, variant=variant,
        open_price=result.open_price, sell_target=result.sell_target,
        stop_price=result.stop_price, entry_bar_idx=result.exit_bar_idx,
        exit_bar_idx=result.exit_bar_idx, position_size=result.position_size,
        trailing_high=result.trailing_high, trade_type=result.trade_type,
        partial_sell_price=result.partial_sell_price,
        partial_sell_shares=result.partial_sell_shares,
        partial2_sell_price=result.partial2_sell_price,
        partial2_sell_shares=result.partial2_sell_shares,
        partial3_sell_price=result.partial3_sell_price,
        partial3_sell_shares=result.partial3_sell_shares,
        entry_method=entry_method,
    )


def run_backtest_06(
    variant: str = "061",
    end_date: pd.Timestamp | None = None,
    n_days: int = 5,
) -> list[TradeResult06]:
    """Run Stone 0.6 backtest for specified variant."""

    client = get_data_client()
    if end_date is None:
        end_date = pd.Timestamp.now(tz="America/New_York")

    variant_labels = {
        "061": "0.6.1: Pullback entry + three-tier + MACD sell + MACD re-entry",
        "062": "0.6.2: MACD entry + three-tier exit (no re-entry)",
        "063": "0.6.3: Dual entry (pullback OR MACD) + mixed exit + MACD re-entry",
        "064": "0.6.4: Pullback entry + pure three-tier + MACD buy re-entry",
    }
    label = variant_labels.get(variant, variant)

    print(f"\n[Stone {variant}] {label}")
    print(f"Capital: ${config.INITIAL_CAPITAL:,.0f} | Deploy: {config.EQUITY_POSITION_RATIO:.0%} | Max daily trades: {config.MAX_DAILY_TRADES}")
    print(f"MACD: {config.MACD_FAST}/{config.MACD_SLOW}/{config.MACD_SIGNAL} | Stop loss: {config.MACD_STOP_PCT:.0%}")

    symbols = get_tradable_symbols()
    print(f"Loading tradable symbols... Found {len(symbols)} tradable symbols")

    trading_days = get_trading_days(client, end_date=end_date, n_days=n_days)
    if not trading_days:
        print("No trading days found.")
        return []

    start_date = trading_days[0]
    end_d = trading_days[-1]
    print(f"Backtest period: {start_date.date()} to {end_d.date()}")

    print(f"\nBulk scanning for gaps...")
    gap_data = bulk_scan_gaps(client, trading_days, symbols)
    print(f"Found {sum(len(v) for v in gap_data.values())} gap entries across {len(gap_data)} days")

    equity = config.INITIAL_CAPITAL
    all_trades: list[TradeResult06] = []

    for date_key, candidates_df in sorted(gap_data.items()):
        if candidates_df.empty:
            continue

        deploy = calc_position_size(equity)
        pos_per_stock = min(deploy, config.MAX_POSITION_SIZE)
        max_stocks = max(config.MAX_POSITIONS_PER_DAY, int(deploy / pos_per_stock))
        candidates_df = candidates_df.head(max_stocks)

        daily_trades = 0
        print(f"\n--- {date_key} ({len(candidates_df)} candidates, equity: ${equity:,.0f}, deploy: ${deploy:,.0f}) ---")

        for _, cand in candidates_df.iterrows():
            if daily_trades >= config.MAX_DAILY_TRADES:
                break

            symbol = cand["symbol"]
            open_price = cand["open_price"]

            # Fetch 5-min bars with warmup
            bars_df = get_5min_bars_with_warmup(client, symbol, pd.Timestamp(date_key))
            if bars_df.empty:
                continue

            today_bars = _bars_to_today_list(bars_df, date_key)
            if len(today_bars) < 5:
                print(f"  {symbol}: only {len(today_bars)} 5-min bars today")
                continue

            all_closes = _all_closes_from_df(bars_df)
            all_lows = _all_lows_from_df(bars_df)
            if len(all_closes) < config.MACD_WARMUP_BARS:
                print(f"  {symbol}: only {len(all_closes)} total bars for MACD warmup")
                continue

            # Calculate MACD
            macd_line, signal_line, histogram = calc_macd(all_closes)

            warmup_count = len(all_closes) - len(today_bars)

            # Force close index
            force_close_ts = pd.Timestamp(f"{date_key} {config.FORCE_CLOSE_TIME}", tz="America/New_York")
            force_close_today = None
            for i, bar in enumerate(today_bars):
                if bar["timestamp"] >= force_close_ts:
                    force_close_today = i
                    break
            force_close_abs = (warmup_count + force_close_today) if force_close_today is not None else None

            # Force close price
            force_close_price = today_bars[-1]["close"] if today_bars else None

            # ── Variant-specific entry and evaluation ──

            if variant == "061":
                result = _run_061(
                    symbol, open_price, equity, today_bars, bars_df,
                    all_closes, all_lows, macd_line, signal_line,
                    warmup_count, date_key, force_close_price, force_close_abs,
                )

            elif variant == "062":
                result = _run_062(
                    symbol, open_price, equity, today_bars,
                    all_closes, all_lows, macd_line, signal_line,
                    warmup_count, date_key, force_close_price, force_close_abs,
                )

            elif variant == "063":
                result = _run_063(
                    symbol, open_price, equity, today_bars, bars_df,
                    all_closes, all_lows, macd_line, signal_line,
                    warmup_count, date_key, force_close_price, force_close_abs,
                )

            elif variant == "064":
                result = _run_064(
                    symbol, open_price, equity, today_bars, bars_df,
                    all_closes, all_lows, macd_line, signal_line,
                    warmup_count, date_key, force_close_price, force_close_abs,
                )

            else:
                continue

            if result is None:
                continue

            # Print trade
            entry_time = today_bars[result.entry_bar_idx]["timestamp"].strftime("%H:%M") if 0 <= result.entry_bar_idx < len(today_bars) else "?"
            exit_time = today_bars[result.exit_bar_idx]["timestamp"].strftime("%H:%M") if 0 <= result.exit_bar_idx < len(today_bars) else "?"
            extra = ""
            if result.partial_sell_shares > 0:
                extra += f", 1/4@${result.partial_sell_price:.4f}"
            if result.partial2_sell_shares > 0:
                extra += f", 1/3@${result.partial2_sell_price:.4f}"
            if result.partial3_sell_shares > 0:
                extra += f", 1/3@${result.partial3_sell_price:.4f}"
            if result.trailing_high > result.entry_price:
                extra += f", high=${result.trailing_high:.4f}"

            tag = f"[{result.trade_type}]"
            print(f"  {symbol} {tag} entry=${result.entry_price:.4f} ({entry_time}) exit=${result.exit_price:.4f} ({exit_time}) "
                  f"({result.exit_reason}), P&L=${result.pnl:,.2f} ({result.pnl_pct:.2%}){extra}")

            all_trades.append(result)
            equity += result.pnl
            daily_trades += 1

            # ── Re-entry for 0.6.1, 0.6.3 and 0.6.4 ──
            if variant in ("061", "063") and result.trade_type == "first" and result.exit_reason != "force_close":
                reentry_results = _find_and_run_macd_reentry(
                    result, symbol, open_price, equity, today_bars,
                    all_closes, all_lows, macd_line, signal_line,
                    warmup_count, force_close_abs, daily_trades,
                )
            elif variant == "064" and result.trade_type == "first" and result.exit_reason != "force_close":
                reentry_results = _find_and_run_macd_reentry_064(
                    result, symbol, open_price, equity, today_bars,
                    all_closes, all_lows, macd_line, signal_line,
                    warmup_count, force_close_abs, daily_trades,
                )
            else:
                reentry_results = []

            for re_result in reentry_results:
                re_entry_time = today_bars[re_result.entry_bar_idx]["timestamp"].strftime("%H:%M") if 0 <= re_result.entry_bar_idx < len(today_bars) else "?"
                re_exit_time = today_bars[re_result.exit_bar_idx]["timestamp"].strftime("%H:%M") if 0 <= re_result.exit_bar_idx < len(today_bars) else "?"
                print(f"  {symbol} [Re] entry={re_result.entry_price:.4f} ({re_entry_time}) exit={re_result.exit_price:.4f} ({re_exit_time}) "
                      f"({re_result.exit_reason}), P&L={re_result.pnl:,.2f} ({re_result.pnl_pct:.2%})")
                all_trades.append(re_result)
                equity += re_result.pnl
                daily_trades += 1
                if daily_trades >= config.MAX_DAILY_TRADES:
                    break

    total_pnl = sum(t.pnl for t in all_trades)
    final_equity = config.INITIAL_CAPITAL + total_pnl
    print(f"\n{'=' * 60}")
    print(f"[Stone {variant}] Backtest complete. Final equity: ${final_equity:,.2f}")
    print(f"Total trades: {len(all_trades)} | Return: {total_pnl / config.INITIAL_CAPITAL:.2%}")

    return all_trades


# ── 0.6.1: Pullback entry + three-tier + MACD sell + MACD re-entry ──

def _run_061(
    symbol, open_price, equity, today_bars, bars_df,
    all_closes, all_lows, macd_line, signal_line,
    warmup_count, date_key, force_close_price, force_close_abs,
) -> TradeResult06 | None:
    """0.6.1 first trade: pullback+confirmation entry, three-tier + MACD sell exit."""
    today_bars_df = _today_bars_as_df(bars_df, date_key)
    if today_bars_df.empty or len(today_bars_df) < 2:
        return None

    pullback, entry_bar_idx, confirmed = find_entry_with_confirmation(today_bars_df, open_price)
    if not confirmed or pullback <= 0:
        return None

    # Entry time check (before 10:00)
    entry_ts = today_bars[entry_bar_idx]["timestamp"]
    cutoff = pd.Timestamp(f"{date_key} 10:00", tz="America/New_York")
    if entry_ts > cutoff:
        return None

    # ATR
    bars_for_atr = today_bars[:entry_bar_idx + 1]
    atr = calc_atr(bars_for_atr, period=14)

    # Trade plan
    pos_size = min(calc_position_size(equity), config.MAX_POSITION_SIZE)
    plan = build_trade_plan(symbol, open_price, pullback, atr, pos_size)
    if plan.shares <= 0:
        return None

    # Evaluate
    bars_after = today_bars[entry_bar_idx + 1:]
    result = evaluate_trade_three_tier_plus_macd(
        plan, bars_after, macd_line, signal_line,
        warmup_count + entry_bar_idx, force_close_price,
    )
    result.date = str(date_key)
    result.variant = "061"
    result.entry_bar_idx = entry_bar_idx
    result.exit_bar_idx = entry_bar_idx + 1 + result.exit_bar_idx
    result.entry_method = "pullback"
    return result


# ── 0.6.2: MACD entry + three-tier exit (no re-entry) ──

def _run_062(
    symbol, open_price, equity, today_bars,
    all_closes, all_lows, macd_line, signal_line,
    warmup_count, date_key, force_close_price, force_close_abs,
) -> TradeResult06 | None:
    """0.6.2 first trade: MACD buy signal entry, three-tier exit."""
    buy_abs_idx = check_macd_buy_signal(macd_line, signal_line, warmup_count)
    if buy_abs_idx < 0:
        return None

    buy_today_idx = buy_abs_idx - warmup_count
    if buy_today_idx < 0:
        return None

    # Entry time check
    entry_ts = today_bars[buy_today_idx]["timestamp"]
    cutoff = pd.Timestamp(f"{date_key} {config.MACD_ENTRY_CUTOFF_TIME}", tz="America/New_York")
    if entry_ts > cutoff:
        return None

    entry_price = today_bars[buy_today_idx]["close"]

    # ATR
    bars_for_atr = today_bars[:buy_today_idx + 1]
    atr = calc_atr(bars_for_atr, period=14)

    # Trade plan with MACD entry price
    pos_size = min(calc_position_size(equity), config.MAX_POSITION_SIZE)
    plan = build_trade_plan(symbol, open_price, entry_price, atr, pos_size)
    if plan.shares <= 0:
        return None

    # Evaluate with standard three-tier exit (no MACD sell early exit)
    bars_after = today_bars[buy_today_idx + 1:]
    result_04 = evaluate_trade_stone(plan, bars_after, force_close_price)

    # Convert to TradeResult06
    result = _trade_result_to_06(result_04, variant="062", entry_method="macd_buy")
    result.date = str(date_key)
    result.entry_bar_idx = buy_today_idx
    result.exit_bar_idx = buy_today_idx + 1 + result_04.exit_bar_idx
    result.macd_at_entry = float(macd_line[buy_abs_idx]) if buy_abs_idx < len(macd_line) and macd_line[buy_abs_idx] is not None else 0.0
    result.signal_at_entry = float(signal_line[buy_abs_idx]) if buy_abs_idx < len(signal_line) and signal_line[buy_abs_idx] is not None else 0.0
    return result


# ── 0.6.3: Dual entry (pullback OR MACD) + mixed exit + MACD re-entry ──

def _run_063(
    symbol, open_price, equity, today_bars, bars_df,
    all_closes, all_lows, macd_line, signal_line,
    warmup_count, date_key, force_close_price, force_close_abs,
) -> TradeResult06 | None:
    """0.6.3 first trade: whichever entry comes first (pullback or MACD buy), three-tier + MACD sell exit."""
    # Find pullback entry
    pullback_entry_idx = -1
    pullback_price = 0.0
    today_bars_df = _today_bars_as_df(bars_df, date_key)
    if not today_bars_df.empty and len(today_bars_df) >= 2:
        pb, pb_idx, confirmed = find_entry_with_confirmation(today_bars_df, open_price)
        if confirmed and pb > 0:
            pullback_cutoff = pd.Timestamp(f"{date_key} 10:00", tz="America/New_York")
            if today_bars[pb_idx]["timestamp"] <= pullback_cutoff:
                pullback_entry_idx = pb_idx
                pullback_price = pb

    # Find MACD buy entry
    macd_entry_idx = -1
    buy_abs_idx = check_macd_buy_signal(macd_line, signal_line, warmup_count)
    if buy_abs_idx >= 0:
        macd_today_idx = buy_abs_idx - warmup_count
        if macd_today_idx >= 0:
            macd_cutoff = pd.Timestamp(f"{date_key} {config.MACD_ENTRY_CUTOFF_TIME}", tz="America/New_York")
            if today_bars[macd_today_idx]["timestamp"] <= macd_cutoff:
                macd_entry_idx = macd_today_idx

    # Pick whichever comes first
    entry_bar_idx = -1
    entry_price = 0.0
    entry_method = ""

    if pullback_entry_idx >= 0 and macd_entry_idx >= 0:
        if pullback_entry_idx <= macd_entry_idx:
            entry_bar_idx = pullback_entry_idx
            entry_price = pullback_price
            entry_method = "pullback"
        else:
            entry_bar_idx = macd_entry_idx
            entry_price = today_bars[macd_entry_idx]["close"]
            entry_method = "macd_buy"
    elif pullback_entry_idx >= 0:
        entry_bar_idx = pullback_entry_idx
        entry_price = pullback_price
        entry_method = "pullback"
    elif macd_entry_idx >= 0:
        entry_bar_idx = macd_entry_idx
        entry_price = today_bars[macd_entry_idx]["close"]
        entry_method = "macd_buy"
    else:
        return None

    # ATR
    bars_for_atr = today_bars[:entry_bar_idx + 1]
    atr = calc_atr(bars_for_atr, period=14)

    # Trade plan
    pos_size = min(calc_position_size(equity), config.MAX_POSITION_SIZE)
    plan = build_trade_plan(symbol, open_price, entry_price, atr, pos_size)
    if plan.shares <= 0:
        return None

    # Evaluate with three-tier + MACD sell early exit
    bars_after = today_bars[entry_bar_idx + 1:]
    entry_abs_idx = warmup_count + entry_bar_idx
    result = evaluate_trade_three_tier_plus_macd(
        plan, bars_after, macd_line, signal_line,
        entry_abs_idx, force_close_price,
    )
    result.date = str(date_key)
    result.variant = "063"
    result.entry_bar_idx = entry_bar_idx
    result.exit_bar_idx = entry_bar_idx + 1 + result.exit_bar_idx
    result.entry_method = entry_method
    return result


# ── MACD re-entry (shared by 0.6.1 and 0.6.3) ──

def _find_and_run_macd_reentry(
    first_result: TradeResult06,
    symbol: str, open_price: float, equity: float, today_bars: list[dict],
    all_closes: list[float], all_lows: list[float],
    macd_line: list[float | None], signal_line: list[float | None],
    warmup_count: int, force_close_abs: int | None,
    daily_trades: int,
) -> list[TradeResult06]:
    """Find MACD buy signal re-entry after first trade exits and evaluate."""
    results = []
    exit_bar_today = first_result.exit_bar_idx
    search_start_abs = warmup_count + exit_bar_today + 1

    reentry_abs_idx = check_macd_buy_signal(macd_line, signal_line, search_start_abs)
    if reentry_abs_idx < 0:
        return results

    reentry_today_idx = reentry_abs_idx - warmup_count
    if reentry_today_idx < 0 or reentry_today_idx >= len(today_bars):
        return results

    # Entry cutoff for re-entry
    reentry_ts = today_bars[reentry_today_idx]["timestamp"]
    reentry_cutoff = pd.Timestamp(f"{first_result.date} {config.MACD_ENTRY_CUTOFF_TIME}", tz="America/New_York")
    if reentry_ts > reentry_cutoff:
        return results

    reentry_price = today_bars[reentry_today_idx]["close"]
    pos_size = min(calc_position_size(equity), config.MAX_POSITION_SIZE)
    reentry_shares = int(pos_size / reentry_price)
    if reentry_shares <= 0:
        return results

    stop_price = round(reentry_price * (1 - config.REENTRY_STOP_PCT), 4)

    reentry_result = evaluate_reentry_macd(
        symbol=symbol, open_price=open_price, shares=reentry_shares,
        all_closes=all_closes, all_lows=all_lows,
        macd_line=macd_line, signal_line=signal_line,
        entry_bar_abs_idx=reentry_abs_idx,
        stop_price=stop_price,
        force_close_abs_idx=force_close_abs,
    )
    reentry_result.date = first_result.date
    reentry_result.variant = first_result.variant
    reentry_result.entry_bar_idx = reentry_today_idx
    reentry_result.exit_bar_idx = reentry_result.exit_bar_idx - warmup_count

    results.append(reentry_result)
    return results


# ── 0.6.4: Pullback entry + pure three-tier exit + MACD buy re-entry ──

def _run_064(
    symbol, open_price, equity, today_bars, bars_df,
    all_closes, all_lows, macd_line, signal_line,
    warmup_count, date_key, force_close_price, force_close_abs,
) -> TradeResult06 | None:
    """0.6.4 first trade: pullback+confirmation entry, pure 0.4 three-tier exit (no MACD early exit)."""
    today_bars_df = _today_bars_as_df(bars_df, date_key)
    if today_bars_df.empty or len(today_bars_df) < 2:
        return None

    pullback, entry_bar_idx, confirmed = find_entry_with_confirmation(today_bars_df, open_price)
    if not confirmed or pullback <= 0:
        return None

    # Entry time check (before 10:00)
    entry_ts = today_bars[entry_bar_idx]["timestamp"]
    cutoff = pd.Timestamp(f"{date_key} 10:00", tz="America/New_York")
    if entry_ts > cutoff:
        return None

    # ATR
    bars_for_atr = today_bars[:entry_bar_idx + 1]
    atr = calc_atr(bars_for_atr, period=14)

    # Trade plan
    pos_size = min(calc_position_size(equity), config.MAX_POSITION_SIZE)
    plan = build_trade_plan(symbol, open_price, pullback, atr, pos_size)
    if plan.shares <= 0:
        return None

    # Evaluate with pure 0.4 three-tier exit (no MACD sell early exit)
    bars_after = today_bars[entry_bar_idx + 1:]
    result_04 = evaluate_trade_stone(plan, bars_after, force_close_price)

    # Convert to TradeResult06
    result = _trade_result_to_06(result_04, variant="064", entry_method="pullback")
    result.date = str(date_key)
    result.entry_bar_idx = entry_bar_idx
    result.exit_bar_idx = entry_bar_idx + 1 + result_04.exit_bar_idx
    return result


def _find_and_run_macd_reentry_064(
    first_result: TradeResult06,
    symbol: str, open_price: float, equity: float, today_bars: list[dict],
    all_closes: list[float], all_lows: list[float],
    macd_line: list[float | None], signal_line: list[float | None],
    warmup_count: int, force_close_abs: int | None,
    daily_trades: int,
) -> list[TradeResult06]:
    """MACD re-entry for 0.6.4: MACD buy signal + 0.4 re-entry evaluation rules (multi-round)."""
    results = []
    prev_high = first_result.trailing_high
    current_exit_today = first_result.exit_bar_idx
    current_equity = equity

    while daily_trades + len(results) < config.MAX_DAILY_TRADES:
        search_start_abs = warmup_count + current_exit_today + 1
        reentry_abs_idx = check_macd_buy_signal(macd_line, signal_line, search_start_abs)
        if reentry_abs_idx < 0:
            break

        reentry_today_idx = reentry_abs_idx - warmup_count
        if reentry_today_idx < 0 or reentry_today_idx >= len(today_bars):
            break

        # Entry cutoff check
        reentry_ts = today_bars[reentry_today_idx]["timestamp"]
        reentry_cutoff = pd.Timestamp(f"{first_result.date} {config.MACD_ENTRY_CUTOFF_TIME}", tz="America/New_York")
        if reentry_ts > reentry_cutoff:
            break

        # Update prev_high from bars between last exit and re-entry
        for i in range(current_exit_today + 1, reentry_today_idx):
            if i < len(today_bars) and today_bars[i]["high"] > prev_high:
                prev_high = today_bars[i]["high"]

        reentry_price = today_bars[reentry_today_idx]["close"]

        # Pullback stop check
        if prev_high > 0 and (prev_high - reentry_price) / prev_high > config.PULLBACK_STOP_THRESHOLD:
            break

        # Position sizing
        pos_size_re = min(calc_position_size(current_equity), config.MAX_POSITION_SIZE)
        reentry_shares = int(pos_size_re / reentry_price)
        if reentry_shares <= 0:
            break

        # Evaluate re-entry using 0.4's re-entry rules
        reentry_bars_after = today_bars[reentry_today_idx + 1:]
        reentry_force_close = reentry_bars_after[-1]["close"] if reentry_bars_after else None

        reentry_result_04 = evaluate_reentry_trade(
            entry_price=reentry_price,
            prev_high=prev_high,
            shares=reentry_shares,
            symbol=symbol,
            open_price=open_price,
            bars_after_entry=reentry_bars_after,
            force_close_price=reentry_force_close,
        )

        # Convert to TradeResult06
        reentry_result = _trade_result_to_06(reentry_result_04, variant="064", entry_method="macd_reentry")
        reentry_result.date = first_result.date
        reentry_result.entry_bar_idx = reentry_today_idx
        reentry_result.exit_bar_idx = reentry_today_idx + 1 + reentry_result_04.exit_bar_idx

        results.append(reentry_result)
        current_equity += reentry_result_04.pnl

        # Prepare for next re-entry
        if reentry_result_04.exit_reason == "reentry_force_close":
            break

        current_exit_today = reentry_today_idx + 1 + reentry_result_04.exit_bar_idx
        prev_high = max(prev_high, reentry_result_04.trailing_high)

        if current_exit_today >= len(today_bars) - 2:
            break

    return results
