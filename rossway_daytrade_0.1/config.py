# rossway_daytrade_0.1 Configuration
# 简洁交易系统: 一次入场、一个OCO、固定盈亏比2.5:1

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

# ── Exit: OCO单 ──
STOP_LOSS_MIN_CENTS = 0.15   # 最低15美分止损
STOP_LOSS_PCT = 0.015        # 1.5%止损
REWARD_RISK_RATIO = 2.5      # 止盈 = 止损 × 2.5
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
VERSION = "rossway_daytrade_0.1"
