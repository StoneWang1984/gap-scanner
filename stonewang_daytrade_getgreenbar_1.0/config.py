"""Config — stonewang_daytrade_getgreenbar_1.0: 连续绿bar骑乘策略

策略:
  - 盘前扫描gap-up股票（复用rtg_1.0 scanner）
  - WebSocket trades实时流 → 逐笔构建1-min bar → 实时判断绿/红
  - 入场: 连续绿bar序列起点（上一根红 + 当前变绿 + 放量 + > open_price）
  - 退出: bar从绿变红 / 硬止损 / 追踪止损 / 目标
  - 退出一根红bar后可再入场（每段连续绿bar是独立冲量）
  - 全天监控 09:30-15:30
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# Alpaca API
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER = False  # False = live account
DRY_RUN = False
ALPACA_BASE_URL = "https://api.alpaca.markets"
ALPACA_DATA_URL = "https://data.alpaca.markets"

# Data feed: SIP
DATA_FEED = "sip"
from alpaca.data.enums import DataFeed as _DF
DATA_FEED_OBJ = _DF.SIP

# ── Scanner filters (与rtg_1.0相同) ────────────────────────────────
GAP_THRESHOLD = 0.10   # min 10% gap
GAP_MAX = 1.0          # max 100% gap
MIN_VOLUME = 10000     # min pre-market volume
MIN_DOLLAR_VOLUME = 100000
PRICE_MIN = 1.0
PRICE_MAX = 20.0
LEVERAGED_ETF_SUFFIXES = ("BULL", "BEAR")
LEVERAGED_ETF_PREFIXES = ()

# ── 候选股 ────────────────────────────────────────────────────────
MAX_CANDIDATES = 5
RVOL_LOOKBACK_DAYS = 20

# ── RVOL-weighted position sizing (与rtg_1.0相同) ────────────────
RVOL_SIZING_TIERS = [
    (10.0, 0.50),   # RVOL > 10× → 50% of equity
    (5.0,  0.35),   # RVOL 5-10× → 35% of equity
    (0.0,  0.20),   # RVOL < 5× → 20% of equity
]

# ── RVOL-adaptive exit tiers (与rtg_1.0相同) ────────────────────
RVOL_EXIT_TIERS = [
    (10.0, 0.07, 0.50, 0.05, 0.05),  # High RVOL: 7% stop, 50% target, trail +5%/5%
    (5.0,  0.05, 0.30, 0.04, 0.04),  # Medium: 5% stop, 30% target, trail +4%/4%
    (0.0,  0.03, 0.15, 0.03, 0.02),  # Low: 3% stop, 15% target, trail +3%/2%
]

# ── 绿bar入场参数 ──────────────────────────────────────────────────
GBAR_VOLUME_MULT = 1.5        # 当前bar volume > 前一根bar × 1.5（放量确认）
GBAR_MIN_VOLUME = 10000       # 当前bar最低成交量（噪声过滤）
GBAR_MIN_PRICE_GAIN = 0.0     # 当前bar close相对open_price的最低涨幅
GBAR_ENTRY_AT_SIGNAL = True   # True=在信号bar的close入场
GBAR_MIN_TRADES_IN_BAR = 5    # bar内至少5笔成交才判断方向
GBAR_REENTRY_COOLDOWN_SEC = 60 # 同股退出后60秒冷却

# ── 绿bar退出参数 ──────────────────────────────────────────────────
GBAR_STOP_PCT = 0.05          # 5%硬止损
GBAR_TRAIL_ACTIVATE_PCT = 0.03 # +3%激活追踪
GBAR_TRAIL_PCT = 0.02          # 2%追踪止损
GBAR_TARGET_PCT = 0.30         # 30%目标
GBAR_EXIT_ON_RED_BAR = True    # bar从绿变红时退出（核心退出逻辑）
GBAR_RED_BAR_CONFIRM_TRADES = 5 # 红bar需至少5笔成交确认

# ── 入场窗口 ──────────────────────────────────────────────────────
ENTRY_WINDOW_START = "09:30"
ENTRY_WINDOW_END = "15:30"    # 全天监控绿bar序列

# ── 仓位管理 ──────────────────────────────────────────────────────
INITIAL_CAPITAL = 390.04
MIN_POSITION_SIZE = 40
MAX_POSITION_SIZE = 9999
MAX_POSITIONS = 2              # 最多同时2只
MAX_DAILY_TRADES = 20          # 每日最多20笔
MAX_DAILY_ENTRIES_PER_SYMBOL = 6  # 同股最多6次入场
MAX_DAILY_LOSS_PCT = 0.04     # 4%日亏损熔断
EQUITY_POSITION_RATIO = 1.0

# ── Market hours ──────────────────────────────────────────────────
MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"
FORCE_CLOSE_TIME = "15:50"

# ── Live trading ──────────────────────────────────────────────────
USE_WEBSOCKET = True
POLL_INTERVAL = 3
DRY_RUN_POLL_INTERVAL = 5

# ── Slippage model ────────────────────────────────────────────────
SLIPPAGE_ENTRY_PCT = 0.005
SLIPPAGE_EXIT_PCT = 0.005
SLIPPAGE_FORCE_CLOSE_PCT = 0.01

# ── Order parameters ──────────────────────────────────────────────
ENTRY_LIMIT_BUFFER = 0.005
STOP_LIMIT_BUFFER = 0.02
FORCE_CLOSE_LIMIT_TIMEOUT = 60

# ── Backtest ──────────────────────────────────────────────────────
BACKTEST_DAYS = 30

# ── Version ──────────────────────────────────────────────────────
VERSION = "stonewang_daytrade_getgreenbar_1.0"
VERSION_SHORT = "getgreenbar_1.0"
