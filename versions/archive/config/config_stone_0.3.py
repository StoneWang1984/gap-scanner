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
GAP_THRESHOLD = 0.20          # gap > 20% (仅要求>20%，不再分级)
MIN_VOLUME = 50000            # minimum previous day volume (盘前成交量)
MIN_DOLLAR_VOLUME = 500000    # minimum previous day dollar volume
PRICE_MIN = 0.25
PRICE_MAX = 20.0

# Entry — confirmation logic
ENTRY_CONFIRMATION = True

# Stop loss — ATR based
STOP_LOSS_ATR_MULT = 2.0
STOP_LOSS_PCT_FALLBACK = 0.20

# Profit targets
PROFIT_RETRACEMENT_75 = 0.75
PROFIT_RETRACEMENT_150 = 1.50

# Dynamic partial profit — Stone 0.3
PARTIAL_SELL_RATIO_75 = 0.25   # sell 1/4 at 75% target
PARTIAL_SELL_RATIO_150 = 1/3   # sell 1/3 of remaining at 150% target

# Trailing stop
TRAILING_STOP_PCT_75 = 0.03
TRAILING_STOP_PCT_150 = 0.05

# Position management — Stone 0.3
MAX_POSITIONS_PER_DAY = 3
EQUITY_POSITION_RATIO = 0.80   # total deployable = current equity × 80%
MAX_POSITION_SIZE = 100000     # per-stock position cap ($100K)
MIN_POSITION_SIZE = 250        # minimum position size floor
INITIAL_CAPITAL = 1000
FORCE_CLOSE_TIME = "15:50"

# Market hours (EST)
MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"

# Backtest parameters
BACKTEST_DAYS = 180
