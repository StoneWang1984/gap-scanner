"""Config — stonewang_daytrade_rtg_2.0: RTG + Profit Protection + Progressive Trailing.

New rules vs rtg_1.0:
  1. Daily profit protection: when today's profit drops to 85% of max profit reached, force close all
  2. Progressive trailing stop:
     - stock profit > 5%  -> trail = 1.5%
     - stock profit > 10% -> trail = 1%
     - stock profit > 15% -> trail = 0.5%
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# Alpaca API
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER = False  # False = live account
DRY_RUN = False
ALPACA_BASE_URL = "https://api.alpaca.markets"
ALPACA_DATA_URL = "https://data.alpaca.markets"

# Data feed: SIP
DATA_FEED = "sip"
from alpaca.data.enums import DataFeed as _DF
DATA_FEED_OBJ = _DF.SIP

# ── Scanner filters ──────────────────────────────────────────────────
GAP_THRESHOLD = 0.10   # min 10% gap
GAP_MAX = 1.0          # max 100% gap
MIN_VOLUME = 10000     # min pre-market volume
MIN_DOLLAR_VOLUME = 100000
PRICE_MIN = 1.0
PRICE_MAX = 20.0

# Leveraged ETF exclusion
LEVERAGED_ETF_SUFFIXES = ("BULL", "BEAR")
LEVERAGED_ETF_PREFIXES = ()

# ── RTG candidate selection ──────────────────────────────────────────
MAX_CANDIDATES = 40  # Top 40 by RVOL — wide monitoring for full-day trading
RVOL_LOOKBACK_DAYS = 20  # 20-day average volume for RVOL calculation
RTG_ONLY = True  # Only trade RTG signals — GapGo has 34% win rate (removed)

# ── RVOL-weighted position sizing ────────────────────────────────────
# (rvol_min, equity_pct) — higher RVOL = bigger conviction = bigger size
# Concentrate on A+ setups: top traders put 50%+ on the best idea
RVOL_SIZING_TIERS = [
    (10.0, 0.50),   # RVOL > 10× → 50% of equity (A+ setup, concentrated)
    (5.0,  0.35),   # RVOL 5-10× → 35% of equity
    (0.0,  0.20),   # RVOL < 5× → 20% of equity (marginal setup)
]

# ── RVOL-adaptive exit tiers ─────────────────────────────────────────
# (rvol_min, stop_pct, target_pct, trail_activate_pct, trail_pct)
# Key insight: gap-momentum stocks have 5-10% normal intraday swings.
# 2% trail gets stopped out on first pullback, missing 80%+ of the move.
# High RVOL gap stocks need 5%+ trail to ride the opening drive.
RVOL_EXIT_TIERS = [
    (10.0, 0.07, 0.50, 0.05, 0.05),  # High RVOL: 7% stop, 50% target, trail +5%/5%
    (5.0,  0.05, 0.30, 0.04, 0.04),  # Medium: 5% stop, 30% target, trail +4%/4%
    (0.0,  0.03, 0.15, 0.03, 0.02),  # Low: 3% stop, 15% target, trail +3%/2%
]

# ── Daily profit protection (rtg_2.0) ──────────────────────────────────
DAILY_PROFIT_PROTECT_ENABLED = True   # When today's profit drops to X% of max, force close all
DAILY_PROFIT_PROTECT_RATIO = 0.85    # 85% — e.g. max profit $25, force close when profit drops to $21.25
DAILY_PROFIT_PROTECT_MIN = 5.0       # Only activate when max profit >= $5 (avoid triggering on noise)

# ── Progressive trailing stop (rtg_2.0) ────────────────────────────────
# As stock profit grows, tighten trailing stop to lock in gains
PROGRESSIVE_TRAIL_TIERS = [
    (0.15, 0.005),  # profit > 15% -> trail = 0.5%
    (0.10, 0.010),  # profit > 10% -> trail = 1%
    (0.05, 0.015),  # profit > 5%  -> trail = 1.5%
]

# ── Re-entry: NONE (Cam Connor — "the opening drive is your only edge") ──
# Backtest proof: first entry P&L +$37.70 (83% WR), ALL re-entries -$39.50 (35% WR)
# Stop-loss = setup failed. Trail-stop = move captured. Either way, you're done.
RTG_REENTRY_ALLOWED = False
RTG_REENTRY_MAX = 0              # No re-entry — the edge was the opening drive
RTG_REENTRY_SIZE_PCT = 0.50      # (unused when REENTRY_MAX=0)
REENTRY_MAX_PRICE_VS_OPEN = 1.15  # (unused)
REENTRY_MIN_PULLBACK = 0.03       # (unused)
REENTRY_COOLDOWN_SEC = 120        # (unused)

# ── Entry parameters ─────────────────────────────────────────────────
ENTRY_WINDOW_START = "09:30"  # Start at open
ENTRY_WINDOW_END = "15:59"    # Full trading day — entries allowed until 15:59

# Signal A: Red-to-Green (THE signal — 75% win rate in backtest)
RTG_VOLUME_MULT = 1.5       # Lower threshold catches earlier signals (was 2.0)
RTG_MIN_VOLUME = 30000      # Base liquidity floor — relaxed for high RVOL in live_trade.py
# RVOL-adaptive min volume: RVOL>=10 → RTG_MIN_VOLUME/3, RVOL>=5 → RTG_MIN_VOLUME/2
RTG_MIN_PRICE_GAIN = 0.0    # Min (close - open_price) / open_price for signal bar
RTG_ENTRY_AT_OPEN = True    # Enter at open_price + 0.1% instead of signal bar close (better price)

# Signal B: Gap-and-Go (DISABLED — 34% win rate, too many false breakouts)
GAPGO_MIN_FIRST_BAR_VOL = 99999999   # Effectively disabled
GAPGO_MIN_BREAKOUT_VOL = 99999999    # Effectively disabled

# ── Exit parameters (defaults — overridden by RVOL_EXIT_TIERS) ──────
RTG_STOP_PCT = 0.05           # 5% hard stop loss (default)
RTG_TARGET_PCT = 0.30         # 30% profit target (default)
RTG_TIME_LIMIT_SEC = 0          # No time limit — let trail/stop manage the trade (Cam Connor)
RTG_TRAIL_ACTIVATE_PCT = 0.03 # Activate trailing stop after +3% gain
RTG_TRAIL_PCT = 0.02          # 2% trailing stop

# ── Position sizing ──────────────────────────────────────────────────
INITIAL_CAPITAL = 377.29      # Account equity (updated 2026-08-22)
MIN_POSITION_SIZE = 40        # Min $40 per position (fractional shares)
MAX_POSITION_SIZE = 9999      # No hard cap — RVOL tiers control sizing
MAX_POSITIONS = 8             # Max 8 concurrent positions
EXCLUDE_SYMBOLS = {"AEI", "LITZ", "VOGX", "WEAV"}  # Managed by external OCO orders
MAX_DAILY_TRADES = 0          # 0 = no limit
MAX_DAILY_LOSS_PCT = 0.04     # 4% daily loss circuit breaker (tighter)
EQUITY_POSITION_RATIO = 1.0

# ── Market hours ─────────────────────────────────────────────────────
MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"
FORCE_CLOSE_TIME = "15:59"   # EOD force close

# ── Live trading ─────────────────────────────────────────────────────
USE_WEBSOCKET = True
POLL_INTERVAL = 3             # Main loop poll interval (seconds)
DRY_RUN_POLL_INTERVAL = 5

# ── Slippage model ───────────────────────────────────────────────────
SLIPPAGE_ENTRY_PCT = 0.005    # 0.5% entry slippage (market buy)
SLIPPAGE_EXIT_PCT = 0.005     # 0.5% exit slippage (market sell)
SLIPPAGE_FORCE_CLOSE_PCT = 0.01

# ── Order parameters ─────────────────────────────────────────────────
ENTRY_LIMIT_BUFFER = 0.005
STOP_LIMIT_BUFFER = 0.02
FORCE_CLOSE_LIMIT_TIMEOUT = 60

# ── Backtest ─────────────────────────────────────────────────────────
BACKTEST_DAYS = 30

# ── Version ──────────────────────────────────────────────────────────
VERSION = "stonewang_daytrade_rtg_2.0"
VERSION_SHORT = "rtg_2.0"
