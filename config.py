"""Stone 0.4.14 — Main config (synced with versions/config_stone_0.4.14.py)"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# Alpaca API
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"
ALPACA_DATA_URL = "https://data.alpaca.markets"

# Scanner filters — aligned with 0.4.10/0.4.14
GAP_THRESHOLD = 0.10
MIN_VOLUME = 10000
MIN_DOLLAR_VOLUME = 100000
PRICE_MIN = 1.0
PRICE_MAX = 20.0

# Leveraged ETF exclusion
LEVERAGED_ETF_SUFFIXES = ("U", "L", "BULL", "BEAR")
LEVERAGED_ETF_PREFIXES = ()

# Entry — confirmation logic
ENTRY_CONFIRMATION = True

# 0.4.11: Skip first trade if entry price >= open price
ENTRY_BELOW_OPEN = True

# Stop loss — ATR based (first trade)
STOP_LOSS_ATR_MULT = 2.0
STOP_LOSS_PCT_FALLBACK = 0.20

# 0.4.14: Stop loss max cap
STOP_LOSS_MAX_PCT = 0.10

# Profit targets — first trade (three tiers)
PROFIT_RETRACEMENT_75 = 0.75
PROFIT_RETRACEMENT_1125 = 1.125
PROFIT_RETRACEMENT_150 = 1.50

# Partial profit — first trade
PARTIAL_SELL_RATIO_75 = 0.25
PARTIAL_SELL_RATIO_1125 = 1/3
PARTIAL_SELL_RATIO_150 = 1/3

# Trailing stop — first trade
TRAILING_STOP_PCT_75 = 0.03
TRAILING_STOP_PCT_1125 = 0.04
TRAILING_STOP_PCT_150 = 0.05

# Time limit exit — if no target hit within N 5-min bars, sell all when price >= entry
FIRST_TRADE_TIME_LIMIT_BARS = 8  # 8 bars × 5 min = 40 minutes (0 = disabled)

# Re-entry trade — Stone 0.4.14: retracement + trailing, no time limit
REENTRY_STOP_PCT = 0.05                 # legacy fallback
REENTRY_STOP_ATR_MULT = 1.5             # ATR-based stop multiplier
REENTRY_STOP_PCT_FALLBACK = 0.04        # fallback when ATR unavailable
REENTRY_PROFIT_RETRACEMENT_1 = 0.75     # tier-1: sell 50% at 75% retracement
REENTRY_SELL_RATIO_1 = 0.5             # sell 50% at tier-1
REENTRY_TRAILING_PCT_2 = 0.03           # 3% trailing after tier-1
REENTRY_POSITION_RATIO = 0.5            # half position vs first trade
REENTRY_CUTOFF_TIME = "12:30"
REENTRY_MAX_BARS_BEFORE_TARGET = 0      # no time limit (0.4.13: removed)
REENTRY_MIN_PULLBACK = 0.03             # 0.4.14: min 3% pullback from peak for re-entry
PULLBACK_STOP_THRESHOLD = 0.15          # if pullback from peak > 15%, stop day

# 0.4.14: Daily loss circuit breaker
MAX_DAILY_LOSS_PCT = 0.05               # 0.4.14: 5% daily loss circuit breaker

# Position management
MAX_POSITIONS_PER_DAY = 3
MAX_DAILY_TRADES = 3               # max total trades per day (first + re-entries)
EQUITY_POSITION_RATIO = 0.80
MAX_POSITION_SIZE = 100000
MIN_POSITION_SIZE = 250
INITIAL_CAPITAL = 1000
FORCE_CLOSE_TIME = "15:50"

# Market hours (EST)
MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"

# Backtest parameters
BACKTEST_DAYS = 180

# ══════════════════════════════════════════════════════════════════
# Stone 0.5 — MACD 2nd-derivative signals on 5-minute bars
# ══════════════════════════════════════════════════════════════════

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
MACD_WARMUP_BARS = 35
MACD_STOP_PCT = 0.08
MACD_ENTRY_CUTOFF_TIME = "13:00"
MACD_WARMUP_DAYS = 3

# ══════════════════════════════════════════════════════════════════
# Stone 0.4.2 — Optimized Stone 0.4
# ══════════════════════════════════════════════════════════════════

LOW_PRICE_STOP_PCT = 0.05
LOW_PRICE_THRESHOLD = 1.0
EARLY_STOP_BARS = 3

VOLUME_RATIO_MIN = 1.5
PRIOR_GAIN_MAX = 0.15
PRIOR_GAIN_DAYS = 3

LATE_TIGHTEN_TIME_BAR = 66
LATE_CLOSE_TIME_BAR = 72
TRAILING_LATE_FACTOR = 0.5

REENTRY_MIN_PULLBACK_042 = 0.03
REENTRY_EARLY_EXIT_BARS = 2

PARTIAL_SELL_RATIO_75_042 = 1/3
PARTIAL_SELL_RATIO_100_042 = 1/3
PARTIAL_SELL_RATIO_150_042 = 1/3
PROFIT_RETRACEMENT_100_042 = 1.0

# ══════════════════════════════════════════════════════════════════
# Stone 0.4.3 — Dynamic position sizing by dollar volume
# ══════════════════════════════════════════════════════════════════
POSITION_DV_RATIO = 0.01
