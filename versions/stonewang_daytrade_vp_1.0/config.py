"""Config — stonewang_daytrade_vp_1.0: Volume+Price derivative entry.

Entry: WebSocket trades stream → volume velocity × price slope spike → market buy
Exit: drop ≥ 0.5% from entry → market sell; 3-min time limit → market sell
Safety: 5% server-side stop-limit order as WS-death fallback
EOD: 15:50 force close
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

# ── Scanner (unchanged from v1.3.2ts) ──
GAP_THRESHOLD = 0.10   # min 10% gap
GAP_MAX = 1.0          # max 100% gap
MIN_VOLUME = 10000
MIN_DOLLAR_VOLUME = 100000
PRICE_MIN = 1.0
PRICE_MAX = 20.0
LEVERAGED_ETF_SUFFIXES = ("BULL", "BEAR")

# ── Entry window (extended for all-day VP monitoring) ──
ENTRY_WINDOW_START = "09:31"
ENTRY_WINDOW_END = "15:30"   # 全天监控 VP 信号
MAX_CANDIDATES = 20

# ── VP entry: volume + price derivative spike ──
VP_VOL_WINDOW_SEC = 10            # 滚动窗口秒数
VP_VOL_BASELINE_SEC = 300         # 基线窗口（5 分钟）
VP_VOL_SPIKE_MULT = 3.0           # 触发倍数：滚动量 > 3× 基线
VP_VOL_MIN_ABSOLUTE = 500         # 噪声地板：滚动量 < 500 股不触发
VP_PRICE_SLOPE_WINDOW_SEC = 10    # 价格斜率窗口
VP_PRICE_SLOPE_THRESHOLD = 0.003  # +0.3%/10s 视为价格瞬时上涨
VP_PRICE_SLOPE_MIN_ABSOLUTE = 0.0005  # 斜率绝对值下限
VP_TRIGGER_TOLERANCE_SEC = 2      # 量价条件 2 秒内同时满足
VP_COOLDOWN_SEC = 60              # 同股退出后 60s 冷却

# ── VP exit ──
VP_EXIT_DROP_PCT = 0.005          # 跌破入场价 0.5% → 立即卖
VP_EXIT_TIME_LIMIT_SEC = 180      # 3 分钟时间到 → 市价卖
VP_SAFETY_STOP_PCT = 0.05         # 服务端 5% 硬止损兜底
VP_SAFETY_STOP_LIMIT_BUFFER = 0.02  # stop-limit 限价 2% buffer

# ── Position management ──
MAX_POSITIONS = 0             # 0=不限
MAX_POSITIONS_PER_DAY = 0     # 0=不限
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

# ── Backtest parameters ──
INITIAL_CAPITAL = 500
BACKTEST_DAYS = 180
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
VERSION = "stonewang_daytrade_vp_1.0"
