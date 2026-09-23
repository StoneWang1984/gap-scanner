"""Config — stonewang_daytrade_keep_raising_1.0: Morning RTG + Afternoon Keep Raising.

Before 10:30: Same as rtg_2.0 (gap scan + RTG entry + vol_surge + ATR stops)
After  10:30: Keep Raising mode — scan for stocks with small-amplitude
              steady uptrend, buy all-in, sell when trend breaks, rescan.
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

# Data feed: SIP (full market data)
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

# ── Volume breakout scan (intraday opportunity discovery) ───────────
VOLUME_SCAN_INTERVAL = 300
VOLUME_SCAN_TOP_N = 30
VOLUME_SCAN_MOVERS_TOP_N = 20
VOLUME_SCAN_MIN_REL_VOL_RATIO = 3.0
VOLUME_SCAN_PRICE_MIN = 0.50
VOLUME_SCAN_PRICE_MAX = 20.0

# ── RVOL-weighted position sizing ────────────────────────────────────
RVOL_SIZING_TIERS = [
    (10.0, 1.00),
    (5.0,  1.00),
    (0.0,  1.00),
]
RVOL_SIZING_CAP = 10.0

# ── ATR-based adaptive stop loss ──────────────────────────────────────
ATR_PERIOD = 14
ATR_MULT_TIERS = [
    (10.0, 3.0),
    (5.0,  2.5),
    (0.0,  2.0),
]
ATR_STOP_MIN_PCT = 0.02
ATR_STOP_MAX_PCT = 0.08
GAP_STOP_FACTOR = 0.3
ATR_TRAIL_MULT = 2.0
ATR_TARGET_MULT = 0.0

# ── vol_surge exit parameters ──────────────────────────────────────
VOL_SURGE_STOP_MAX_PCT = 0.05
VOL_SURGE_TRAIL_MULT = 1.5
VOL_SURGE_TRAIL_MAX_PCT = 0.03

# ── RVOL-adaptive exit tiers (FALLBACK) ──────────────────────────────
RVOL_EXIT_TIERS = [
    (10.0, 0.07, 0.50, 0.05, 0.05),
    (5.0,  0.05, 0.30, 0.04, 0.04),
    (0.0,  0.03, 0.15, 0.03, 0.02),
]

# ── Daily profit protection ────────────────────────────────────────────
DAILY_PROFIT_PROTECT_ENABLED = True
DAILY_PROFIT_PROTECT_RATIO = 0.90
DAILY_PROFIT_PROTECT_MIN = 10.0
DAILY_PROFIT_PROTECT_DELAY_SEC = 1800

# ── Progressive trailing stop ───────────────────────────────────────────
PROGRESSIVE_TRAIL_TIERS = [
    (0.15, 0.000),
    (0.10, 0.005),
    (0.075, 0.010),
    (0.05, 0.012),
    (0.025, 0.015),
]

# ── Afternoon momentum scan — DISABLED (replaced by keep-raising) ──
AFTERNOON_SCAN_ENABLED = False
AFTERNOON_PRICE_MAX = 200.0
AFTERNOON_MIN_RVOL = 2.0
AFTERNOON_MIN_GAIN_PCT = 0.02
AFTERNOON_MAX_CANDIDATES = 5
AFTERNOON_ENTRY_END = "15:30"
AFTERNOON_STOP_PCT = 0.03
AFTERNOON_TRAIL_PCT = 0.015
AFTERNOON_TRAIL_ACTIVATE_PCT = 0.01

# ── Re-entry: NONE ────────────────────────────────────────────────────
RTG_REENTRY_ALLOWED = False
RTG_REENTRY_MAX = 0
RTG_REENTRY_SIZE_PCT = 0.50
REENTRY_MAX_PRICE_VS_OPEN = 1.15
REENTRY_MIN_PULLBACK = 0.03
REENTRY_COOLDOWN_SEC = 120

# ── Entry parameters ─────────────────────────────────────────────────
ENTRY_WINDOW_START = "09:30"
ENTRY_WINDOW_END = "10:30"

RTG_VOLUME_MULT = 1.5
RTG_MIN_VOLUME = 30000
RTG_MIN_PRICE_GAIN = 0.0
RTG_ENTRY_AT_OPEN = True

GAPGO_MIN_FIRST_BAR_VOL = 99999999
GAPGO_MIN_BREAKOUT_VOL = 99999999

# ── Exit parameters ──────────────────────────────────────────────────
RTG_STOP_PCT = 0.05
RTG_TARGET_PCT = 0.0
RTG_TIME_LIMIT_SEC = 0
RTG_TRAIL_ACTIVATE_PCT = 0.03
RTG_TRAIL_PCT = 0.02

# ── Position sizing ──────────────────────────────────────────────────
INITIAL_CAPITAL = 306.24
MIN_POSITION_SIZE = 40
MAX_POSITION_SIZE = 9999
MAX_POSITIONS = 1
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

# ══════════════════════════════════════════════════════════════════════
# ── Keep Raising mode (10:30-15:59) ────────────────────────────────
# ══════════════════════════════════════════════════════════════════════
KEEP_RAISING_ENABLED = True
KEEP_RAISING_START = "10:30"
KEEP_RAISING_ENTRY_END = "15:45"      # No new entries after 15:45
KEEP_RAISING_PRICE_MIN = 1.0
KEEP_RAISING_PRICE_MAX = 200.0

# ── Keep Raising screener narrowing ────────────────────────────────
KR_SCREENER_GAINERS_TOP_N = 50
KR_SCREENER_ACTIVES_TOP_N = 50
KR_SCREENER_MIN_GAIN_PCT = 1.0        # Min +1% from prev close
KR_SCREENER_MAX_GAIN_PCT = 50.0       # Max +50% (avoid extreme movers)

# ── Keep Raising bar pattern detection ─────────────────────────────
KR_LOOKBACK_MINUTES = 20              # Look at last 20 1-min bars
KR_MIN_UP_BARS_RATIO = 0.60           # 60%+ bars green (close > open)
KR_MAX_AMPLITUDE_RATIO = 0.50         # avg bar range / total move <= 0.50
KR_MIN_TOTAL_GAIN_PCT = 0.01          # Min +1% gain in window
KR_MAX_TOTAL_GAIN_PCT = 0.15          # Max +15% (avoid spikes)
KR_MIN_CONSISTENCY_SCORE = 0.40       # 40%+ bars push price higher
KR_NEAR_HIGH_PCT = 0.01              # Price must be within 1% of window high
KR_MIN_AVG_BAR_VOLUME = 1000          # Min avg volume per bar (avoid illiquid)

# ── Keep Raising exit triggers ─────────────────────────────────────
KR_EXIT_CONSEC_DOWN_BARS = 3          # 3 red bars = stopped rising
KR_EXIT_DROP_PCT = 0.005             # Drop 0.5% from local high = exit
KR_EXIT_MAX_HOLD_MINUTES = 120        # Max hold 2 hours

# ── Keep Raising scan intervals ────────────────────────────────────
KR_SCAN_INTERVAL_SEC = 30             # Rescan every 30s when no position
KR_MONITOR_INTERVAL_SEC = 5           # Check exit every 5s while in position

# ── Keep Raising stop/trail (safety net) ────────────────────────────
KR_STOP_PCT = 0.03                    # 3% hard stop
KR_TRAIL_ACTIVATE_PCT = 0.01         # Trail activates after +1%
KR_TRAIL_PCT = 0.010                  # 1% trailing stop
KR_PROGRESSIVE_TRAIL_TIERS = [
    (0.10, 0.003),  # +10% -> 0.3% trail
    (0.05, 0.005),  # +5%  -> 0.5% trail
    (0.03, 0.008),  # +3%  -> 0.8% trail
    (0.01, 0.010),  # +1%  -> 1% trail
]

# ── Version ──────────────────────────────────────────────────────────
VERSION = "stonewang_daytrade_keep_raising_1.0"
VERSION_SHORT = "keep_raising_1.0"
