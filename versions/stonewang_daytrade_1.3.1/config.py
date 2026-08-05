"""Config — stonewang_daytrade_1.3.1: phased trailing stop strategy.

Exit: phased trailing stop (wide→tight)
- Entry: place 10% trailing stop (= initial stop loss 10%)
- After gain >5%: cancel old trail, place 3% trailing stop (lock profits)
- Only ONE trailing stop active at any time
- EOD 15:50 force close
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

# ── Scanner ──
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

# ── Exit: Phased trailing stop ──
WIDE_TRAIL_PCT = 10.0       # 入场时 trailing stop 宽度% (= 初始止损10%)
TIGHT_TRAIL_PCT = 3.0       # 收紧后 trailing stop 宽度%
TIGHTEN_AFTER_PCT = 5.0     # 盈利达到此%后收紧 trailing
TIME_LIMIT_BARS = 8         # 无盈利时 breakeven 退出 (8×5min=40min)

# ── Position management ──
MAX_POSITIONS = 0             # 0=不限, 全力部署购买力
MAX_POSITIONS_PER_DAY = 0    # 0=不限
MIN_POSITION_SIZE = 40        # 最小仓位$40
MAX_POSITION_SIZE = 100       # 最大仓位$100

# ── EOD / Circuit breaker ──
FORCE_CLOSE_TIME = "15:50"    # 强制平仓时间 EST
MAX_DAILY_LOSS_PCT = 0.05     # 日亏5%熔断
MAX_DAILY_PROFIT_PCT = 0      # 止盈熔断已禁用

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

# ── Invariant checker ──
INVARIANT_CHECK_INTERVAL = 4

# ── Version ──
VERSION = "stonewang_daytrade_1.3.1"
