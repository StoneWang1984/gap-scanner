"""Config — stonewang_daytrade_1025out_2.0: RTG + RVOL-Adaptive Stop + 10:25 Exit.

Improvements over 1.0:
  - MIN_RVOL_TO_TRADE = 3x: filter out low-RVOL stocks (no momentum)
  - RVOL-adaptive stop loss: high RVOL → 7% stop, medium → 5%, low → 3%
  - Entry confirmation: require bar gain >= 1% above open_price
  - 10:25 exit captures opening drive (same as 1.0)
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
MIN_RVOL_TO_TRADE = 3.0  # Only trade stocks with RVOL >= 3x

# ── RVOL-weighted position sizing ────────────────────────────────────
RVOL_SIZING_TIERS = [
    (10.0, 0.50),   # RVOL > 10x → 50% of equity
    (5.0,  0.35),   # RVOL 5-10x → 35% of equity
    (0.0,  0.20),   # RVOL < 5x → 20% of equity
]

# ── RVOL-adaptive stop loss (same as rtg_2.0) ──────────────────────
RVOL_EXIT_TIERS = [
    (10.0, 0.07, 0.50),  # High RVOL: 7% stop, 50% target (for reference)
    (5.0,  0.05, 0.30),  # Medium: 5% stop, 30% target
    (0.0,  0.03, 0.15),  # Low: 3% stop, 15% target
]
STOP_PCT = 0.03    # Fallback default stop
TARGET_PCT = 0.15  # Fallback default target (unused, 10:25 exit is the target)

# ── Exit parameters (10:25 time-based exit) ──────────────────────────
EXIT_TIME = "10:25"         # Market sell all positions at 10:25 EST
# No trailing stop — 10:25 exit captures opening drive

# ── Entry parameters ─────────────────────────────────────────────────
ENTRY_WINDOW_START = "09:30"
ENTRY_WINDOW_END = "10:24"  # Only enter before 10:25

# Signal9Signal A: Red-to-Green with confirmation
RTG_VOLUME_MULT = 1.5
RTG_MIN_VOLUME = 30000
RTG_MIN_BAR_GAIN_PCT = 0.01  # Bar must be >=1% above open_price (filter weak signals)
RTG_MIN_PRICE_GAIN = 0.0
RTG_ENTRY_AT_OPEN = False

# Signal B: Gap-and-Go (DISABLED)
GAPGO_MIN_FIRST_BAR_VOL = 99999999
GAPGO_MIN_BREAKOUT_VOL = 99999999

# ── Re-entry: NONE ───────────────────────────────────────────────────
RTG_REENTRY_ALLOWED = False
RTG_REENTRY_MAX = 0
RTG_REENTRY_SIZE_PCT = 0.50
REENTRY_MAX_PRICE_VS_OPEN = 1.15
REENTRY_MIN_PULLBACK = 0.03
REENTRY_COOLDOWN_SEC = 120

# ── Position sizing ──────────────────────────────────────────────────
INITIAL_CAPITAL = 377.29
MIN_POSITION_SIZE = 40
MAX_POSITION_SIZE = 9999
MAX_POSITIONS = 8
EXCLUDE_SYMBOLS = {"AEI", "LITZ", "VOGX", "WEAV"}
MAX_DAILY_TRADES = 0          # 0 = no limit
MAX_DAILY_LOSS_PCT = 0.04     # 4% daily loss circuit breaker
EQUITY_POSITION_RATIO = 1.0

# ── Market hours ─────────────────────────────────────────────────────
MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"
FORCE_CLOSE_TIME = "10:25"    # Force close at 10:25 (strategy exit time)

# ── Live trading ─────────────────────────────────────────────────────
USE_WEBSOCKET = True
POLL_INTERVAL = 3
DRY_RUN_POLL_INTERVAL = 5

# ── Slippage model ───────────────────────────────────────────────────
SLIPPAGE_ENTRY_PCT = 0.005    # 0.5% entry slippage
SLIPPAGE_EXIT_PCT = 0.005     # 0.5% exit slippage
SLIPPAGE_FORCE_CLOSE_PCT = 0.01

# ── Order parameters ─────────────────────────────────────────────────
ENTRY_LIMIT_BUFFER = 0.005
STOP_LIMIT_BUFFER = 0.02
FORCE_CLOSE_LIMIT_TIMEOUT = 60

# ── Backtest ─────────────────────────────────────────────────────────
BACKTEST_DAYS = 30

# ── Version ──────────────────────────────────────────────────────────
VERSION = "stonewang_daytrade_1025out_2.0"
VERSION_SHORT = "1025out_2.0"
