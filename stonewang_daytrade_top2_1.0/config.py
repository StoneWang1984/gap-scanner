"""Config — stonewang_daytrade_top2_1.0: Top-2 Ranking + Open Entry + 5-Bar Add + Trailing Stop.

Strategy:
  - Pre-market scan for gap-up stocks (gap > 10%), rank by RVOL, select top 40
  - Rank candidates by first5chg (5-min momentum), select top N per day
  - Entry Phase 1: Buy base position (30%) at open price (09:30)
  - Entry Phase 2: After 5 bars (09:35), check 5-bar filter:
      - PASS: Add 70% position at bar5 close, activate trailing stop
      - FAIL: Hold base with 5% stop, exit at 10:25
  - Exit: Trailing stop (2% after +3% profit) or initial stop loss
  - No fixed 10:25 exit for confirmed positions — let winners run

Backtest validation (3 months, 90 days):
  - Combined Top-2: +278.4%, 180 trades, 60.0% WR, R/R 4.8:1
  - Combined Top-1: +178.8%, 90 trades, 61.1% WR, R/R 7.5:1
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

# ── Candidate selection ──────────────────────────────────────────────
MAX_CANDIDATES = 40
RVOL_LOOKBACK_DAYS = 20
RTG_ONLY = True

# ── RVOL-weighted position sizing ────────────────────────────────────
RVOL_SIZING_TIERS = [
    (10.0, 0.50),   # RVOL > 10x → 50% of equity
    (5.0,  0.35),   # RVOL 5-10x → 35% of equity
    (0.0,  0.20),   # RVOL < 5x → 20% of equity
]

# ── Top-N selection ──────────────────────────────────────────────────
TOP_N = 2                    # Number of top-ranked stocks to trade per day
RANK_BY = "first5chg"       # Rank candidates by first 5-min change

# ── 5-Bar entry filter (frik5bar) ────────────────────────────────────
FRIK5BAR_ENABLED = True           # Master switch for 5-bar filter
FRIK5BAR_BARS = 5                 # Wait N 1-min bars before checking filter
FRIK5BAR_BAR1_BULLISH = True      # Require first bar close > open_price
FRIK5BAR_MIN_5MIN_CHG = 0.02      # Require first 5 bars net change > +2%
FRIK5BAR_MAX_GAP = 0.25           # Skip if gap >= 25% (0 = disabled)

# ── Open entry + 5-bar add ──────────────────────────────────────────
OPEN_ENTRY_BASE_PCT = 0.30    # Base position (30%) entered at open price
OPEN_ENTRY_ADD_PCT = 0.70     # Add position (70%) on 5-bar confirmation

# ── Exit parameters ──────────────────────────────────────────────────
STOP_LOSS_PCT = 0.03          # Initial stop loss for confirmed positions (3%)
BASE_STOP_PCT = 0.05          # Stop loss for base position when filter fails (5%)
TRAIL_START = 0.03            # Profit threshold to activate trailing stop (+3%)
TRAIL_PCT = 0.02              # Trailing stop percentage (2%)
FORCE_CLOSE_TIME = "15:50"    # Force close all positions at 15:50 EST

# ── Entry parameters ─────────────────────────────────────────────────
ENTRY_WINDOW_START = "09:30"  # Open entry at market open
ENTRY_WINDOW_END = "09:35"    # Add position after 5-bar filter check

# Signal A: Red-to-Green (used internally, not primary signal)
RTG_VOLUME_MULT = 1.5
RTG_MIN_VOLUME = 30000
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
EXIT_TIME = "10:25"            # For base positions when filter fails

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
VERSION = "stonewang_daytrade_top2_1.0"
VERSION_SHORT = "top2_1.0"
