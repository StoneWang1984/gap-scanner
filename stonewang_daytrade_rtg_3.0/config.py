"""Config — stonewang_daytrade_rtg_3.0: Event-driven All-in RTG.

Fundamental design:
  1. Event-driven: scan -> buy -> monitor -> sell -> immediately scan again
  2. Single position: at most ONE position at a time
  3. All-in: buy with ALL available capital (no RVOL tiered sizing)
  4. Exit: green-to-red (1 red bar) / RVOL-adaptive stop / target
  5. Entry: RTG signal (close > open, 1.5x volume) + Breakout
  6. Daily profit protection: same as rtg_2.0
  7. Full-day trading until force close at 15:59
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
MAX_CANDIDATES = 40
RVOL_LOOKBACK_DAYS = 20
RTG_ONLY = True

# ── Cycle parameters (rtg_3.0) ───────────────────────────────────────
SCAN_INTERVAL_SEC = 30           # Re-scan quickly after no signal (30s)
MIN_RVOL_TO_TRADE = 3.0         # Minimum RVOL to qualify as "best stock"
ALL_IN_BP_RATIO = 0.95          # Buy with 95% of available buying power
MAX_POSITIONS = 8                # Up to 8 concurrent positions (same as rtg_2.0)
MAX_ENTRY_ATTEMPTS = 8          # Try top 8 candidates per scan

# ── Exit: green-to-red only + RVOL-adaptive stop (same as rtg_2.0) ────
EXIT_ON_RED_BAR = False           # Disabled — only green_to_red exits
EXIT_ON_GREEN_TO_RED = True       # Green-to-red transition (1 red bar) → sell
GREEN_TO_RED_CONSEC_BARS = 1      # 1 red bar = exit immediately
EXIT_ON_THREE_GREEN = False       # Disabled — let winners run

# RVOL-adaptive stop/target (same as rtg_2.0)
RVOL_EXIT_TIERS = [
    (10.0, 0.07, 0.50),  # High RVOL: 7% stop, 50% target
    (5.0,  0.05, 0.30),  # Medium: 5% stop, 30% target
    (0.0,  0.03, 0.15),  # Low: 3% stop, 15% target
]

# Fallback defaults (used when RVOL tier not matched)
STOP_PCT = 0.05
TARGET_PCT = 0.30

# ── Entry restriction: disabled (same as rtg_2.0) ────────────────────
MAX_GREEN_BARS_TO_ENTER = 999    # Effectively no restriction

# Legacy trail params (disabled — using G2R instead)
TRAIL_PCT = 0.0
TRAIL_ACTIVATE_PCT = 0.0

# ── Intraday breakout signal ────────────────────────────────────────
BREAKOUT_ENABLED = True           # Enable intraday breakout signal
BREAKOUT_MIN_BARS = 5             # Min bars before checking (need history for day_high)
BREAKOUT_VOLUME_MULT = 1.5        # Same as RTG (same as rtg_2.0)
BREAKOUT_ENTRY_AT_CLOSE = True    # Enter at breakout close price (not open)

# ── Daily profit protection (same as rtg_2.0) ────────────────────────
DAILY_PROFIT_PROTECT_ENABLED = True
DAILY_PROFIT_PROTECT_RATIO = 0.85    # When profit drops to 85% of max, force close all
DAILY_PROFIT_PROTECT_MIN = 5.0       # Only activate when max profit >= $5

# ── No progressive trailing (rtg_3.0 uses bar exit, not trailing) ────
PROGRESSIVE_TRAIL_TIERS = []

# ── RVOL sizing (same as rtg_2.0) ──────────────────
RVOL_SIZING_TIERS = [
    (10.0, 0.50),   # RVOL > 10x → 50% of equity
    (5.0,  0.35),   # RVOL 5-10x → 35% of equity
    (0.0,  0.20),   # RVOL < 5x → 20% of equity
]

# ── Re-entry: NONE ───────────────────────────────────────────────────
RTG_REENTRY_ALLOWED = False
RTG_REENTRY_MAX = 0
RTG_REENTRY_SIZE_PCT = 0.50
REENTRY_MAX_PRICE_VS_OPEN = 1.15
REENTRY_MIN_PULLBACK = 0.03
REENTRY_COOLDOWN_SEC = 120

# ── Entry parameters (same as rtg_2.0) ───────────────────────────────
ENTRY_WINDOW_START = "09:30"
ENTRY_WINDOW_END = "15:55"
RTG_VOLUME_MULT = 1.5            # Same as rtg_2.0
RTG_MIN_VOLUME = 30000
RTG_MIN_PRICE_GAIN = 0.0
RTG_MIN_BAR_GAIN_PCT = 0.01     # Bar must be >=1% above open_price to qualify
RTG_ENTRY_AT_OPEN = True
ENTRY_CONFIRM_BARS = 1           # No 2-bar confirmation (same as rtg_2.0)

# GapGo disabled
GAPGO_MIN_FIRST_BAR_VOL = 99999999
GAPGO_MIN_BREAKOUT_VOL = 99999999

# ── Exit parameters (defaults — overridden by RVOL_EXIT_TIERS) ─────
RTG_STOP_PCT = 0.05
RTG_TARGET_PCT = 0.30
RTG_TIME_LIMIT_SEC = 0
RTG_TRAIL_ACTIVATE_PCT = 0.005
RTG_TRAIL_PCT = 0.01

# ── Position sizing ──────────────────────────────────────────────────
INITIAL_CAPITAL = 377.29
MIN_POSITION_SIZE = 40
MAX_POSITION_SIZE = 9999
EXCLUDE_SYMBOLS = {"AEI", "LITZ", "VOGX", "WEAV"}
MAX_DAILY_TRADES = 0
MAX_DAILY_LOSS_PCT = 0.04
EQUITY_POSITION_RATIO = 1.0

# ── Market hours ─────────────────────────────────────────────────────
MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"
FORCE_CLOSE_TIME = "15:59"

# ── Live trading ─────────────────────────────────────────────────────
USE_WEBSOCKET = True
POLL_INTERVAL = 3
DRY_RUN_POLL_INTERVAL = 5

# ── Slippage model ───────────────────────────────────────────────────
SLIPPAGE_ENTRY_PCT = 0.005
SLIPPAGE_EXIT_PCT = 0.005
SLIPPAGE_FORCE_CLOSE_PCT = 0.01

# ── Order parameters ─────────────────────────────────────────────────
ENTRY_LIMIT_BUFFER = 0.005
STOP_LIMIT_BUFFER = 0.02
FORCE_CLOSE_LIMIT_TIMEOUT = 60

# ── Backtest ─────────────────────────────────────────────────────────
BACKTEST_DAYS = 30

# ── Version ──────────────────────────────────────────────────────────
VERSION = "stonewang_daytrade_rtg_3.0"
VERSION_SHORT = "rtg_3.0"
