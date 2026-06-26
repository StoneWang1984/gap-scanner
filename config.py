import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# Alpaca API
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"
ALPACA_DATA_URL = "https://data.alpaca.markets"

# Scanner filters
GAP_THRESHOLD = 0.20
MIN_VOLUME = 50000
MIN_DOLLAR_VOLUME = 500000
PRICE_MIN = 0.25
PRICE_MAX = 20.0

# Entry — confirmation logic
ENTRY_CONFIRMATION = True

# Stop loss — ATR based (first trade)
STOP_LOSS_ATR_MULT = 2.0
STOP_LOSS_PCT_FALLBACK = 0.20

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

# Re-entry trade — Stone 0.4
REENTRY_STOP_PCT = 0.05            # 5% stop loss for re-entry
REENTRY_PROFIT_RETRACEMENT = 1.50  # 150% of (prev_high - entry) as target
REENTRY_SELL_RATIO = 1/3           # sell 1/3 at target
REENTRY_TRAILING_PCT = 0.05        # 5% trailing stop after target sell
PULLBACK_STOP_THRESHOLD = 0.15     # if pullback from peak > 15%, stop day

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

# MACD parameters (standard)
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# Minimum bars before MACD is valid (26 slow + 9 signal - 1 + 1 margin)
MACD_WARMUP_BARS = 35

# Stop loss
MACD_STOP_PCT = 0.08  # 8% stop loss from entry price

# Entry cutoff (MACD on 5-min bars needs ~34 bars ≈ 2.8 hrs from open)
MACD_ENTRY_CUTOFF_TIME = "13:00"

# Number of prior trading days to fetch for MACD warmup
MACD_WARMUP_DAYS = 3

# ══════════════════════════════════════════════════════════════════
# Stone 0.4.2 — Optimized Stone 0.4
# ══════════════════════════════════════════════════════════════════

# Stop loss — low-price optimization
LOW_PRICE_STOP_PCT = 0.05           # fixed 5% stop for stocks < $1
LOW_PRICE_THRESHOLD = 1.0           # low-price stock threshold
EARLY_STOP_BARS = 3                 # check first N bars for early stop

# Entry quality filters
VOLUME_RATIO_MIN = 1.5              # opening volume ratio minimum
PRIOR_GAIN_MAX = 0.15               # max cumulative gain in prior N days
PRIOR_GAIN_DAYS = 3                 # how many prior days to check

# Late-day management (bar index from 9:30 open, 5-min bars)
LATE_TIGHTEN_TIME_BAR = 66          # 14:00 = bar #66
LATE_CLOSE_TIME_BAR = 72            # 14:30 = bar #72
TRAILING_LATE_FACTOR = 0.5          # tighten trailing stop by this factor after 14:00

# Re-entry quality control
REENTRY_MIN_PULLBACK_042 = 0.03     # minimum 3% pullback from peak for re-entry
REENTRY_EARLY_EXIT_BARS = 2         # exit re-entry if no gain in N bars

# Three-tier ratios — 0.4.2 (1/3 each)
PARTIAL_SELL_RATIO_75_042 = 1/3
PARTIAL_SELL_RATIO_100_042 = 1/3
PARTIAL_SELL_RATIO_150_042 = 1/3
PROFIT_RETRACEMENT_100_042 = 1.0    # 100% gap retracement target

# ══════════════════════════════════════════════════════════════════
# Stone 0.4.3 — Dynamic position sizing by dollar volume
# ══════════════════════════════════════════════════════════════════
POSITION_DV_RATIO = 0.01            # max position = daily dollar volume × this ratio
