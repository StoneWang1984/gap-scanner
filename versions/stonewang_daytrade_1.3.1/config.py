"""Config — stonewang_daytrade_1.3.1: hybrid 1.2.0 entry + rossway 0.1wp stop/target.

Changes from 1.3.0 → 1.3.1:
- Entry: revert to 1.2.0's 3-bar confirmation (remove hammer bar / 2-tier fast confirm)
- Exit: replace 8-tier ladder with single OCO using rossway 0.1wp's STOP_TIERS
- Added STOP_TIERS for tiered stop/target by price range
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

# ── Alpaca API ──
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER = False  # LIVE account
ALPACA_BASE_URL = "https://api.alpaca.markets"
ALPACA_DATA_URL = "https://data.alpaca.markets"

# ── Data feed ──
DATA_FEED = "SIP"
from alpaca.data.enums import DataFeed as _DF
DATA_FEED_OBJ = _DF.SIP

# ── Scanner (继承 stonewang 1.3) ──
GAP_THRESHOLD = 0.10   # min 10% gap
GAP_MAX = 1.0          # max 100% gap
MIN_VOLUME = 10000
MIN_DOLLAR_VOLUME = 100000
PRICE_MIN = 1.0
PRICE_MAX = 20.0
LEVERAGED_ETF_SUFFIXES = ("BULL", "BEAR")

# ── Entry ──
ENTRY_CONFIRMATION = True
ENTRY_BUFFER_PCT = 0.01     # 买入限价 = pullback_price × (1 + buffer)
MAX_ENTRY_SLIPPAGE = 0.04   # 限价单最大滑点4%
ENTRY_WINDOW_START = "09:31"
ENTRY_WINDOW_END = "10:00"
MAX_CANDIDATES = 20

# ── Exit: OCO单 (按价格分档, rossway 0.1wp) ──
USE_TIERED_STOP_TARGET = True
# (min_price, max_price, stop_pct, target_pct)
STOP_TIERS = [
    (1,  2,  0.06,  0.12),    # $1-2:  止损6%,  止盈12%   (R:R 2.0)
    (2,  3,  0.05,  0.10),    # $2-3:  止损5%,  止盈10%   (R:R 2.0)
    (3,  4,  0.04,  0.06),    # $3-4:  止损4%,  止盈6%    (R:R 1.5)
    (4,  5,  0.03,  0.045),   # $4-5:  止损3%,  止盈4.5%  (R:R 1.5)
    (5,  10, 0.02,  0.04),    # $5-10: 止损2%,  止盈4%    (R:R 2.0)
    (10, 15, 0.015, 0.035),   # $10-15:止损1.5%,止盈3.5%   (R:R 2.3)
    (15, 20, 0.0125,0.02),    # $15-20:止损1.25%,止盈2%   (R:R 1.6)
]
STOP_LIMIT_BUFFER = 0.03     # stop-limit 3% buffer

# ── Position management ──
MAX_POSITIONS = 5             # 最多5个同时持仓
MAX_POSITIONS_PER_DAY = 5    # (backtest compat)
MIN_POSITION_SIZE = 40        # 最小仓位$40
MAX_POSITION_SIZE = 100       # 最大仓位$100

# ── EOD / Circuit breaker ──
FORCE_CLOSE_TIME = "15:50"    # 强制平仓时间 EST
MAX_DAILY_LOSS_PCT = 0.05     # 日亏5%熔断

# ── Trading ──
DRY_RUN = False
POLL_INTERVAL = 3             # 轮询间隔秒
EQUITY_POSITION_RATIO = 1.0
MAX_DAILY_TRADES = 0          # 0 = no limit

# ── Re-entry / Rescan ──
REENTRY_POSITION_RATIO = 1.0
REENTRY_MIN_PULLBACK = 0.04
REENTRY_CUTOFF_TIME = "12:30"
RESCAN_TIMES = ["10:30", "11:30"]

# ── Backtest parameters (for backtest.py compat) ──
INITIAL_CAPITAL = 500
BACKTEST_DAYS = 180
ENTRY_BELOW_OPEN = True
FIRST_TRADE_TIME_LIMIT_BARS = 8
STOP_LOSS_ATR_MULT = 2.0
STOP_LOSS_PCT_FALLBACK = 0.20
STOP_LOSS_MAX_PCT = 0.10
STOP_LOSS_ATR_MIN_PCT = 0.70
STOP_LOSS_ATR_MAX_PCT = 0.95
PROFIT_RETRACEMENT_TIERS = [0.10, 0.20, 0.35, 0.50, 0.75, 1.00, 1.25, 1.50]
MIN_RETRACE_PCT = 0.03
TARGET_CAP_TIERS =         [0.01, 0.02, 0.035, 0.05, 0.08, 0.10, 0.13, 0.18]
PARTIAL_SELL_RATIOS =      [1/8,  1/8,  1/8,  1/8,  1/8,  1/8,  1/8,  1/8]
TRAILING_STOP_PCTS =       [0.01, 0.015, 0.02, 0.025, 0.03, 0.035, 0.04, 0.05]
REENTRY_STOP_PCT = 0.05
REENTRY_STOP_ATR_MULT = 1.5
REENTRY_STOP_PCT_FALLBACK = 0.04
REENTRY_PROFIT_RETRACEMENT_1 = 0.75
REENTRY_TRAILING_PCT = 0.01
REENTRY_MIN_POSITION_SIZE = 20
PULLBACK_STOP_THRESHOLD = 0.15
FORCE_QTY = 0
USE_WEBSOCKET = True
MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"

# ── Slippage model (backtest) ──
SLIPPAGE_ENTRY_PCT = 0.005
SLIPPAGE_ENTRY_BASE = 0.01
SLIPPAGE_ENTRY_GAP_FACTOR = 0.15
SLIPPAGE_ENTRY_MAX = 0.05
SLIPPAGE_STOP_PCT = 0.02
SLIPPAGE_TRAILING_PCT = 0.01
SLIPPAGE_TARGET_PCT = 0.003
SLIPPAGE_FORCE_CLOSE_PCT = 0.01
SLIPPAGE_REENTRY_STOP_PCT = 0.025

# ── Live trading order parameters ──
ENTRY_LIMIT_BUFFER = 0.01
STOP_LOSS_MIN_CENTS = 0.15

# ── OCO (legacy compat for backtest.py) ──
OCO_ENABLED = True
OCO_STOP_BUFFER_PCT = 0.02

# ── Invariant checker ──
INVARIANT_CHECK_INTERVAL = 4

# ── Version ──
VERSION = "stonewang_daytrade_1.3.1"
