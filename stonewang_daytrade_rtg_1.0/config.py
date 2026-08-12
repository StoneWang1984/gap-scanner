"""Config — stonewang_daytrade_rtg_1.0: Red-to-Green Volume Breakout.

Strategy:
  - Pre-market scan for gap-up stocks (gap > 10%), rank by RVOL, select top 5
  - At 09:30 open, monitor 1-min bars for entry signals (NO 09:31 rescan)
  - Entry A (Red-to-Green): bar close > open_price AND bar volume > 2× prior bar volume
  - Entry B (Gap-and-Go): bar 1 bullish + bar 2 breaks bar 1 high (both with volume)
  - Exit: 3% hard stop, 10% target, 10-min time limit, trailing 3% after +5%
  - Position size: $100-150 per stock, max 3 concurrent, max 5 trades/day

Designed to capture the patterns seen on 8/10:
  XHLD +59% HOD, FEAM +22.6%, VIVK +20%, INHD +16.2%, ABCL +14.3%
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

# ── Scanner filters ──────────────────────────────────────────────────
GAP_THRESHOLD = 0.10   # min 10% gap
GAP_MAX = 1.0          # max 100% gap
MIN_VOLUME = 10000     # min pre-market volume
MIN_DOLLAR_VOLUME = 100000
PRICE_MIN = 1.0
PRICE_MAX = 20.0

# Leveraged ETF exclusion
LEVERAGED_ETF_SUFFIXES = ("BULL", "BEAR")
LEVERAGED_ETF_PREFIXES = ()

# ── RTG candidate selection ──────────────────────────────────────────
MAX_CANDIDATES = 8  # Top 8 by RVOL — more trades = more compounding
RVOL_LOOKBACK_DAYS = 20  # 20-day average volume for RVOL calculation
RTG_ONLY = True  # Only trade RTG signals — GapGo has 34% win rate (removed)

# ── RVOL-weighted position sizing ────────────────────────────────────
# (rvol_min, equity_pct) — higher RVOL = bigger conviction = bigger size
# Breakthrough: 80% on best signal (top day traders go 100%)
RVOL_SIZING_TIERS = [
    (10.0, 0.80),   # RVOL > 10× → 80% of equity (full conviction)
    (5.0,  0.50),   # RVOL 5-10× → 50% of equity
    (0.0,  0.30),   # RVOL < 5× → 30% of equity
]

# ── RVOL-adaptive exit tiers ─────────────────────────────────────────
# (rvol_min, stop_pct, target_pct, trail_activate_pct, trail_pct)
# Breakthrough: activate trail at +3%, trail at 2% — captures 17% more per winner
RVOL_EXIT_TIERS = [
    (10.0, 0.07, 0.50, 0.03, 0.02),  # High RVOL: 7% stop, 50% target, trail +3%/2%
    (5.0,  0.05, 0.30, 0.03, 0.02),  # Medium: 5% stop, 30% target, trail +3%/2%
    (0.0,  0.03, 0.15, 0.03, 0.02),  # Low: 3% stop, 15% target, trail +3%/2%
]

# ── Re-entry after profitable exit ───────────────────────────────────
RTG_REENTRY_ALLOWED = True
RTG_REENTRY_MAX = 1             # Max 1 re-entry per stock per day
RTG_REENTRY_SIZE_PCT = 0.50     # Re-entry at 50% of original position size

# ── Entry parameters ─────────────────────────────────────────────────
ENTRY_WINDOW_START = "09:30"  # Start at open (v1.0 was 09:31)
ENTRY_WINDOW_END = "12:00"    # Extended to noon (was 10:30)

# Signal A: Red-to-Green (THE signal — 75% win rate in backtest)
RTG_VOLUME_MULT = 1.5       # Lower threshold catches earlier signals (was 2.0)
RTG_MIN_VOLUME = 30000      # Lower liquidity floor (was 50000)
RTG_MIN_PRICE_GAIN = 0.0    # Min (close - open_price) / open_price for signal bar
RTG_ENTRY_AT_OPEN = True    # Enter at open_price + 0.1% instead of signal bar close (better price)

# Signal B: Gap-and-Go (DISABLED — 34% win rate, too many false breakouts)
GAPGO_MIN_FIRST_BAR_VOL = 99999999   # Effectively disabled
GAPGO_MIN_BREAKOUT_VOL = 99999999    # Effectively disabled

# ── Exit parameters (defaults — overridden by RVOL_EXIT_TIERS) ──────
RTG_STOP_PCT = 0.05           # 5% hard stop loss (default)
RTG_TARGET_PCT = 0.30         # 30% profit target (default)
RTG_TIME_LIMIT_SEC = 1200     # 20-minute time limit
RTG_TRAIL_ACTIVATE_PCT = 0.03 # Activate trailing stop after +3% gain
RTG_TRAIL_PCT = 0.02          # 2% trailing stop

# ── Position sizing ──────────────────────────────────────────────────
INITIAL_CAPITAL = 390.04      # Current account equity
MIN_POSITION_SIZE = 40        # Min $40 per position (fractional shares)
MAX_POSITION_SIZE = 9999      # No hard cap — RVOL tiers control sizing
MAX_POSITIONS = 3             # Max 3 concurrent positions
MAX_DAILY_TRADES = 12         # More trades: 8 candidates + re-entries
MAX_DAILY_LOSS_PCT = 0.05     # 5% daily loss circuit breaker
EQUITY_POSITION_RATIO = 1.0

# ── Market hours ─────────────────────────────────────────────────────
MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"
FORCE_CLOSE_TIME = "15:50"   # EOD force close

# ── Live trading ─────────────────────────────────────────────────────
USE_WEBSOCKET = True
POLL_INTERVAL = 3             # Main loop poll interval (seconds)
DRY_RUN_POLL_INTERVAL = 5

# ── Slippage model ───────────────────────────────────────────────────
SLIPPAGE_ENTRY_PCT = 0.005    # 0.5% entry slippage (market buy)
SLIPPAGE_EXIT_PCT = 0.005     # 0.5% exit slippage (market sell)
SLIPPAGE_FORCE_CLOSE_PCT = 0.01

# ── Order parameters ─────────────────────────────────────────────────
ENTRY_LIMIT_BUFFER = 0.005
STOP_LIMIT_BUFFER = 0.02
FORCE_CLOSE_LIMIT_TIMEOUT = 60

# ── Backtest ─────────────────────────────────────────────────────────
BACKTEST_DAYS = 30

# ── Version ──────────────────────────────────────────────────────────
VERSION = "stonewang_daytrade_rtg_1.0"
VERSION_SHORT = "rtg_1.0"
