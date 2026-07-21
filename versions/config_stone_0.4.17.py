"""Config — Stone 0.4.17: based on 0.4.14 + operational fixes.

Changes over 0.4.14:
- Same config parameters, version bump for clean tracking
- Daily loss circuit breaker: 5% (MAX_DAILY_LOSS_PCT = 0.05)
- Re-entry min pullback: 3% from peak (REENTRY_MIN_PULLBACK = 0.03)
- Scanner: PRICE_MIN = $1.0, MIN_VOL = 10K, MIN_$VOL = $100K (aligned with 0.4.10)
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# Alpaca API
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER = False  # False = live account, True = paper trading
ALPACA_BASE_URL = "https://api.alpaca.markets"  # live account
ALPACA_DATA_URL = "https://data.alpaca.markets"

# Data feed: IEX for live trading (SIP requires real-time subscription)
# IEX (SIP subscription currently unavailable)
DATA_FEED = "sip"
from alpaca.data.enums import DataFeed as _DF
DATA_FEED_OBJ = _DF.SIP

# Scanner filters — aligned with 0.4.10
GAP_THRESHOLD = 0.10   # min 10% gap
GAP_MAX = 1.0          # max 100% gap — filters reverse splits & extreme gaps
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

# 0.4.11: Time limit exit — if no target hit within this many 5-min bars,
# sell all remaining shares when price >= entry price (0 = disabled)
FIRST_TRADE_TIME_LIMIT_BARS = 8  # 8 bars × 5 min = 40 minutes

# Stop loss — ATR based (first trade)
STOP_LOSS_ATR_MULT = 2.0
STOP_LOSS_PCT_FALLBACK = 0.20

# 0.4.14: Stop loss max cap — ATR/fallback can produce very wide stops;
# cap the maximum loss from entry at this percentage
STOP_LOSS_MAX_PCT = 0.10

# Profit targets — first trade (six tiers)
PROFIT_RETRACEMENT_TIERS = [0.25, 0.50, 0.75, 1.00, 1.25, 1.50]
TARGET_CAP_TIERS =         [0.05, 0.10, 0.15, 0.20, 0.25, 0.35]
PARTIAL_SELL_RATIOS =      [1/8,  1/8,  1/8,  1/8,  1/8,  1/8]   # 6×1/8 = 75%
TRAILING_STOP_PCTS =       [0.02, 0.025, 0.03, 0.035, 0.04, 0.05]

# Legacy aliases (for backward compat)
PROFIT_RETRACEMENT_75 = 0.75
PROFIT_RETRACEMENT_1125 = 1.125
PROFIT_RETRACEMENT_150 = 1.50
TARGET_CAP_TIER1 = 0.15
TARGET_CAP_TIER2 = 0.25
TARGET_CAP_TIER3 = 0.35
PARTIAL_SELL_RATIO_75 = 0.25
PARTIAL_SELL_RATIO_1125 = 1/3
PARTIAL_SELL_RATIO_150 = 1/3
TRAILING_STOP_PCT_75 = 0.03
TRAILING_STOP_PCT_1125 = 0.04
TRAILING_STOP_PCT_150 = 0.05

# ── Re-entry trade — 0.4.14: retracement + trailing, no time limit ───
REENTRY_POSITION_RATIO = 0.5            # half position vs first trade
REENTRY_STOP_PCT = 0.05                 # legacy fallback (unused by v2)
REENTRY_STOP_ATR_MULT = 1.5             # ATR-based stop multiplier
REENTRY_STOP_PCT_FALLBACK = 0.04        # fallback when ATR unavailable
REENTRY_PROFIT_RETRACEMENT_1 = 0.75     # v2 tier-1: sell 1/2 at 75% retracement
REENTRY_SELL_RATIO_1 = 0.5             # v2 tier-1: sell 1/2 of position
REENTRY_TRAILING_PCT_2 = 0.03           # v2 tier-2: 3% trailing after tier-1
REENTRY_CUTOFF_TIME = "12:30"           # no re-entries after 12:30 PM EST
REENTRY_MAX_BARS_BEFORE_TARGET = 0      # 0.4.13: no time stop (removed)

# 0.4.14: Minimum pullback from peak before re-entry
# Prevents re-entering during shallow pullbacks / choppy consolidation
REENTRY_MIN_PULLBACK = 0.03

PULLBACK_STOP_THRESHOLD = 0.15

# ── 0.4.14: Daily loss circuit breaker ───────────────────────────────
# Stop trading for the day if cumulative daily loss exceeds this % of equity
MAX_DAILY_LOSS_PCT = 0.05

# Position management
MAX_POSITIONS_PER_DAY = 5
MAX_DAILY_TRADES = 5
EQUITY_POSITION_RATIO = 0.80
MAX_POSITION_SIZE = 100000
MIN_POSITION_SIZE = 1  # Test mode: allow small positions
FORCE_QTY = 8  # 8 shares: each tier sells 1 share (8×1/8=1), full 6-tier verification
INITIAL_CAPITAL = 500
FORCE_CLOSE_TIME = "15:50"

# WebSocket real-time streaming
USE_WEBSOCKET = True  # True = use WS for instant triggers, False = snapshot polling only

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
