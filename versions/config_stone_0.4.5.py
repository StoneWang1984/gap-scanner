"""Config — Stone 0.4.5: re-entry cutoff + wider trailing + leveraged ETF filter."""

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
GAP_THRESHOLD = 0.10
MIN_VOLUME = 50000
MIN_DOLLAR_VOLUME = 500000
PRICE_MIN = 0.25
PRICE_MAX = 20.0

# ── Stone 0.4.5: Exclude leveraged ETFs ──────────────────────────────
# Suffixes that indicate leveraged/inverse ETFs (2x, 3x, -1x, -2x, -3x)
LEVERAGED_ETF_SUFFIXES = ("U", "L", "S", "BULL", "BEAR")
LEVERAGED_ETF_PREFIXES = ()

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

# Trailing stop — first trade (0.4.5: 75% tier widened from 3% to 5%)
TRAILING_STOP_PCT_75 = 0.05
TRAILING_STOP_PCT_1125 = 0.04
TRAILING_STOP_PCT_150 = 0.05

# Re-entry trade (0.4.5: added time cutoff)
REENTRY_STOP_PCT = 0.05
REENTRY_PROFIT_RETRACEMENT = 1.50
REENTRY_SELL_RATIO = 1/3
REENTRY_TRAILING_PCT = 0.05
REENTRY_CUTOFF_TIME = "13:00"       # no re-entries after 1:00 PM EST
PULLBACK_STOP_THRESHOLD = 0.15

# Position management
MAX_POSITIONS_PER_DAY = 3
MAX_DAILY_TRADES = 3
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

# ── Slippage model (unchanged from 0.4.4) ────────────────────────────
SLIPPAGE_ENTRY_PCT = 0.005
SLIPPAGE_STOP_PCT = 0.02
SLIPPAGE_TRAILING_PCT = 0.01
SLIPPAGE_TARGET_PCT = 0.003
SLIPPAGE_FORCE_CLOSE_PCT = 0.01
SLIPPAGE_REENTRY_STOP_PCT = 0.025

# ── Live trading order parameters (unchanged from 0.4.4) ─────────────
ENTRY_LIMIT_BUFFER = 0.005
STOP_LIMIT_BUFFER = 0.03
FORCE_CLOSE_LIMIT_TIMEOUT = 120
