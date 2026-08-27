"""Config — stonewang_daytrade_rtg_3.0: Cycle-based All-in RTG with 1% Trailing Stop.

Fundamental differences from rtg_2.0:
  1. Cycle-based: scan -> buy best stock -> monitor -> force close -> scan again
  2. Single position: at most ONE position at a time
  3. All-in: buy with ALL available capital (no RVOL tiered sizing)
  4. Fixed 1% trailing stop (not progressive, not RVOL-tiered)
  5. Force close before next scan: if still holding at scan time, close then scan
  6. Scan every 5 minutes (not 30 min)
  7. No daily profit protection (each cycle is independent)
  8. No re-entry (not applicable in cycle model)
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

# ── Cycle parameters (NEW in rtg_3.0) ────────────────────────────────
SCAN_INTERVAL_SEC = 300          # Scan every 5 minutes
MIN_RVOL_TO_TRADE = 3.0         # Minimum RVOL to qualify as "best stock"
ALL_IN_BP_RATIO = 0.95          # Buy with 95% of available buying power
MAX_POSITIONS = 1               # Exactly one position at a time
MAX_ENTRY_ATTEMPTS = 3          # Try top 3 candidates per cycle

# ── Exit: Fixed 1% trailing stop ─────────────────────────────────────
TRAIL_PCT = 0.01                 # 1% trailing stop (FIXED)
TRAIL_ACTIVATE_PCT = 0.005       # Activate trail after +0.5% gain
STOP_PCT = 0.03                  # 3% hard stop (backstop)
TARGET_PCT = 0.50                # 50% target (safety valve)

# ── Intraday breakout signal (rtg_3.0) ──────────────────────────────
# Detects afternoon breakouts: stock makes new day high + volume spike
# Catches momentum that develops after the opening drive fades
BREAKOUT_ENABLED = True           # Enable intraday breakout signal
BREAKOUT_MIN_BARS = 5             # Min bars before checking (need history for day_high)
BREAKOUT_VOLUME_MULT = 1.5        # Volume multiplier (same as RTG)
BREAKOUT_ENTRY_AT_CLOSE = True    # Enter at breakout close price (not open)

# ── No daily profit protection (rtg_3.0) ─────────────────────────────
DAILY_PROFIT_PROTECT_ENABLED = False
DAILY_PROFIT_PROTECT_RATIO = 0.85
DAILY_PROFIT_PROTECT_MIN = 5.0

# ── No progressive trailing (rtg_3.0) ───────────────────────────────
PROGRESSIVE_TRAIL_TIERS = []

# ── RVOL sizing/exit tiers (flat for rtg_3.0) ───────────────────────
RVOL_SIZING_TIERS = [(0.0, 1.0)]  # Always 100% (all-in)
RVOL_EXIT_TIERS = [(0.0, 0.03, 0.50, 0.005, 0.01)]  # Flat: 3% stop, 50% target, +0.5% activate, 1% trail

# ── Re-entry: NONE ───────────────────────────────────────────────────
RTG_REENTRY_ALLOWED = False
RTG_REENTRY_MAX = 0
RTG_REENTRY_SIZE_PCT = 0.50
REENTRY_MAX_PRICE_VS_OPEN = 1.15
REENTRY_MIN_PULLBACK = 0.03
REENTRY_COOLDOWN_SEC = 120

# ── Entry parameters ─────────────────────────────────────────────────
ENTRY_WINDOW_START = "09:30"
ENTRY_WINDOW_END = "15:55"       # Slightly earlier to avoid late entries
RTG_VOLUME_MULT = 1.5
RTG_MIN_VOLUME = 30000
RTG_MIN_PRICE_GAIN = 0.0
RTG_ENTRY_AT_OPEN = True

# GapGo disabled
GAPGO_MIN_FIRST_BAR_VOL = 99999999
GAPGO_MIN_BREAKOUT_VOL = 99999999

# ── Exit parameters (defaults — overridden by flat values above) ─────
RTG_STOP_PCT = 0.03
RTG_TARGET_PCT = 0.50
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
