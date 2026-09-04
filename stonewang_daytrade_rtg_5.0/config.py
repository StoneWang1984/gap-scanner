"""Config — stonewang_daytrade_rtg_5.0: Opening Range Breakout + ATR Stops.

Based on top day traders (Cam Connor, Brian Shannon):
  1. Opening Range Breakout: wait 3 bars, enter on breakout above range high
  2. ATR-based stops + gap expansion: stop = max(ATR×mult, |gap|×0.3), clamp 2%-8%
  3. Failed-entry cut: +1% in 3 min or exit
  4. Progressive trailing: profit>5%→1.5%, >10%→1%, >15%→0.5%
  5. Daily profit protection: peak profit drawdown 30%→force close all
  6. Concentrated positions: max 3, A+ gets 50%
  7. Momentum window: entries 09:30-10:30 only
  8. High quality filter: RVOL≥2.0, price≥$2, top 5 candidates
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
PRICE_MIN = 2.0        # $2+ only — penny stocks have wide spreads and manipulation
PRICE_MAX = 20.0

# Leveraged ETF exclusion
LEVERAGED_ETF_SUFFIXES = ("BULL", "BEAR")
LEVERAGED_ETF_PREFIXES = ()

# ── RTG candidate selection ──────────────────────────────────────────
MAX_CANDIDATES = 5   # Top 5 by RVOL — concentrated, not diversified
RVOL_LOOKBACK_DAYS = 20
RTG_ONLY = True

# ── Entry quality filters ────────────────────────────────────────────
MIN_ENTRY_RVOL = 2.0           # Skip RVOL < 2.0 (no institutional participation)
MIN_ENTRY_PRICE = 2.0          # Skip penny stocks: wide spreads, manipulation
NO_ENTRY_BEFORE_CLOSE_MIN = 30 # No entries within 30 min of force_close

# ── Opening Range Breakout (rtg_5.0) ─────────────────────────────────
ORB_ENABLED = True
ORB_BARS = 3                   # Wait 3 one-min bars for opening range
ORB_BREAKOUT_BUFFER = 0.002    # Entry = range_high × (1 + 0.002)
ORB_MIN_RANGE_PCT = 0.005      # Min opening range width 0.5% (flat opens → skip)

# ── Failed-Entry Cut (rtg_5.0) ──────────────────────────────────────
FAILED_ENTRY_ENABLED = True
FAILED_ENTRY_MIN_GAIN_PCT = 0.01  # Require +1% gain within grace window
FAILED_ENTRY_MAX_SECONDS = 180    # 3 min grace window

# ── ATR-based stops (rtg_5.0, ported from rtg_2.0) ──────────────────
ATR_PERIOD = 14
ATR_MULT_TIERS = [
    (10.0, 3.0),   # RVOL > 10× → 3.0× ATR (A+ setup, give room)
    (5.0,  2.5),   # RVOL > 5×  → 2.5× ATR
    (0.0,  2.0),   # else       → 2.0× ATR
]
ATR_STOP_MIN_PCT = 0.02   # Floor 2%
ATR_STOP_MAX_PCT = 0.08   # Cap 8% — gap stocks need wider stops
GAP_STOP_FACTOR = 0.3     # Stop covers 30% of gap magnitude
ATR_TRAIL_MULT = 2.0      # Trail width = 2.0× ATR

# ── Progressive trailing (rtg_5.0, ported from rtg_2.0) ─────────────
PROGRESSIVE_TRAIL_TIERS = [
    (0.15, 0.005),  # profit > 15% → trail = 0.5%
    (0.10, 0.010),  # profit > 10% → trail = 1%
    (0.05, 0.015),  # profit > 5%  → trail = 1.5%
]

# ── Daily profit protection (rtg_5.0, ported from rtg_2.0) ──────────
DAILY_PROFIT_PROTECT_ENABLED = True
DAILY_PROFIT_PROTECT_RATIO = 0.70    # Allow 30% drawdown from peak
DAILY_PROFIT_PROTECT_MIN = 5.0       # Activate when peak profit >= $5
DAILY_PROFIT_PROTECT_DELAY_SEC = 1800  # 30 min delay after open

# ── RVOL-weighted position sizing ────────────────────────────────────
RVOL_SIZING_TIERS = [
    (10.0, 0.50),   # A+: 50% equity
    (5.0,  0.35),   # A:  35% equity
    (2.0,  0.20),   # B:  20% equity
]

# ── RVOL-adaptive exit tiers (FALLBACK when ATR unavailable) ─────────
RVOL_EXIT_TIERS = [
    (10.0, 0.07, 0.00, 0.05, 0.05),  # High RVOL: 7% stop, no target, trail 5%
    (5.0,  0.05, 0.00, 0.04, 0.04),  # Medium:    5% stop, no target, trail 4%
    (2.0,  0.03, 0.00, 0.03, 0.02),  # Low:       3% stop, no target, trail 2%
]

# ── Re-entry: NONE ───────────────────────────────────────────────────
RTG_REENTRY_ALLOWED = False
RTG_REENTRY_MAX = 0
RTG_REENTRY_SIZE_PCT = 0.50
REENTRY_MAX_PRICE_VS_OPEN = 1.15
REENTRY_MIN_PULLBACK = 0.03
REENTRY_COOLDOWN_SEC = 120

# ── Entry parameters ─────────────────────────────────────────────────
ENTRY_WINDOW_START = "09:30"
ENTRY_WINDOW_END = "10:30"   # Gap momentum half-life ~30 min, no entries after 10:30

RTG_VOLUME_MULT = 1.5
RTG_MIN_VOLUME = 30000
RTG_MIN_PRICE_GAIN = 0.0
RTG_ENTRY_AT_OPEN = True

GAPGO_MIN_FIRST_BAR_VOL = 99999999   # Disabled
GAPGO_MIN_BREAKOUT_VOL = 99999999    # Disabled

# ── Exit parameters ──────────────────────────────────────────────────
RTG_STOP_PCT = 0.05
RTG_TARGET_PCT = 0.0          # No target — trail + progressive trail manage exit
RTG_TIME_LIMIT_SEC = 0
RTG_TRAIL_ACTIVATE_PCT = 0.03
RTG_TRAIL_PCT = 0.02

# ── Position sizing ──────────────────────────────────────────────────
INITIAL_CAPITAL = 294.04      # Account equity (updated 2026-09-04)
MIN_POSITION_SIZE = 40
MAX_POSITION_SIZE = 9999
MAX_POSITIONS = 3             # Concentrated: $296 can't split 8 ways
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

# �Bcktest ────────────────────────────────────────────────────────────
BACKTEST_DAYS = 30

# ── Version ──────────────────────────────────────────────────────────
VERSION = "stonewang_daytrade_rtg_5.0"
VERSION_SHORT = "rtg_5.0"
