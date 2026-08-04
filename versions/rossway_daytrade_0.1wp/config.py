# rossway_daytrade_0.1wp Configuration
# 简洁交易系统: 一次入场、一个OCO、按价格分档止损止盈

import os
from dotenv import load_dotenv
load_dotenv("/Users/stonewang2014/gap-scanner/.env")

# ── Alpaca API ──
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER = False  # LIVE account

# ── Scanner (继承 stonewang 1.3) ──
GAP_THRESHOLD = 0.10
GAP_MAX = 1.0
MIN_VOLUME = 10000
MIN_DOLLAR_VOLUME = 100000
PRICE_MIN = 1.0
PRICE_MAX = 20.0
LEVERAGED_ETF_SUFFIXES = ("BULL", "BEAR")

# ── Entry ──
ENTRY_CONFIRMATION = True
MAX_ENTRY_SLIPPAGE = 0.04   # 限价单最大滑点4%
ENTRY_BUFFER_PCT = 0.01     # 买入限价 = pullback_price × (1 + buffer)
ENTRY_WINDOW_START = "09:31"
ENTRY_WINDOW_END = "10:00"
MAX_CANDIDATES = 20

# ── Exit: OCO单 (按价格分档) ──
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
STOP_LIMIT_BUFFER = 0.03     # stop-limit 3% buffer (stop_limit = stop_price × (1 - buffer))

# ── Position management ──
MAX_POSITIONS = 5             # 最多5个同时持仓
MIN_POSITION_SIZE = 40        # 最小仓位$40
MAX_POSITION_SIZE = 200       # 最大仓位$200

# ── EOD / Circuit breaker ──
FORCE_CLOSE_TIME = "15:50"    # 强制平仓时间 EST
MAX_DAILY_LOSS_PCT = 0.05     # 日亏5%熔断

# ── Trading ──
DRY_RUN = False
POLL_INTERVAL = 3             # 轮询间隔秒
DATA_FEED = "SIP"             # SIP or IEX
DATA_FEED_OBJ = None          # Set by scanner import; None = use default

# ── Rescan ──
RESCAN_TIMES = ["10:30", "11:30"]

# ── Backtest ──
INITIAL_CAPITAL = 500
BACKTEST_DAYS = 5
EQUITY_POSITION_RATIO = 1.0

# ── Version ──
VERSION = "rossway_daytrade_0.1wp"
