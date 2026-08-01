# rossway_daytrade_0.1B Configuration
# 量价齐升 + 纯trailing stop 2%

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
MAX_ENTRY_SLIPPAGE = 0.04
ENTRY_BUFFER_PCT = 0.01
ENTRY_WINDOW_START = "09:31"
ENTRY_WINDOW_END = "10:00"
MAX_CANDIDATES = 20
VOLUME_RATIO_MIN = 1.5  # 确认bar放量≥前5bar均量1.5倍

# ── Exit: 纯trailing stop ──
TRAILING_STOP_PCT = 0.02   # 2% trailing stop

# ── Position management ──
MAX_POSITIONS = 5
MIN_POSITION_SIZE = 40
MAX_POSITION_SIZE = 200

# ── EOD / Circuit breaker ──
FORCE_CLOSE_TIME = "15:50"
MAX_DAILY_LOSS_PCT = 0.05

# ── Trading ──
DRY_RUN = False
POLL_INTERVAL = 3
DATA_FEED = "SIP"
DATA_FEED_OBJ = None

# ── Rescan ──
RESCAN_TIMES = ["10:30", "11:30"]

# ── Backtest ──
INITIAL_CAPITAL = 500
BACKTEST_DAYS = 5
EQUITY_POSITION_RATIO = 1.0

# ── Version ──
VERSION = "rossway_daytrade_0.1B"
