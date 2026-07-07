"""Backtesting engine — Stone 0.4.8: pure trailing stop (no partial sells).

Changes over 0.4.5:
- Remove three-tier partial profit targets (1/4@75%, 1/3@112.5%, 1/3@150%)
- Replace with pure trailing: activate at 75% retracement, trail all shares at 5%
- All shares exit together via trailing stop, stop loss, or force close
"""

import re
import pandas as pd

import config
from scanner import get_data_client, get_tradable_symbols
from strategy import (
    build_trade_plan, evaluate_reentry_trade,
    calc_atr, calc_stop_price, calc_price_at_retracement, calc_position_size,
    find_reentry_point, TradeResult, TradePlan,
)
from backtest import (
    get_trading_days, bulk_scan_gaps, get_5min_bars,
    find_entry_with_confirmation, _bars_to_list,
)

# ── Slippage parameters (unchanged from 0.4.5) ────────────────────────
SLIPPAGE_ENTRY_PCT = getattr(config, "SLIPPAGE_ENTRY_PCT", 0.005)
SLIPPAGE_STOP_PCT = getattr(config, "SLIPPAGE_STOP_PCT", 0.02)
SLIPPAGE_TRAILING_PCT = getattr(config, "SLIPPAGE_TRAILING_PCT", 0.01)
SLIPPAGE_TARGET_PCT = getattr(config, "SLIPPAGE_TARGET_PCT", 0.003)
SLIPPAGE_FORCE_CLOSE_PCT = getattr(config, "SLIPPAGE_FORCE_CLOSE_PCT", 0.01)
SLIPPAGE_REENTRY_STOP_PCT = getattr(config, "SLIPPAGE_REENTRY_STOP_PCT", 0.025)

# ── 0.4.8: Pure trailing parameters ──────────────────────────────────
TRAILING_ACTIVATION = getattr(config, "TRAILING_ACTIVATION_RETRACEMENT", 0.75)
TRAILING_PCT = getattr(config, "TRAILING_STOP_PCT", 0.05)

# ── Leveraged ETF detection (unchanged from 0.4.5) ────────────────────
_LEV_PATTERN = re.compile(r'(2X|3X|BULL|BEAR)$', re.IGNORECASE)
_LEV_SUFFIXES = ("U", "L")


def is_leveraged_etf(symbol: str) -> bool:
    if _LEV_PATTERN.search(symbol):
        return True
    if len(symbol) > 3 and symbol[-1] in _LEV_SUFFIXES:
        return True
    if any(symbol.startswith(p) for p in ("TQQQ", "SQQQ", "UPRO", "SPXU", "TNA", "TZA",
                                           "MSTU", "MSTZ", "CONL", "NAIL", "WEBL", "FNGU",
                                           "FNGD", "SOXL", "SOXS", "TECL", "TECS", "UDOW",
                                           "SDOW", "UMDD", "SMDD", "TQQ", "SQQ", "YINN",
                                           "YANG", "CURE", "LABD", "LABU", "DRN", "DRV",
                                           "DGP", "DGZ", "BOIL", "KOLD", "NUGT", "DUST",
                                           "JNUG", "JDST", "GLL", "UGL")):
        return True
    return False


def _exit_slippage_pct(exit_reason: str) -> float:
    if "stop_loss" in exit_reason:
        return SLIPPAGE_STOP_PCT
    if "reentry_stop" in exit_reason:
        return SLIPPAGE_REENTRY_STOP_PCT
    if "trailing_stop" in exit_reason or "reentry_trailing" in exit_reason:
        return SLIPPAGE_TRAILING_PCT
    if "force_close" in exit_reason or "reentry_force_close" in exit_reason:
        return SLIPPAGE_FORCE_CLOSE_PCT
    if "late_close" in exit_reason or "reentry_early_exit" in exit_reason:
        return SLIPPAGE_TRAILING_PCT
    return SLIPPAGE_TARGET_PCT


def apply_slippage(result: TradeResult) -> tuple[TradeResult, float]:
    original_pnl = result.pnl
    adj_entry = result.entry_price * (1 + SLIPPAGE_ENTRY_PCT)
    exit_slip = _exit_slippage_pct(result.exit_reason)
    adj_exit = result.exit_price * (1 - exit_slip)
    remaining = result.shares
    adj_pnl = (adj_exit - adj_entry) * remaining
    adj_pnl_pct = adj_pnl / (adj_entry * result.shares) if adj_entry > 0 else 0
    result.entry_price = round(adj_entry, 2)
    result.exit_price = round(adj_exit, 2)
    result.pnl = round(adj_pnl, 2)
    result.pnl_pct = round(adj_pnl_pct, 4)
    result.position_size = round(adj_entry * result.shares, 2)
    slippage_cost = round(original_pnl - adj_pnl, 2)
    return result, slippage_cost


# ── 0.4.8: Pure trailing stop evaluation ─────────────────────────────

def evaluate_trade_pure_trailing(
    symbol: str,
    entry_price: float,
    open_price: float,
    stop_price: float,
    shares: int,
    atr: float,
    bars_after_entry: list[dict],
    force_close_price: float | None = None,
    activation_retracement: float = TRAILING_ACTIVATION,
    trail_pct: float = TRAILING_PCT,
) -> TradeResult:
    """0.4.8 first trade: no partial sells, pure trailing stop after activation."""
    activation_price = round(entry_price + activation_retracement * (open_price - entry_price), 2)
    highest = entry_price
    trailing_active = False

    def _make_result(reason, exit_price, bi):
        pnl = (exit_price - entry_price) * shares
        pnl_pct = pnl / (entry_price * shares) if entry_price > 0 else 0
        return TradeResult(
            symbol=symbol, date=str(bar.get("timestamp", pd.Timestamp.now()).date()) if bi >= 0 else "",
            entry_price=entry_price, exit_price=exit_price, shares=shares,
            pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4), exit_reason=reason,
            open_price=open_price, sell_target=activation_price,
            stop_price=stop_price,
            trailing_high=highest, trailing_exit_price=exit_price, atr=atr,
            exit_bar_idx=bi, position_size=entry_price * shares,
            trade_type="first",
        )

    import pandas as pd

    for bi, bar in enumerate(bars_after_entry):
        bh, bl = bar["high"], bar["low"]
        if bh > highest:
            highest = bh

        # Stop loss (always active)
        if bl <= stop_price:
            return _make_result("stop_loss", stop_price, bi)

        # Check activation: price reaches 75% retracement
        if not trailing_active and bh >= activation_price:
            trailing_active = True

        # Trailing stop (after activation)
        if trailing_active:
            tsp = round(highest * (1 - trail_pct), 2)
            tsp = max(tsp, entry_price)  # breakeven protection
            if bl <= tsp:
                return _make_result("trailing_stop", tsp, bi)

    # Force close at end of day
    if force_close_price is not None:
        exit_price = force_close_price
    else:
        exit_price = bars_after_entry[-1]["close"] if bars_after_entry else entry_price

    # If trailing never activated, breakeven exit
    if not trailing_active:
        exit_price = entry_price

    return _make_result("force_close", exit_price, len(bars_after_entry) - 1)


# ── Backtest runner ──────────────────────────────────────────────────

def run_backtest_048(end_date=None, n_days=config.BACKTEST_DAYS) -> tuple[list[TradeResult], float]:
    client = get_data_client()
    if end_date is None:
        end_date = pd.Timestamp.now(tz="America/New_York")

    trading_days = get_trading_days(client, end_date, n_days)
    if not trading_days:
        print("No trading days found.")
        return [], 0.0

    reentry_cutoff = getattr(config, "REENTRY_CUTOFF_TIME", "13:00")

    print(f"[Stone 0.4.8] Backtesting {len(trading_days)} trading days: "
          f"{trading_days[0].date()} to {trading_days[-1].date()}")
    print(f"Capital: ${config.INITIAL_CAPITAL:,.0f} | Deploy: {config.EQUITY_POSITION_RATIO:.0%} | "
          f"Per-stock cap: ${config.MAX_POSITION_SIZE:,.0f} | Max daily trades: {config.MAX_DAILY_TRADES}")
    print(f"First trade: PURE TRAILING {TRAILING_PCT:.0%} (activate at {TRAILING_ACTIVATION:.0%} retracement)")
    print(f"Re-entry cutoff: {reentry_cutoff} EST | Leveraged ETF filter: ON")
    print(f"Slippage: entry +{SLIPPAGE_ENTRY_PCT:.1%} | stop -{SLIPPAGE_STOP_PCT:.1%} | "
          f"trailing -{SLIPPAGE_TRAILING_PCT:.1%} | force_close -{SLIPPAGE_FORCE_CLOSE_PCT:.1%}")

    print("\nLoading tradable symbols...")
    symbols = get_tradable_symbols()
    print(f"Found {len(symbols)} tradable symbols")

    # Filter out leveraged ETFs
    symbols = [s for s in symbols if not is_leveraged_etf(s)]
    print(f"After leveraged ETF filter: {len(symbols)} symbols")

    print("\nBulk scanning for gaps...")
    gap_data = bulk_scan_gaps(client, trading_days, symbols)
    total_candidates = sum(len(v) for v in gap_data.values())
    print(f"Found {total_candidates} gap entries across {len(gap_data)} days")

    all_trades: list[TradeResult] = []
    total_slippage = 0.0
    equity = config.INITIAL_CAPITAL

    for date in trading_days:
        date_key = date.date()
        if date_key not in gap_data or gap_data[date_key].empty:
            continue

        deployable = calc_position_size(equity)
        pos_per_stock = min(deployable, config.MAX_POSITION_SIZE)
        max_stocks_today = max(config.MAX_POSITIONS_PER_DAY, int(deployable / pos_per_stock))
        candidates = gap_data[date_key].head(max_stocks_today)

        print(f"\n--- {date_key} ({len(gap_data[date_key])} candidates, equity: ${equity:,.0f}, "
              f"deploy: ${deployable:,.0f}, per-stock: ${pos_per_stock:,.0f}) ---")

        daily_trades = 0
        daily_stopped = False

        for _, row in candidates.iterrows():
            if daily_trades >= config.MAX_DAILY_TRADES or daily_stopped:
                break

            symbol = row["symbol"]
            open_price = row["open_price"]

            if is_leveraged_etf(symbol):
                print(f"  {symbol}: leveraged ETF, skipping")
                continue

            bars_5m = get_5min_bars(client, symbol, date)
            if bars_5m.empty or len(bars_5m) < 3:
                continue

            all_bars = _bars_to_list(bars_5m)

            # ========== FIRST TRADE ==========
            pullback, entry_bar_idx, confirmed = find_entry_with_confirmation(bars_5m, open_price)
            if not confirmed or pullback <= 0:
                print(f"  {symbol}: no confirmed entry, skipping")
                continue

            idx_val = bars_5m.index[entry_bar_idx]
            if isinstance(idx_val, tuple):
                entry_ts = pd.Timestamp(idx_val[1])
            else:
                entry_ts = pd.Timestamp(idx_val)
            if entry_ts.tzinfo is None:
                entry_ts = entry_ts.tz_localize('UTC').tz_convert('America/New_York')
            cutoff = pd.Timestamp(f"{date_key} 10:00", tz="America/New_York")
            if entry_ts > cutoff:
                print(f"  {symbol}: entry after 10:00, skipping")
                continue

            bars_for_atr = []
            for j in range(min(entry_bar_idx + 1, len(bars_5m))):
                b = bars_5m.iloc[j]
                bars_for_atr.append({"high": b["high"], "low": b["low"], "close": b["close"]})
            atr = calc_atr(bars_for_atr, period=14)

            stop_price = calc_stop_price(pullback, atr)

            pos_size = min(calc_position_size(equity), config.MAX_POSITION_SIZE)
            shares = int(pos_size / pullback)
            if shares <= 0:
                continue

            remaining_list = all_bars[entry_bar_idx + 1:]
            force_close_price = remaining_list[-1]["close"] if remaining_list else None

            result = evaluate_trade_pure_trailing(
                symbol=symbol,
                entry_price=pullback,
                open_price=open_price,
                stop_price=stop_price,
                shares=shares,
                atr=atr,
                bars_after_entry=remaining_list,
                force_close_price=force_close_price,
            )

            result, slip = apply_slippage(result)
            total_slippage += slip

            result.date = str(date_key)
            result.open_price = open_price
            result.stop_price = stop_price

            type_tag = "[1st]"
            extra = ""
            if result.trailing_high > result.entry_price:
                extra += f", high=${result.trailing_high:.4f}"
            extra += f", slip=${slip:.2f}"
            print(f"  {symbol} {type_tag} entry=${result.entry_price:.4f} exit=${result.exit_price:.4f} "
                  f"({result.exit_reason}), P&L=${result.pnl:,.2f} ({result.pnl_pct:.2%}){extra}")

            all_trades.append(result)
            equity += result.pnl
            daily_trades += 1

            # ========== RE-ENTRY TRADES ==========
            exit_bar_in_all = entry_bar_idx + 1 + result.exit_bar_idx
            bars_after_exit = all_bars[exit_bar_in_all + 1:]

            if result.exit_reason == "force_close" or not bars_after_exit:
                continue

            reentry_cutoff_ts = pd.Timestamp(f"{date_key} {reentry_cutoff}", tz="America/New_York")

            reentry_round = 1
            while daily_trades < config.MAX_DAILY_TRADES and not daily_stopped:
                if not bars_after_exit or len(bars_after_exit) < 3:
                    break

                first_bar_ts = bars_after_exit[0].get("timestamp")
                if first_bar_ts:
                    if hasattr(first_bar_ts, "tzinfo") and first_bar_ts.tzinfo is None:
                        first_bar_ts = pd.Timestamp(first_bar_ts).tz_localize('UTC').tz_convert('America/New_York')
                    if pd.Timestamp(first_bar_ts) > reentry_cutoff_ts:
                        print(f"  {symbol}: re-entry after {reentry_cutoff}, skipping")
                        break

                reentry_price, prev_high, reentry_idx, reentry_confirmed = find_reentry_point(
                    bars_after_exit, open_price
                )

                if not reentry_confirmed or reentry_price <= 0:
                    break

                if prev_high > 0 and (prev_high - reentry_price) / prev_high > config.PULLBACK_STOP_THRESHOLD:
                    print(f"  {symbol}: significant pullback from ${prev_high:.4f}, stopping day")
                    daily_stopped = True
                    break

                reentry_stop = round(reentry_price * (1 - config.REENTRY_STOP_PCT), 2)
                reentry_target = round(reentry_price + config.REENTRY_PROFIT_RETRACEMENT * (prev_high - reentry_price), 2)

                pos_size_re = min(calc_position_size(equity), config.MAX_POSITION_SIZE)
                reentry_shares = int(pos_size_re / reentry_price)
                if reentry_shares <= 0:
                    break

                reentry_remaining = bars_after_exit[reentry_idx + 1:]
                reentry_force_close = reentry_remaining[-1]["close"] if reentry_remaining else None

                reentry_result = evaluate_reentry_trade(
                    entry_price=reentry_price,
                    prev_high=prev_high,
                    shares=reentry_shares,
                    symbol=symbol,
                    open_price=open_price,
                    bars_after_entry=reentry_remaining,
                    force_close_price=reentry_force_close,
                )

                reentry_result, re_slip = apply_slippage(reentry_result)
                total_slippage += re_slip

                reentry_result.date = str(date_key)

                type_tag = f"[Re{reentry_round}]"
                re_extra = ""
                if reentry_result.partial_sell_shares > 0:
                    re_extra += f", 1/3@${reentry_result.partial_sell_price:.4f}"
                if reentry_result.trailing_high > reentry_result.entry_price:
                    re_extra += f", high=${reentry_result.trailing_high:.4f}"
                re_extra += f", slip=${re_slip:.2f}"
                print(f"  {symbol} {type_tag} entry=${reentry_result.entry_price:.4f} exit=${reentry_result.exit_price:.4f} "
                      f"({reentry_result.exit_reason}), P&L=${reentry_result.pnl:,.2f} ({reentry_result.pnl_pct:.2%})"
                      f", prev_high=${prev_high:.4f}, target=${reentry_target:.4f}{re_extra}")

                all_trades.append(reentry_result)
                equity += reentry_result.pnl
                daily_trades += 1
                reentry_round += 1

                reentry_exit_bar = reentry_idx + 1 + reentry_result.exit_bar_idx
                bars_after_exit = bars_after_exit[reentry_exit_bar + 1:]

                if reentry_result.exit_reason == "reentry_force_close":
                    break

    print(f"\n{'='*60}")
    print(f"[Stone 0.4.8] Backtest complete.")
    print(f"Final equity: ${equity:,.2f} | Total slippage cost: ${total_slippage:,.2f}")
    print(f"Total trades: {len(all_trades)}")
    return all_trades, total_slippage
