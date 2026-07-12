"""Config — Stone 0.4.12: TARGET_150 full exit + re-entry two-tier percentage targets.

Changes over 0.4.11:
- First trade: TARGET_150 sells all remaining shares (no trailing after 150%)
- Re-entry: sell 50% at +3%, sell remaining 50% at +5%
- Re-entry time limit: 1 hour (12 bars), sell at breakeven if no tier-1 hit
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

# Scanner filters — 0.4.10: tightened price range
GAP_THRESHOLD = 0.10
MIN_VOLUME = 10000
MIN_DOLLAR_VOLUME = 100000
PRICE_MIN = 1.0
PRICE_MAX = 20.0

# ── Leveraged ETF exclusion ──────────────────────────────────────────
LEVERAGED_ETF_SUFFIXES = ("U", "L", "S", "BULL", "BEAR")
LEVERAGED_ETF_PREFIXES = ()

# Entry — confirmation logic
ENTRY_CONFIRMATION = True

# 0.4.11: Skip first trade if entry price >= open price
ENTRY_BELOW_OPEN = True

# 0.4.11: Time limit exit — if no target hit within this many 5-min bars,
# sell all remaining shares when price >= entry price (0 = disabled)
FIRST_TRADE_TIME_LIMIT_BARS = 8  # 8 bars × 5 min = 40 minutes

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

# ── Re-entry trade — 0.4.12: two-tier percentage targets ────────────────
REENTRY_POSITION_RATIO = 0.5            # half position vs first trade
REENTRY_STOP_PCT = 0.05                 # legacy fallback
REENTRY_STOP_ATR_MULT = 1.5             # ATR-based stop multiplier
REENTRY_STOP_PCT_FALLBACK = 0.04        # fallback when ATR unavailable
REENTRY_PROFIT_PCT_1 = 0.03             # tier-1: sell 50% at +3%
REENTRY_PROFIT_PCT_2 = 0.05             # tier-2: sell remaining at +5%
REENTRY_SELL_RATIO_1 = 0.5              # sell 50% at tier-1
REENTRY_TIME_LIMIT_BARS = 12            # 1 hour = 12 × 5min bars; sell at breakeven if no tier-1
REENTRY_CUTOFF_TIME = "12:30"           # no re-entries after 12:30 PM EST
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

# ── Slippage model ───────────────────────────────────────────────────
SLIPPAGE_ENTRY_PCT = 0.005
SLIPPAGE_STOP_PCT = 0.02
SLIPPAGE_TRAILING_PCT = 0.01
SLIPPAGE_TARGET_PCT = 0.003
SLIPPAGE_FORCE_CLOSE_PCT = 0.01
SLIPPAGE_REENTRY_STOP_PCT = 0.025

# ── Live trading order parameters ────────────────────────────────────
ENTRY_LIMIT_BUFFER = 0.005
STOP_LIMIT_BUFFER = 0.03
FORCE_CLOSE_LIMIT_TIMEOUT = 120
