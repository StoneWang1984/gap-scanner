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
GAP_THRESHOLD = 0.10          # gap > 10% (lowered from 20%)
MIN_VOLUME = 50000            # minimum previous day volume
MIN_DOLLAR_VOLUME = 500000    # minimum previous day dollar volume
PRICE_MIN = 0.25              # minimum price
PRICE_MAX = 20.0              # maximum price

# Entry — confirmation logic
ENTRY_CONFIRMATION = True     # wait for 2nd bar to confirm pullback low holds

# Stop loss — ATR based
STOP_LOSS_ATR_MULT = 2.0     # stop = entry - ATR_MULT * ATR (fallback: 20%)
STOP_LOSS_PCT_FALLBACK = 0.20 # fallback fixed stop if ATR unavailable

# Profit targets
PROFIT_RETRACEMENT_75 = 0.75  # 75% retracement — start trailing stop
PROFIT_RETRACEMENT_150 = 1.50 # 150% retracement — sell 1/3, then trail rest
PARTIAL_SELL_RATIO = 1/3      # sell 1/3 at 150% target

# Trailing stop — tiered
TRAILING_STOP_PCT_75 = 0.03   # 3% trailing after reaching 75%
TRAILING_STOP_PCT_150 = 0.05  # 5% trailing after reaching 150%

# Gap tiering — Stone strategy (different params by gap size)
GAP_TIER_2_THRESHOLD = 0.50   # 50% gap boundary
GAP_TIER_3_THRESHOLD = 1.00   # 100% gap boundary

# Tier 1: 10%-50% gap
GAP_TIER_1_ATR_MULT = 2.0
GAP_TIER_1_TRAIL_75 = 0.03
GAP_TIER_1_TRAIL_150 = 0.05

# Tier 2: 50%-100% gap (wider stop, tighter trailing)
GAP_TIER_2_ATR_MULT = 2.5
GAP_TIER_2_TRAIL_75 = 0.025
GAP_TIER_2_TRAIL_150 = 0.04

# Tier 3: >100% gap (widest stop, tightest trailing)
GAP_TIER_3_ATR_MULT = 3.0
GAP_TIER_3_TRAIL_75 = 0.015
GAP_TIER_3_TRAIL_150 = 0.03

# Position management — Stone 0.2
MAX_POSITIONS_PER_DAY = 4     # top 4 stocks
MAX_ENTRIES_PER_STOCK = 3     # up to 3 entries per stock (re-entry on pullback)
POSITION_SIZE = 25000         # $25K per entry
INITIAL_CAPITAL = 100000      # $100K total
FORCE_CLOSE_TIME = "15:50"

# Re-entry parameters
REENTRY_PRICE_TOLERANCE = 0.02  # 2% tolerance for re-entry near first pullback

# Short selling (very conservative)
SHORT_GAP_THRESHOLD = 0.20    # gap down > 20% for shorts
SHORT_MIN_VOLUME = 200000     # higher volume requirement for shorts
SHORT_MIN_DOLLAR_VOLUME = 1000000  # higher liquidity for shorts
SHORT_PRICE_MIN = 2.0         # avoid penny stocks for shorts
SHORT_PRICE_MAX = 20.0
SHORT_STOP_PCT = 0.10         # 10% stop for shorts
SHORT_TARGET_PCT = 0.15       # 15% target for shorts
SHORT_BOUNCE_MIN_RETRACE = 0.50  # bounce must retrace at least 50% of gap

# Market hours (EST)
MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"

# Backtest parameters
BACKTEST_DAYS = 180
