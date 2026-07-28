"""Config — stonewang_daytrade_1.1: ladder sell lock-up fix release.

Changes from stonewang_daytrade_1.0 → 1.1:
- No config parameter changes — only live_trade.py logic fixes.

Changes from Stone 1.0 → 1.1:
- P0-1: STOP_LOSS_MAX_PCT = 0.10 (was 0.12, align with backtest/CLAUDE.md)
- P0-1: TRAILING_STOP_PCTS aligned with config.py (2.0%/2.5%/3.0%/3.5%/4.0%/5.0%)
- P0-1: MIN_POSITION_SIZE = 250 (was 1, eliminates tiny positions)
- P0-2: LEVERAGED_ETF_SUFFIXES removed single-letter L/U (only BULL/BEAR remain)
- P1-12: STOP_LOSS_ATR_MIN_PCT/ATR_MAX_PCT added (configurable stop bounds)
- P2: REENTRY_STOP_PCT comment corrected
- P2: DATA_FEED comment corrected (SIP active)
- P2: Legacy aliases marked DEPRECATED
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# Alpaca API
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER = False  # False = live account, True = paper trading
DRY_RUN = False  # True = simulate orders, no real trades; use --dry-run CLI flag
ALPACA_BASE_URL = "https://api.alpaca.markets"  # live account
ALPACA_DATA_URL = "https://data.alpaca.markets"

# Data feed: SIP subscription active
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

# Leveraged ETF exclusion — single-letter suffixes removed (L/U matched AAPL/GOOGL)
LEVERAGED_ETF_SUFFIXES = ("BULL", "BEAR")
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

# Stop loss max cap — ATR/fallback can produce very wide stops;
# cap the maximum loss from entry at this percentage (aligned with backtest)
STOP_LOSS_MAX_PCT = 0.10

# P1-12: Configurable ATR stop bounds (replaces hardcoded 0.70/0.95)
STOP_LOSS_ATR_MIN_PCT = 0.70  # min stop = entry * (1 - this) → max 30% loss from ATR
STOP_LOSS_ATR_MAX_PCT = 0.95  # max stop = entry * (1 - this) → min 5% loss from ATR

# Profit targets — first trade (six tiers)
PROFIT_RETRACEMENT_TIERS = [0.25, 0.50, 0.75, 1.00, 1.25, 1.50]
TARGET_CAP_TIERS =         [0.05, 0.10, 0.15, 0.20, 0.25, 0.35]
PARTIAL_SELL_RATIOS =      [1/8,  1/8,  1/8,  1/8,  1/8,  1/8]   # 6×1/8 = 75%
TRAILING_STOP_PCTS =       [0.02, 0.025, 0.03, 0.035, 0.04, 0.05]  # aligned with config.py

# DEPRECATED — Legacy aliases for old 3-tier system. New code uses 6-tier params above.
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

# ── Re-entry trade — simplified: 1-min entry + trailing 1% ───
REENTRY_POSITION_RATIO = 0.5            # half position vs first trade
REENTRY_STOP_PCT = 0.05                 # ATR不可用时的止损回退百分比
REENTRY_STOP_ATR_MULT = 1.5             # ATR-based stop multiplier
REENTRY_STOP_PCT_FALLBACK = 0.04        # ATR止损保底4%
REENTRY_PROFIT_RETRACEMENT_1 = 0.75     # target = entry + 75% × (peak - entry)
REENTRY_TRAILING_PCT = 0.01             # 1% trailing stop after target reached
REENTRY_CUTOFF_TIME = "12:30"           # no re-entries after 12:30 PM EST
# DEPRECATED: old 2-tier system params (no longer used)
REENTRY_SELL_RATIO_1 = 0.5             # [DEPRECATED] no partial sells anymore
REENTRY_TRAILING_PCT_2 = 0.03           # [DEPRECATED] replaced by REENTRY_TRAILING_PCT=0.01
REENTRY_MAX_BARS_BEFORE_TARGET = 0      # [DEPRECATED] no time stop

# Minimum pullback from peak before re-entry
# Prevents re-entering during shallow pullbacks / choppy consolidation
REENTRY_MIN_PULLBACK = 0.04

PULLBACK_STOP_THRESHOLD = 0.15

# ── Daily loss circuit breaker ───────────────────────────────
# Stop trading for the day if cumulative PnL (realized + unrealized) exceeds this % of equity
MAX_DAILY_LOSS_PCT = 0.05

# Position management
MAX_POSITIONS_PER_DAY = 5  # max 5 positions held simultaneously
MAX_CANDIDATES = 20        # monitor up to 20 candidates, buy whichever confirms
MAX_DAILY_TRADES = 0       # 0 = no limit, as many trades as buying power allows
EQUITY_POSITION_RATIO = 0.80

# Invariant checker — 实盘状态一致性验证
INVARIANT_CHECK_INTERVAL = 4  # 每4轮轮询检查一次（避免API限频）
MAX_POSITION_SIZE = 100  # max $100 per position
MIN_POSITION_SIZE = 85   # minimum $85 per position
FORCE_QTY = 0  # 0 = dynamic position sizing based on equity; >0 = fixed shares (test mode)
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
MAX_ENTRY_SLIPPAGE = 0.10  # reject buy if ask > entry_price × (1 + this); 0 = no check
STOP_LIMIT_BUFFER = 0.03
FORCE_CLOSE_LIMIT_TIMEOUT = 120
POLL_INTERVAL = 3  # main loop polling interval in seconds (3s = faster than 5s default)
