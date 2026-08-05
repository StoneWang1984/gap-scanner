"""Stone 1.2 — Main config (synced with stonewang_daytrade_1.2/config.py)"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# Alpaca API
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER = False  # False = live account, True = paper trading
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

# Stop loss — ATR based (first trade)
STOP_LOSS_ATR_MULT = 2.0
STOP_LOSS_PCT_FALLBACK = 0.20

# Stop loss max cap — ATR/fallback can produce very wide stops;
# cap the maximum loss from entry at this percentage (aligned with backtest)
STOP_LOSS_MAX_PCT = 0.10

# P1-12: Configurable ATR stop bounds (replaces hardcoded 0.70/0.95)
STOP_LOSS_ATR_MIN_PCT = 0.70  # min stop = entry * (1 - this) → max 30% loss from ATR
STOP_LOSS_ATR_MAX_PCT = 0.95  # max stop = entry * (1 - this) → min 5% loss from ATR

# Profit targets — first trade (eight tiers)
PROFIT_RETRACEMENT_TIERS = [0.10, 0.20, 0.35, 0.50, 0.75, 1.00, 1.25, 1.50]
MIN_RETRACE_PCT = 0.03    # minimum 3% gap for retracement mode; below this, force capped mode
TARGET_CAP_TIERS =         [0.01, 0.02, 0.035, 0.05, 0.08, 0.10, 0.13, 0.18]
PARTIAL_SELL_RATIOS =      [1/8,  1/8,  1/8,  1/8,  1/8,  1/8,  1/8,  1/8]   # 8×1/8 = 100%
TRAILING_STOP_PCTS =       [0.01, 0.015, 0.02, 0.025, 0.03, 0.035, 0.04, 0.05]

# DEPRECATED — Legacy aliases for old 3-tier system. New code uses 8-tier params above.
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

# 0.4.11: Time limit exit — if no target hit within this many 5-min bars,
# sell all remaining shares when price >= entry price (0 = disabled)
FIRST_TRADE_TIME_LIMIT_BARS = 8  # 8 bars × 5 min = 40 minutes

# ── Re-entry trade — simplified: 1-min entry + trailing 1% ───
REENTRY_POSITION_RATIO = 1.0            # v1.3: full slot for re-entry (maximize capital efficiency)
REENTRY_STOP_PCT = 0.05                 # ATR不可用时的止损回退百分比
REENTRY_STOP_ATR_MULT = 1.5             # ATR-based stop multiplier
REENTRY_STOP_PCT_FALLBACK = 0.04        # ATR止损保底4%
REENTRY_PROFIT_RETRACEMENT_1 = 0.75     # target = entry + 75% × (peak - entry)
REENTRY_TRAILING_PCT = 0.01             # 1% trailing stop after target reached
REENTRY_CUTOFF_TIME = "12:30"           # no re-entries after 12:30 PM EST
RESCAN_TIMES = ["10:30", "11:30"]       # v1.3: mid-day re-scan times (EST)
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
EQUITY_POSITION_RATIO = 1.0

# Invariant checker — 实盘状态一致性验证
INVARIANT_CHECK_INTERVAL = 4  # 每4轮轮询检查一次（避免API限频）
MAX_POSITION_SIZE = 100  # max $100 per position
MIN_POSITION_SIZE = 40   # minimum $40 per position (first trade)
REENTRY_MIN_POSITION_SIZE = 20  # minimum $20 per position (re-entry uses half position)
FORCE_QTY = 0  # 0 = dynamic position sizing based on equity; >0 = fixed shares (test mode)
INITIAL_CAPITAL = 500
FORCE_CLOSE_TIME = "15:50"

# ── Live trading parameters ──────────────────────────────────────────────
DRY_RUN = False               # True = simulate orders, no real trades; use --dry-run CLI flag
MAX_CANDIDATES = 20            # monitor up to 20 candidates

# ── Slippage model ──────────────────────────────────────────────────────
SLIPPAGE_ENTRY_PCT = 0.005
SLIPPAGE_STOP_PCT = 0.02
SLIPPAGE_TRAILING_PCT = 0.01
SLIPPAGE_TARGET_PCT = 0.003
SLIPPAGE_FORCE_CLOSE_PCT = 0.01
SLIPPAGE_REENTRY_STOP_PCT = 0.025

# ── Live trading order parameters ──────────────────────────────────────
ENTRY_LIMIT_BUFFER = 0.005
MAX_ENTRY_SLIPPAGE = 0.10     # reject buy if ask > entry_price × (1 + this); 0 = no check
STOP_LIMIT_BUFFER = 0.03
FORCE_CLOSE_LIMIT_TIMEOUT = 120

# ── Phased trailing stop (v1.3.1) ──────────────────────────────────────
WIDE_TRAIL_PCT = 10.0       # 入场时 trailing stop 宽度% (= 初始止损10%)
TIGHT_TRAIL_PCT = 3.0       # 收紧后 trailing stop 宽度%
TIGHTEN_AFTER_PCT = 5.0     # 盈利达到此%后收紧 trailing
TIME_LIMIT_BARS = 8         # 无盈利时 breakeven 退出 (8×5min=40min)

# ── Polling interval ──────────────────────────────────────────────────
POLL_INTERVAL = 3  # seconds (reduced from 5 after batch order cache optimization)

# WebSocket real-time streaming
USE_WEBSOCKET = True

# Market hours (EST)
MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"

# Backtest parameters
BACKTEST_DAYS = 180
