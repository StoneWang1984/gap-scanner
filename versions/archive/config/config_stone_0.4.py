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
