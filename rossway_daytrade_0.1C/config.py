# rossway_daytrade_0.1C Configuration
# 量价齐升 + 两段式退出 (OCO半仓止盈 + Trailing Stop 3%)

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
VOLUME_RATIO_MIN = 1.5  # 量价齐升: 确认bar放量≥前5bar均量1.5倍

# ── Exit Phase 1: OCO (保护全部仓位) ──
STOP_LOSS_MIN_CENTS = 0.15   # 最低15美分止损
STOP_LOSS_PCT = 0.015        # 1.5%止损
REWARD_RISK_RATIO = 2.5      # 止盈 = 止损 × 2.5
TP_SELL_RATIO = 0.5          # 止盈卖出50%仓位

# ── Exit Phase 2: Trailing stop (止盈后激活) ──
TRAILING_STOP_PCT = 0.03     # 3% trailing stop (比0.1B的2%更宽)

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
VERSION = "rossway_daytrade_0.1C"
