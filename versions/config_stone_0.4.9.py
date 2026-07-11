"""Config — Stone 0.4.9: relaxed scanner filters for broader gap coverage.

Changes over 0.4.5:
- PRICE_MAX: 20 → 30 (capture more mid-cap gap stocks)
- MIN_VOLUME: 50000 → 10000 (lower volume threshold)
- MIN_DOLLAR_VOLUME: 500000 → 100000 (lower dollar volume threshold)
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# Alpaca API
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"
ALPACA_DATA_URL = "https://data.alpaca.markets"

# Scanner filters — 0.4.9: relaxed thresholds
GAP_THRESHOLD = 0.10
MIN_VOLUME = 10000
MIN_DOLLAR_VOLUME = 100000
PRICE_MIN = 0.25
PRICE_MAX = 30.0

# ── Stone 0.4.5: Exclude leveraged ETFs ──────────────────────────────
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

# Trailing stop — first trade (0.4.5: 75% tier kept at 3%)
TRAILING_STOP_PCT_75 = 0.03
TRAILING_STOP_PCT_1125 = 0.04
TRAILING_STOP_PCT_150 = 0.05

# ── Re-entry trade — v2 (0.4.9: redesigned) ──────────────────────────
REENTRY_POSITION_RATIO = 0.5            # half position vs first trade
REENTRY_STOP_PCT = 0.05                 # legacy fallback
REENTRY_STOP_ATR_MULT = 1.5             # ATR-based stop multiplier
REENTRY_STOP_PCT_FALLBACK = 0.04        # fallback when ATR unavailable
REENTRY_PROFIT_RETRACEMENT = 1.50       # legacy 150% target
REENTRY_PROFIT_RETRACEMENT_1 = 0.75     # v2 tier-1: sell 1/2 at 75% retracement
REENTRY_SELL_RATIO = 1/3                # legacy
REENTRY_SELL_RATIO_1 = 0.5             # v2 tier-1: sell 1/2 of position
REENTRY_TRAILING_PCT = 0.05             # legacy
REENTRY_TRAILING_PCT_2 = 0.03           # v2 tier-2: 3% trailing after tier-1
REENTRY_CUTOFF_TIME = "12:30"           # no re-entries after 12:30 PM EST
REENTRY_MAX_BARS_BEFORE_TARGET = 6      # time stop: exit if no tier-1 in 6 bars (30 min)
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
