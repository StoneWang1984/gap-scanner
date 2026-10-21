"""Config — stonewang_daytrade_momentum_surge_1.1

Pre-market + Three-pronged intraday momentum strategy:
  Pre-Market: Gap continuation (5%+, news catalyst) — 7:00-9:25 AM
  Prong 1: Small Gap Scan (3% gap, RVOL >= 2x) — RTG entry
  Prong 2: Intraday Momentum Scan (every 5 min) — breakout entry
  Prong 3: Low-Open Reversal (gap-down -3% to -10% with high RVOL) — reversal entry
Exit: 5% hard stop + trailing stop (3% after +5% activation) + 10-min time limit
Daily loss circuit breaker: 8%
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# Alpaca API
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER = False
DRY_RUN = False
ALPACA_BASE_URL = "https://api.alpaca.markets"
ALPACA_DATA_URL = "https://data.alpaca.markets"

DATA_FEED = "iex"
from alpaca.data.enums import DataFeed as _DF
DATA_FEED_OBJ = _DF.IEX

# ── Scanner filters ──────────────────────────────────────────────────
GAP_THRESHOLD = 0.03          # 3% min gap (Prong 1 — lowered from 10%)
GAP_MAX = 1.0                 # max 100% gap
MIN_VOLUME = 10000
MIN_DOLLAR_VOLUME = 100000
PRICE_MIN = 3.0
PRICE_MAX = 20.0

LEVERAGED_ETF_SUFFIXES = ("BULL", "BEAR")
LEVERAGED_ETF_PREFIXES = ()

# ── Prong 1: Small Gap Scan ─────────────────────────────────────────
MAX_CANDIDATES = 100
RTG_MIN_RVOL = 2.0
RVOL_LOOKBACK_DAYS = 20
RTG_ONLY = True

# ── Prong 2: Intraday Momentum Scan ─────────────────────────────────
MOMENTUM_SCAN_ENABLED = True
MOMENTUM_SCAN_INTERVAL_SEC = 300     # Scan every 5 minutes
MOMENTUM_MIN_GAIN_PCT = 0.03        # Price up >3% from open
MOMENTUM_MIN_RVOL = 1.5             # RVOL > 1.5x
MOMENTUM_VOLUME_SURGE_MULT = 2.0    # 5-min vol > 2x avg 5-min vol
MOMENTUM_BREAKOUT_LOOKBACK_MIN = 30 # 30-min high for breakout
MOMENTUM_MAX_CANDIDATES = 20
MOMENTUM_PRICE_MIN = 3.0
MOMENTUM_PRICE_MAX = 20.0

# ── Prong 3: Low-Open Reversal Scan ─────────────────────────────────
REVERSAL_SCAN_ENABLED = True
REVERSAL_GAP_MIN = -0.10            # -10% max gap down
REVERSAL_GAP_MAX = -0.03            # -3% min gap down
REVERSAL_MIN_RVOL = 3.0             # High RVOL required (>3x)
REVERSAL_MAX_CANDIDATES = 20
REVERSAL_PRICE_MIN = 3.0
REVERSAL_PRICE_MAX = 20.0

# ── Volume scan (DISABLED — replaced by Prong 2) ────────────────────
VOLUME_SCAN_INTERVAL = 999999
VOLUME_SCAN_TOP_N = 0
VOLUME_SCAN_MOVERS_TOP_N = 0
VOLUME_SCAN_MIN_REL_VOL_RATIO = 999
VOLUME_SCAN_PRICE_MIN = 0.50
VOLUME_SCAN_PRICE_MAX = 20.0

# ── RVOL-weighted position sizing ────────────────────────────────────
RVOL_SIZING_TIERS = [
    (10.0, 1.00),
    (5.0,  1.00),
    (0.0,  1.00),
]
RVOL_SIZING_CAP = 10.0

# ── Entry parameters ────────────────────────────────────────────────
ENTRY_WINDOW_START = "09:30"
ENTRY_WINDOW_END = "15:30"

RTG_VOLUME_MULT = 1.5
RTG_MIN_VOLUME = 30000
RTG_MIN_PRICE_GAIN = 0.0
RTG_ENTRY_AT_OPEN = True

GAPGO_MIN_FIRST_BAR_VOL = 99999999
GAPGO_MIN_BREAKOUT_VOL = 99999999

# ── Exit parameters ─────────────────────────────────────────────────
RTG_STOP_PCT = 0.05                # 5% hard stop loss
RTG_TIME_LIMIT_SEC = 600           # 10-minute time limit
RTG_MIN_HOLD_SEC = 60              # Min 60s hold

# Trailing stop (key differentiator from rtg_2mins_all)
TRAILING_STOP_ENABLED = True
TRAILING_ACTIVATE_PCT = 0.05       # Activate after +5% profit
TRAILING_PCT = 0.03                # Trail at 3%

RTG_TARGET_PCT = 0.0
RTG_TRAIL_ACTIVATE_PCT = 0.05
RTG_TRAIL_PCT = 0.03

# ── ATR-based adaptive stop ──────────────────────────────────────────
ATR_PERIOD = 14
ATR_MULT_TIERS = [
    (10.0, 3.0),
    (5.0,  2.5),
    (0.0,  2.0),
]
ATR_STOP_MIN_PCT = 0.05
ATR_STOP_MAX_PCT = 0.05
GAP_STOP_FACTOR = 0.0
ATR_TRAIL_MULT = 2.0
ATR_TARGET_MULT = 0.0

VOL_SURGE_STOP_MAX_PCT = 0.05
VOL_SURGE_TRAIL_MULT = 1.5
VOL_SURGE_TRAIL_MAX_PCT = 0.05

RVOL_EXIT_TIERS = [
    (10.0, 0.05, 0.50, 0.05, 0.03),
    (5.0,  0.05, 0.30, 0.05, 0.03),
    (0.0,  0.05, 0.15, 0.05, 0.03),
]

# ── Daily loss circuit breaker (ENABLED) ────────────────────────────
MAX_DAILY_LOSS_PCT = 0.08

# ── Daily profit protection ─────────────────────────────────────────
DAILY_PROFIT_PROTECT_ENABLED = False
DAILY_PROFIT_PROTECT_RATIO = 0.90
DAILY_PROFIT_PROTECT_MIN = 10.0
DAILY_PROFIT_PROTECT_DELAY_SEC = 1800

PROGRESSIVE_TRAIL_TIERS = []

# ── Afternoon momentum (DISABLED — replaced by Prong 2) ─────────────
AFTERNOON_SCAN_ENABLED = False
AFTERNOON_PRICE_MAX = 200.0
AFTERNOON_MIN_RVOL = 2.0
AFTERNOON_MIN_GAIN_PCT = 0.02
AFTERNOON_MAX_CANDIDATES = 5
AFTERNOON_ENTRY_END = "15:30"
AFTERNOON_STOP_PCT = 0.05
AFTERNOON_TRAIL_PCT = 0.015
AFTERNOON_TRAIL_ACTIVATE_PCT = 0.01

# ── Re-entry ────────────────────────────────────────────────────────
RTG_REENTRY_ALLOWED = True
RTG_REENTRY_MAX = 3
RTG_REENTRY_SIZE_PCT = 1.00
REENTRY_MAX_PRICE_VS_OPEN = 1.15
REENTRY_MIN_PULLBACK = 0.0
REENTRY_COOLDOWN_SEC = 60
REENTRY_STOP_COOLDOWN_SEC = 120

# ── Position sizing ─────────────────────────────────────────────────
INITIAL_CAPITAL = 163.00           # Updated 2026-10-16
MIN_POSITION_SIZE = 40
MAX_POSITION_SIZE = 9999
MAX_POSITIONS = 1
EXCLUDE_SYMBOLS = {"AEI", "LITZ", "VOGX", "WEAV"}
MAX_DAILY_TRADES = 0
EQUITY_POSITION_RATIO = 1.0

# ── Market hours ────────────────────────────────────────────────────
MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"
FORCE_CLOSE_TIME = "15:59"

# ── Live trading ────────────────────────────────────────────────────
USE_WEBSOCKET = True
POLL_INTERVAL = 3
DRY_RUN_POLL_INTERVAL = 5

# ── Slippage model ─────────────────────────────────────────────────
SLIPPAGE_ENTRY_PCT = 0.005
SLIPPAGE_EXIT_PCT = 0.005
SLIPPAGE_FORCE_CLOSE_PCT = 0.01

# ── Order parameters ───────────────────────────────────────────────
ENTRY_LIMIT_BUFFER = 0.005
STOP_LIMIT_BUFFER = 0.02
FORCE_CLOSE_LIMIT_TIMEOUT = 60

# ── Backtest ────────────────────────────────────────────────────────
BACKTEST_DAYS = 30

# ── Pre-Market Trading (7:00-9:25 AM) ────────────────────────────────
PREMARKET_ENABLED = True
PREMARKET_START = "07:00"             # Begin scanning at 7:00 AM
PREMARKET_ENTRY_END = "09:20"         # Stop new entries at 9:20
PREMARKET_TRANSITION_TIME = "09:25"   # Convert to regular session at 9:25
PREMARKET_GAP_MIN = 0.05             # 5% min pre-market gap
PREMARKET_MIN_VOLUME = 50000          # Min pre-market volume (shares)
PREMARKET_PRICE_MIN = 3.0
PREMARKET_PRICE_MAX = 20.0
PREMARKET_MAX_CANDIDATES = 10
PREMARKET_STOP_PCT = 0.03            # 3% hard stop (wider for spread)
PREMARKET_TRAIL_ACTIVATE_PCT = 0.03  # +3% activates trail
PREMARKET_TRAIL_PCT = 0.02           # 2% trail
PREMARKET_TIME_LIMIT_SEC = 1800      # 30 min time limit
PREMARKET_LIMIT_BUFFER = 0.005       # 0.5% limit price buffer
PREMARKET_MAX_POSITIONS = 1

# Pre-market news catalyst filter
PREMARKET_NEWS_REQUIRED = True                            # Must have news catalyst
PREMARKET_NEWS_KEYWORDS = [                               # Catalyst keywords
    "FDA", "approval", "approved",                         # FDA
    "earnings", "EPS", "revenue", "beat", "exceed",       # Earnings
    "acquisition", "acquire", "merger", "buyout", "takeover",  # M&A
    "guidance", "upgraded", "initiated",                   # Analyst
]
PREMARKET_NEWS_LOOKBACK_HOURS = 12                        # Check news within 12 hours

# Pre-market entry speed split
PREMARKET_FAST_GAP_PCT = 0.08         # ≥8% gap = fast rise → immediate buy
PREMARKET_SLOW_GAP_PCT = 0.05         # 5-8% gap = slow rise → 3-bar confirmation

# ── Version ─────────────────────────────────────────────────────────
VERSION = "stonewang_daytrade_momentum_surge_1.0"
VERSION_SHORT = "momentum_surge_1.0"
