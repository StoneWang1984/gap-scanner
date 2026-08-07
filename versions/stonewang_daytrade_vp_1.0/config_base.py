"""Config base — stonewang_daytrade_vp_1.0 (backtest-compatible).

Mirrors config.py but used by backtest.py / strategy.py. Kept as a separate
file so live trading (config.py) and backtest (config_base.py) can diverge
if needed (e.g. paper keys, different slippage assumptions).
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
GAP_THRESHOLD = 0.10
GAP_MAX = 1.0
MIN_VOLUME = 10000
MIN_DOLLAR_VOLUME = 100000
PRICE_MIN = 1.0
PRICE_MAX = 20.0
LEVERAGED_ETF_SUFFIXES = ("BULL", "BEAR")
LEVERAGED_ETF_PREFIXES = ()

# ── Entry window (extended for all-day VP monitoring) ──
ENTRY_WINDOW_START = "09:31"
ENTRY_WINDOW_END = "15:30"
MAX_CANDIDATES = 20

# ── VP entry: volume + price derivative spike ──
VP_VOL_WINDOW_SEC = 10
VP_VOL_BASELINE_SEC = 300
VP_VOL_SPIKE_MULT = 3.0
VP_VOL_MIN_ABSOLUTE = 500
VP_PRICE_SLOPE_WINDOW_SEC = 10
VP_PRICE_SLOPE_THRESHOLD = 0.003
VP_PRICE_SLOPE_MIN_ABSOLUTE = 0.0005
VP_TRIGGER_TOLERANCE_SEC = 2
VP_COOLDOWN_SEC = 60

# ── VP exit ──
VP_EXIT_DROP_PCT = 0.005
VP_EXIT_TIME_LIMIT_SEC = 180
VP_SAFETY_STOP_PCT = 0.05
VP_SAFETY_STOP_LIMIT_BUFFER = 0.02

# ── Position management ──
MAX_POSITIONS = 0
MAX_POSITIONS_PER_DAY = 0
MIN_POSITION_SIZE = 40
MAX_POSITION_SIZE = 100
EQUITY_POSITION_RATIO = 1.0
MAX_DAILY_TRADES = 0

# ── EOD / Circuit breaker ──
FORCE_CLOSE_TIME = "15:50"
MAX_DAILY_LOSS_PCT = 0.05
MAX_DAILY_PROFIT_PCT = 0

# ── Trading ──
DRY_RUN = False
POLL_INTERVAL = 3
USE_WEBSOCKET = True
MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"

# ── Backtest parameters ──
INITIAL_CAPITAL = 500
BACKTEST_DAYS = 180
FORCE_QTY = 0

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
