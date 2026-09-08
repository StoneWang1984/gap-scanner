"""6-tier backtest comparison: 3-tier (15/25/35) vs 6-tier (5/10/15/20/25/35)."""

import pandas as pd
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import Adjustment, DataFeed

import config
from scanner import get_data_client, get_tradable_symbols
from strategy import (
    calc_atr, calc_stop_price, calc_price_at_retracement, calc_position_size,
    find_reentry_point,
    TradeResult,
)
from backtest import (
    is_leveraged_etf, bulk_scan_gaps, get_5min_bars,
    get_trading_days, _bars_to_list, find_entry_with_confirmation,
)

# ═══════════════════════════════════════════════════════════════
# 6-tier config
# ═══════════════════════════════════════════════════════════════
TIER6_RETRACEMENTS = [0.25, 0.50, 0.75, 1.00, 1.25, 1.50]
TIER6_CAPS =         [0.05, 0.10, 0.15, 0.20, 0.25, 0.35]
TIER6_SELL_RATIOS =  [1/8,  1/8,  1/8,  1/8,  1/8,  1/8]   # 6×1/8 = 75%
TIER6_TRAIL_PCTS =   [0.02, 0.025, 0.03, 0.035, 0.04, 0.05]

# 3-tier config (current)
TIER3_RETRACEMENTS = [0.75, 1.125, 1.50]
TIER3_CAPS =         [0.15, 0.25,  0.35]
TIER3_SELL_RATIOS =  [0.25, 1/3,   1/3]
TIER3_TRAIL_PCTS =   [0.03, 0.04,  0.05]


def evaluate_tiered_trade(
    symbol: str, open_price: float, entry_price: float, shares: int,
    stop_price: float, targets: list[float], sell_ratios: list[float],
    trail_pcts: list[float], bars_after_entry: list[dict],
    force_close_price: float | None = None, time_limit_bars: int = 0,
) -> dict:
    """Generic N-tier trade evaluation."""
    n_tiers = len(targets)
    reached = [False] * n_tiers
    sold = [False] * n_tiers
    partial_prices = [0.0] * n_tiers
    partial_shares = [0] * n_tiers
    remaining = shares
    highest = entry_price
    time_limit_active = False

    for bi, bar in enumerate(bars_after_entry):
        bh, bl = bar["high"], bar["low"]
        if bh > highest:
            highest = bh

        if bl <= stop_price:
            return _make_tier_result("stop_loss", stop_price, bi, entry_price, shares,
                                     sold, partial_prices, partial_shares, remaining, highest, symbol, open_price, bars_after_entry)

        if time_limit_bars > 0 and not reached[0] and bi >= time_limit_bars:
            time_limit_active = True
        if time_limit_active and bh >= entry_price:
            return _make_tier_result("time_limit_exit", max(bh, entry_price), bi, entry_price, shares,
                                     sold, partial_prices, partial_shares, remaining, highest, symbol, open_price, bars_after_entry)

        # Check targets from highest to lowest (skip-gaps handling)
        for ti in range(n_tiers - 1, -1, -1):
            if not reached[ti] and bh >= targets[ti]:
                # Mark all lower tiers as reached too
                for tj in range(ti + 1):
                    reached[tj] = True
                # Sell at this tier if not already sold
                if not sold[ti]:
                    sold[ti] = True
                    partial_prices[ti] = targets[ti]
                    # For skip-gaps: higher tiers that were never hit get 0 shares
                    sell_shares = int(shares * sell_ratios[ti]) if remaining > int(shares * sell_ratios[ti]) else remaining
                    partial_shares[ti] = sell_shares
                    remaining -= sell_shares
                # Handle lower unsold tiers in a skip-gap
                for tj in range(ti):
                    if not sold[tj]:
                        sold[tj] = True
                        partial_prices[tj] = targets[ti]  # sold at this price
                        sell_shares = int(shares * sell_ratios[tj])
                        if sell_shares > remaining:
                            sell_shares = remaining
                        partial_shares[tj] = sell_shares
                        remaining -= sell_shares

        # Trailing stop
        if reached[0]:
            # Find highest reached tier
            highest_tier = 0
            for ti in range(n_tiers - 1, -1, -1):
                if reached[ti]:
                    highest_tier = ti
                    break
            pct = trail_pcts[highest_tier]
            tsp = round(highest * (1 - pct), 2)
            tsp = max(tsp, entry_price)
            if bl <= tsp:
                suffix = f"_{int(TIER6_RETRACEMENTS[highest_tier] * 100) if n_tiers == 6 else ''}"
                return _make_tier_result(f"trailing_stop{suffix}", tsp, bi, entry_price, shares,
                                         sold, partial_prices, partial_shares, remaining, highest, symbol, open_price, bars_after_entry)

    if force_close_price is not None:
        exit_price = force_close_price
    else:
        exit_price = bars_after_entry[-1]["close"] if bars_after_entry else entry_price
    if not any(reached):
        exit_price = entry_price

    return _make_tier_result("force_close", exit_price, len(bars_after_entry) - 1, entry_price, shares,
                             sold, partial_prices, partial_shares, remaining, highest, symbol, open_price, bars_after_entry)


def _make_tier_result(reason, exit_price, bi, entry_price, shares,
                      sold, partial_prices, partial_shares, remaining, highest,
                      symbol, open_price, bars_after_entry):
    pnl = 0.0
    for i in range(len(sold)):
        if sold[i]:
            pnl += (partial_prices[i] - entry_price) * partial_shares[i]
    pnl += (exit_price - entry_price) * remaining
    pnl_pct = pnl / (entry_price * shares) if entry_price > 0 else 0

    mode = "capped"  # simplified

    import pandas as pd
    date_str = ""
    if bi >= 0 and bars_after_entry:
        bar = bars_after_entry[bi]
        date_str = str(bar.get("timestamp", pd.Timestamp.now()).date())

    return {
        "symbol": symbol, "date": date_str,
        "entry_price": entry_price, "exit_price": exit_price,
        "shares": shares, "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 4),
        "exit_reason": reason, "open_price": open_price,
        "highest": highest, "remaining": remaining,
        "mode": mode, "sold_shares": sum(partial_shares),
        "partial_prices": partial_prices, "partial_shares_list": partial_shares,
    }


def run_comparison(n_days=60):
    client = get_data_client()
    end_date = pd.Timestamp.now(tz="America/New_York")
    trading_days = get_trading_days(client, end_date, n_days)
    if not trading_days:
        print("No trading days found.")
        return

    print(f"Loading tradable symbols...")
    symbols = get_tradable_symbols()
    symbols = [s for s in symbols if not is_leveraged_etf(s)]
    print(f"Found {len(symbols)} symbols after filter")

    print("Bulk scanning for gaps...")
    gap_data = bulk_scan_gaps(client, trading_days, symbols)
    total_candidates = sum(len(v) for v in gap_data.values())
    print(f"Found {total_candidates} gap entries across {len(gap_data)} days\n")

    results_3tier = []
    results_6tier = []
    equity_3 = equity_6 = config.INITIAL_CAPITAL

    for date in trading_days:
        date_key = date.date()
        if date_key not in gap_data or gap_data[date_key].empty:
            continue

        n_cands = len(gap_data[date_key])
        max_stocks = min(config.MAX_POSITIONS_PER_DAY, n_cands)
        pos_per_stock_3 = min(equity_3 / max_stocks, config.MAX_POSITION_SIZE) if max_stocks > 0 else equity_3
        pos_per_stock_6 = min(equity_6 / max_stocks, config.MAX_POSITION_SIZE) if max_stocks > 0 else equity_6

        candidates = gap_data[date_key].head(max_stocks)
        daily_pnl_3 = daily_pnl_6 = 0.0
        daily_trades_3 = daily_trades_6 = 0
        max_daily_loss_3 = equity_3 * getattr(config, "MAX_DAILY_LOSS_PCT", 0.05)
        max_daily_loss_6 = equity_6 * getattr(config, "MAX_DAILY_LOSS_PCT", 0.05)

        for _, row in candidates.iterrows():
            if daily_trades_3 >= config.MAX_DAILY_TRADES and daily_trades_6 >= config.MAX_DAILY_TRADES:
                break
            if daily_pnl_3 <= -max_daily_loss_3 and daily_pnl_6 <= -max_daily_loss_6:
                break

            symbol = row["symbol"]
            open_price = row["open_price"]

            bars_5m = get_5min_bars(client, symbol, date)
            if bars_5m.empty or len(bars_5m) < 3:
                continue

            all_bars = _bars_to_list(bars_5m)

            pullback, entry_bar_idx, confirmed = find_entry_with_confirmation(bars_5m, open_price)
            if not confirmed or pullback <= 0 or pullback >= open_price:
                continue

            # Entry time check
            idx_val = bars_5m.index[entry_bar_idx]
            if isinstance(idx_val, tuple):
                entry_ts = pd.Timestamp(idx_val[1])
            else:
                entry_ts = pd.Timestamp(idx_val)
            if entry_ts.tzinfo is None:
                entry_ts = entry_ts.tz_localize('UTC')
            entry_ts = entry_ts.tz_convert('America/New_York')
            cutoff = pd.Timestamp(f"{date_key} 10:00", tz="America/New_York")
            if entry_ts > cutoff:
                continue

            # ATR
            bars_for_atr = []
            for j in range(min(entry_bar_idx + 1, len(bars_5m))):
                b = bars_5m.iloc[j]
                bars_for_atr.append({"high": b["high"], "low": b["low"], "close": b["close"]})
            atr = calc_atr(bars_for_atr, period=14)

            stop_price = calc_stop_price(pullback, atr)
            stop_max_pct = getattr(config, "STOP_LOSS_MAX_PCT", 0)
            if stop_max_pct > 0:
                stop_price = max(stop_price, round(pullback * (1 - stop_max_pct), 2))

            remaining_list = all_bars[entry_bar_idx + 1:]
            force_close_price = remaining_list[-1]["close"] if remaining_list else None
            time_limit = getattr(config, "FIRST_TRADE_TIME_LIMIT_BARS", 0)

            # ── 3-tier trade ──
            if daily_trades_3 < config.MAX_DAILY_TRADES and daily_pnl_3 > -max_daily_loss_3:
                shares_3 = int(pos_per_stock_3 / pullback) if pullback > 0 else 0
                if shares_3 > 0:
                    targets_3 = []
                    any_capped_3 = False
                    for i in range(3):
                        ret = calc_price_at_retracement(pullback, open_price, TIER3_RETRACEMENTS[i])
                        cap_price = round(pullback * (1 + TIER3_CAPS[i]), 2)
                        t = min(ret, cap_price)
                        if t < ret:
                            any_capped_3 = True
                        targets_3.append(t)

                    r3 = evaluate_tiered_trade(
                        symbol, open_price, pullback, shares_3, stop_price,
                        targets_3, TIER3_SELL_RATIOS, TIER3_TRAIL_PCTS,
                        remaining_list, force_close_price, time_limit,
                    )
                    r3["mode"] = "capped" if any_capped_3 else "retracement"
                    r3["tier"] = 3
                    results_3tier.append(r3)
                    equity_3 += r3["pnl"]
                    daily_pnl_3 += r3["pnl"]
                    daily_trades_3 += 1

            # ── 6-tier trade ──
            if daily_trades_6 < config.MAX_DAILY_TRADES and daily_pnl_6 > -max_daily_loss_6:
                shares_6 = int(pos_per_stock_6 / pullback) if pullback > 0 else 0
                if shares_6 > 0:
                    targets_6 = []
                    any_capped_6 = False
                    for i in range(6):
                        ret = calc_price_at_retracement(pullback, open_price, TIER6_RETRACEMENTS[i])
                        cap_price = round(pullback * (1 + TIER6_CAPS[i]), 2)
                        t = min(ret, cap_price)
                        if t < ret:
                            any_capped_6 = True
                        targets_6.append(t)

                    r6 = evaluate_tiered_trade(
                        symbol, open_price, pullback, shares_6, stop_price,
                        targets_6, TIER6_SELL_RATIOS, TIER6_TRAIL_PCTS,
                        remaining_list, force_close_price, time_limit,
                    )
                    r6["mode"] = "capped" if any_capped_6 else "retracement"
                    r6["tier"] = 6
                    results_6tier.append(r6)
                    equity_6 += r6["pnl"]
                    daily_pnl_6 += r6["pnl"]
                    daily_trades_6 += 1

    # ── Summary ──
    print("\n" + "=" * 70)
    print("COMPARISON: 3-tier (15/25/35%) vs 6-tier (5/10/15/20/25/35%)")
    print("=" * 70)

    for label, results, eq in [("3-tier", results_3tier, equity_3), ("6-tier", results_6tier, equity_6)]:
        total = len(results)
        wins = sum(1 for r in results if r["pnl"] > 0)
        losses = sum(1 for r in results if r["pnl"] < 0)
        breakeven = sum(1 for r in results if r["pnl"] == 0)
        total_pnl = sum(r["pnl"] for r in results)
        stop_losses = [r for r in results if r["exit_reason"] == "stop_loss"]
        sl_pnl = sum(r["pnl"] for r in stop_losses)
        avg_win = sum(r["pnl"] for r in results if r["pnl"] > 0) / max(wins, 1)
        avg_loss = sum(r["pnl"] for r in results if r["pnl"] < 0) / max(losses, 1)
        capped = sum(1 for r in results if r["mode"] == "capped")

        print(f"\n{label}:")
        print(f"  Final equity: ${eq:,.2f} (start: ${config.INITIAL_CAPITAL:,})")
        print(f"  Total P&L: ${total_pnl:,.2f}")
        print(f"  Trades: {total} | Wins: {wins} | Losses: {losses} | BE: {breakeven}")
        print(f"  Win rate: {wins/total*100:.1f}%" if total else "  No trades")
        print(f"  Avg win: ${avg_win:,.2f} | Avg loss: ${avg_loss:,.2f}")
        print(f"  Stop losses: {len(stop_losses)} (${sl_pnl:,.2f})")
        print(f"  Capped trades: {capped}")

    # ── Detailed comparison of capped trades ──
    print("\n--- 6-tier: trades that hit different tiers ---")
    tier_hit_counts = [0] * 6
    for r in results_6tier:
        for i in range(6):
            if r["partial_shares_list"][i] > 0:
                tier_hit_counts[i] += 1
    for i in range(6):
        print(f"  Tier {i+1} ({int(TIER6_RETRACEMENTS[i]*100)}% retracement, cap {TIER6_CAPS[i]*100:.0f}%): hit {tier_hit_counts[i]} trades")


if __name__ == "__main__":
    run_comparison(n_days=60)
