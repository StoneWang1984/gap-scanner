#!/usr/bin/env python3
"""Compare Stone 0.4.8 vs 0.4.5 backtests on today's data."""

import sys
import os
import importlib.util
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

end_date = pd.Timestamp.now(tz="America/New_York")
today_str = end_date.strftime("%Y-%m-%d")
print(f"=== Backtest Date: {today_str} ===\n")

# ═══════════════════════════════════════════════════════════════
# Stone 0.4.5
# ═══════════════════════════════════════════════════════════════
print("=" * 70)
print("STONE 0.4.5 (Three-tier partial sells + re-entry cutoff)")
print("=" * 70)

# Patch config for 0.4.5 specific values
config.TRAILING_STOP_PCT_75 = 0.03
config.TRAILING_STOP_PCT_1125 = 0.04
config.TRAILING_STOP_PCT_150 = 0.05
config.REENTRY_CUTOFF_TIME = "13:00"
config.SLIPPAGE_ENTRY_PCT = 0.005
config.SLIPPAGE_STOP_PCT = 0.02
config.SLIPPAGE_TRAILING_PCT = 0.01
config.SLIPPAGE_TARGET_PCT = 0.003
config.SLIPPAGE_FORCE_CLOSE_PCT = 0.01
config.SLIPPAGE_REENTRY_STOP_PCT = 0.025

spec_045 = importlib.util.spec_from_file_location(
    "backtest_045", os.path.join(os.path.dirname(__file__), "versions", "backtest_stone_0.4.5.py")
)
mod_045 = importlib.util.module_from_spec(spec_045)
spec_045.loader.exec_module(mod_045)

trades_045, slip_045 = mod_045.run_backtest_045(end_date=end_date, n_days=1)

# ═══════════════════════════════════════════════════════════════
# Stone 0.4.8
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("STONE 0.4.8 (Pure trailing stop — no partial sells)")
print("=" * 70)

# Patch config for 0.4.8 specific values
config.TRAILING_STOP_PCT = 0.01               # 1% trailing
config.TRAILING_ACTIVATION_RETRACEMENT = 0.75  # activate at 75% retracement
config.TRAILING_STOP_PCT_75 = 0.03
config.TRAILING_STOP_PCT_1125 = 0.04
config.TRAILING_STOP_PCT_150 = 0.05
config.REENTRY_CUTOFF_TIME = "13:00"
config.SLIPPAGE_ENTRY_PCT = 0.005
config.SLIPPAGE_STOP_PCT = 0.02
config.SLIPPAGE_TRAILING_PCT = 0.01
config.SLIPPAGE_TARGET_PCT = 0.003
config.SLIPPAGE_FORCE_CLOSE_PCT = 0.01
config.SLIPPAGE_REENTRY_STOP_PCT = 0.025

spec_048 = importlib.util.spec_from_file_location(
    "backtest_048", os.path.join(os.path.dirname(__file__), "versions", "backtest_stone_0.4.8.py")
)
mod_048 = importlib.util.module_from_spec(spec_048)
spec_048.loader.exec_module(mod_048)

trades_048, slip_048 = mod_048.run_backtest_048(end_date=end_date, n_days=1)

# ═══════════════════════════════════════════════════════════════
# Comparison Summary
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print(f"COMPARISON SUMMARY — {today_str}")
print("=" * 70)

def summarize(trades, label):
    if not trades:
        print(f"\n{label}: No trades")
        return
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    total_pnl = sum(t.pnl for t in trades)
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.pnl for t in losses) / len(losses) if losses else 0
    print(f"\n{label}:")
    print(f"  Trades: {len(trades)} | Wins: {len(wins)} | Losses: {len(losses)}")
    print(f"  Win rate: {win_rate:.1f}%")
    print(f"  Total P&L: ${total_pnl:,.2f}")
    print(f"  Avg win: ${avg_win:,.2f} | Avg loss: ${avg_loss:,.2f}")
    for t in trades:
        tag = "[1st]" if t.trade_type == "first" else "[Re]"
        print(f"    {tag} {t.symbol} entry=${t.entry_price:.4f} exit=${t.exit_price:.4f} "
              f"({t.exit_reason}) P&L=${t.pnl:,.2f} ({t.pnl_pct:.2%})")

summarize(trades_045, "Stone 0.4.5")
summarize(trades_048, "Stone 0.4.8")
