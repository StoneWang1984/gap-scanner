"""Dry-run trading day simulator — Stone 0.4.14 with SIP data.
Runs full scan + monitoring loop WITHOUT placing any orders.
Tests: scanning, price tracking, entry/exit signal detection, timing.
Logs to versions/dryrun.log
"""

import time, datetime as dt, re, json, math
from zoneinfo import ZoneInfo
from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import Adjustment, DataFeed

import importlib.util, sys, os

_ver_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_ver_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

_spec = importlib.util.spec_from_file_location("config", os.path.join(_ver_dir, "config_stone_0.4.14.py"))
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)
sys.modules["config"] = config

from scanner import get_tradable_symbols
from strategy import calc_atr, calc_stop_price, calc_price_at_retracement, calc_position_size

tc = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=config.ALPACA_PAPER)
dc = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)

DATA_FEED = DataFeed.SIP if config.DATA_FEED == "sip" else DataFeed.IEX

_LOG_FILE = os.path.join(_ver_dir, "dryrun.log")
_RESULT_FILE = os.path.join(_ver_dir, "dryrun_result.json")

def log(msg):
    now = dt.datetime.now().strftime("%H:%M:%S")
    line = f"[{now}] {msg}"
    print(line, flush=True)
    with open(_LOG_FILE, "a") as f:
        f.write(line + "\n")

_LEV_PATTERN = re.compile(r'(2X|3X|BULL|BEAR)$', re.IGNORECASE)
def is_leveraged_etf(symbol):
    if _LEV_PATTERN.search(symbol): return True
    if len(symbol) > 3 and symbol[-1] in ('U','L'): return True
    if any(symbol.startswith(p) for p in ('TQQQ','SQQQ','UPRO','SPXU','TNA','TZA',
        'MSTU','MSTZ','CONL','NAIL','WEBL','FNGU','FNGD','SOXL','SOXS','TECL','TECS',
        'UDOW','SDOW','UMDD','SMDD','YINN','YANG','CURE','LABD','LABU','DRN','DRV',
        'DGP','DGZ','BOIL','KOLD','NUGT','DUST','JNUG','JDST','GLL','UGL')): return True
    return False


@dataclass
class SimPosition:
    symbol: str
    entry_price: float
    shares: int
    stop_price: float
    target_75: float
    target_1125: float
    target_150: float
    open_price: float
    trade_type: str = "first"
    reached_75: bool = False
    reached_1125: bool = False
    reached_150: bool = False
    remaining_shares: int = 0
    highest: float = 0.0
    prev_high: float = 0.0
    bar_count: int = 0
    time_limit_active: bool = False

    def __post_init__(self):
        self.remaining_shares = self.shares
        self.highest = self.entry_price


class BarAccumulator:
    def __init__(self):
        self._cache = defaultdict(list)

    def add_bar(self, symbol, bar):
        if bar:
            self._cache[symbol].append({
                "ts": str(bar.timestamp),
                "open": float(bar.open), "high": float(bar.high),
                "low": float(bar.low), "close": float(bar.close),
                "volume": int(bar.volume),
            })


def scan_gaps():
    symbols = get_tradable_symbols()
    symbols = [s for s in symbols if not is_leveraged_etf(s)]
    log(f"Scanning {len(symbols)} symbols for gaps...")

    today = dt.date.today()
    yesterday = today - pd.Timedelta(days=5)
    end = pd.Timestamp(today, tz="America/New_York") + pd.Timedelta(days=1)
    batch_size = 500
    results = []

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        request = StockBarsRequest(
            symbol_or_symbols=batch, timeframe=TimeFrame.Day,
            start=yesterday, end=end, adjustment=Adjustment.RAW, feed=DATA_FEED,
        )
        try:
            bars = dc.get_stock_bars(request)
        except Exception as e:
            log(f"API error: {str(e)[:60]}")
            continue
        if bars.df.empty:
            continue
        df = bars.df
        for sym in batch:
            try:
                sym_df = df[df.index.get_level_values("symbol") == sym].sort_index() if isinstance(df.index[0], tuple) else df
                if len(sym_df) < 2:
                    continue
                prev = sym_df.iloc[-2]
                curr = sym_df.iloc[-1]
                prev_close = prev["close"]
                open_price = curr["open"]
                volume = prev["volume"]
                if prev_close <= 0:
                    continue
                gap_pct = (open_price / prev_close) - 1.0
                if gap_pct < config.GAP_THRESHOLD:
                    continue
                if volume < config.MIN_VOLUME:
                    continue
                if not (config.PRICE_MIN <= open_price <= config.PRICE_MAX):
                    continue
                dollar_volume = prev_close * volume
                if dollar_volume < config.MIN_DOLLAR_VOLUME:
                    continue
                results.append({
                    "symbol": sym, "open_price": float(open_price),
                    "prev_close": float(prev_close), "gap_pct": float(gap_pct),
                    "volume": int(volume), "dollar_volume": float(dollar_volume),
                })
            except (KeyError, IndexError):
                continue

    results.sort(key=lambda x: x["gap_pct"], reverse=True)
    return results


def get_atr(symbol):
    today = dt.date.today()
    start = today - pd.Timedelta(days=30)
    request = StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
        start=start, end=today, adjustment=Adjustment.RAW, feed=DATA_FEED,
    )
    try:
        bars = dc.get_stock_bars(request)
        if bars.df.empty:
            return 0.0
        df = bars.df
        if isinstance(df.index[0], tuple):
            df = df.xs(symbol, level="symbol")
        bar_list = [{"high": r["high"], "low": r["low"], "close": r["close"]}
                     for _, r in df.iterrows()]
        return calc_atr(bar_list, period=14)
    except Exception:
        return 0.0


def get_snapshots(symbols):
    request = StockSnapshotRequest(symbol_or_symbols=symbols, feed=DATA_FEED)
    return dc.get_stock_snapshot(request)


def main():
    log("=" * 60)
    log("Stone 0.4.14 DRY-RUN Simulator (no orders)")
    log(f"Data feed: {DATA_FEED.value}")
    log(f"Capital: ${config.INITIAL_CAPITAL}")
    log("=" * 60)

    # ── Scan ──
    t0 = time.time()
    candidates = scan_gaps()
    scan_time = time.time() - t0
    log(f"Scan complete: {len(candidates)} gaps found in {scan_time:.1f}s")

    if not candidates:
        log("No gaps found. Nothing to simulate.")
        return

    deployable = calc_position_size(config.INITIAL_CAPITAL)
    max_stocks = config.MAX_POSITIONS_PER_DAY
    candidates = candidates[:max_stocks]

    log(f"Candidates: {[c['symbol'] for c in candidates]}")
    for c in candidates:
        log(f"  {c['symbol']}: gap +{c['gap_pct']:.1%}, open=${c['open_price']:.4f}")

    # ── Initialize simulation ──
    capital = config.INITIAL_CAPITAL
    positions = []
    entry_checked = set()
    daily_trades = 0
    events = []
    day_highs = {c['symbol']: c['open_price'] for c in candidates}
    accumulator = BarAccumulator()
    poll_count = 0
    daily_loss = 0.0
    daily_stopped = False

    ENTRY_LIMIT_BUFFER = 0.005
    REENTRY_CUTOFF = getattr(config, "REENTRY_CUTOFF_TIME", "12:30")

    # ── Main loop ──
    log("Starting simulation loop (30s polls)...")
    force_close_time = dt.time(15, 50)
    cutoff_time = dt.time(10, 0)
    reentry_cutoff_time = dt.time(int(REENTRY_CUTOFF[:2]), int(REENTRY_CUTOFF[3:]))

    max_polls = 20  # Limit for dry-run (10 minutes)

    while poll_count < max_polls:
        now_est = dt.datetime.now(tz=ZoneInfo("America/New_York"))
        now_time = now_est.time()
        poll_count += 1

        if daily_stopped:
            log("Daily loss circuit breaker triggered. Stopping simulation.")
            break

        if now_time >= force_close_time:
            log("Force close time reached. Ending simulation.")
            break

        # Get snapshots
        all_symbols = list(set(
            [c['symbol'] for c in candidates] +
            [p.symbol for p in positions]
        ))
        if not all_symbols:
            log("No symbols to monitor.")
            break

        try:
            snaps = get_snapshots(all_symbols)
        except Exception as e:
            log(f"Snapshot error: {str(e)[:60]}")
            time.sleep(30)
            continue

        # Accumulate bars + track highs
        for symbol in all_symbols:
            snap = snaps.get(symbol)
            if snap and snap.minute_bar:
                accumulator.add_bar(symbol, snap.minute_bar)
            if snap and snap.daily_bar:
                h = float(snap.daily_bar.high)
                day_highs[symbol] = max(day_highs.get(symbol, 0), h)

        # ── Check entry for candidates ──
        for c in candidates:
            sym = c['symbol']
            if sym in entry_checked:
                continue
            if daily_trades >= config.MAX_DAILY_TRADES:
                continue
            if now_time > cutoff_time:
                entry_checked.add(sym)
                continue

            snap = snaps.get(sym)
            if not snap or not snap.latest_trade:
                continue
            cur_price = float(snap.latest_trade.price)
            open_price = c['open_price']

            # Entry condition: price below open (pullback)
            if cur_price >= open_price:
                continue

            # Calculate entry price
            entry_price = round(open_price * (1 + ENTRY_LIMIT_BUFFER), 2)

            # Check if current price is at or below entry price
            if cur_price > entry_price:
                continue

            # Calculate position
            deployable = calc_position_size(capital)
            shares = int(deployable / entry_price)
            if shares <= 0:
                entry_checked.add(sym)
                continue

            # ATR stop
            atr = get_atr(sym)
            stop_price = calc_stop_price(entry_price, atr, config.STOP_LOSS_ATR_MULT,
                                          config.STOP_LOSS_PCT_FALLBACK,
                                          getattr(config, 'STOP_LOSS_MAX_PCT', 1.0))

            # Targets
            target_75 = calc_price_at_retracement(entry_price, open_price, config.PROFIT_RETRACEMENT_75)
            target_1125 = calc_price_at_retracement(entry_price, open_price, config.PROFIT_RETRACEMENT_1125)
            target_150 = calc_price_at_retracement(entry_price, open_price, config.PROFIT_RETRACEMENT_150)

            pos = SimPosition(
                symbol=sym, entry_price=entry_price, shares=shares,
                stop_price=stop_price, target_75=target_75,
                target_1125=target_1125, target_150=target_150,
                open_price=open_price, prev_high=open_price,
            )
            positions.append(pos)
            entry_checked.add(sym)
            daily_trades += 1
            log(f"[DRY BUY] {sym} {shares}sh @ ${entry_price:.4f} stop=${stop_price:.4f}")
            log(f"  targets: 75%=${target_75:.4f} 112.5%=${target_1125:.4f} 150%=${target_150:.4f}")
            events.append({"ts": now_est.strftime("%H:%M:%S"), "type": "buy", "symbol": sym,
                          "price": entry_price, "shares": shares})

        # ── Check exits for positions ──
        for pos in positions[:]:
            if pos.remaining_shares <= 0:
                continue

            snap = snaps.get(pos.symbol)
            if not snap or not snap.latest_trade:
                continue

            cur_price = float(snap.latest_trade.price)
            if cur_price > pos.highest:
                pos.highest = cur_price

            # Stop loss
            if cur_price <= pos.stop_price:
                pnl = (cur_price - pos.entry_price) * pos.shares
                daily_loss += min(0, pnl)
                log(f"[DRY STOP] {pos.symbol} @ ${pos.stop_price:.4f} (cur=${cur_price:.4f}) P&L=${pnl:.2f}")
                pos.remaining_shares = 0
                events.append({"ts": now_est.strftime("%H:%M:%S"), "type": "stop",
                              "symbol": pos.symbol, "price": pos.stop_price, "pnl": pnl})
                continue

            # Time limit
            pos.bar_count += 1
            time_limit = getattr(config, "FIRST_TRADE_TIME_LIMIT_BARS", 0)
            if time_limit > 0 and not pos.reached_75 and pos.bar_count >= time_limit:
                pos.time_limit_active = True
            if pos.time_limit_active and cur_price >= pos.entry_price:
                pnl = (cur_price - pos.entry_price) * pos.remaining_shares
                log(f"[DRY TIME_LIMIT] {pos.symbol} @ ${cur_price:.4f} P&L=${pnl:.2f}")
                pos.remaining_shares = 0
                events.append({"ts": now_est.strftime("%H:%M:%S"), "type": "time_limit",
                              "symbol": pos.symbol, "price": cur_price, "pnl": pnl})
                continue

            # Profit targets
            if not pos.reached_150 and pos.highest >= pos.target_150:
                log(f"[DRY TARGET_150] {pos.symbol} highest=${pos.highest:.4f}")
                pos.reached_150 = pos.reached_1125 = pos.reached_75 = True
            elif not pos.reached_1125 and pos.highest >= pos.target_1125:
                log(f"[DRY TARGET_112.5%] {pos.symbol} highest=${pos.highest:.4f}")
                pos.reached_1125 = pos.reached_75 = True
            elif not pos.reached_75 and pos.highest >= pos.target_75:
                log(f"[DRY TARGET_75%] {pos.symbol} highest=${pos.highest:.4f}")
                pos.reached_75 = True

            # Trailing stop checks
            if pos.reached_75 and not pos.reached_1125:
                trail = pos.highest * (1 - config.TRAILING_STOP_PCT_75)
                if cur_price <= trail:
                    pnl = (trail - pos.entry_price) * pos.remaining_shares
                    log(f"[DRY TRAIL_75] {pos.symbol} @ ${trail:.4f} P&L=${pnl:.2f}")
                    pos.remaining_shares = 0
                    events.append({"ts": now_est.strftime("%H:%M:%S"), "type": "trail_75",
                                  "symbol": pos.symbol, "price": trail, "pnl": pnl})

            elif pos.reached_1125 and not pos.reached_150:
                trail = pos.highest * (1 - config.TRAILING_STOP_PCT_1125)
                if cur_price <= trail:
                    pnl = (trail - pos.entry_price) * pos.remaining_shares
                    log(f"[DRY TRAIL_112] {pos.symbol} @ ${trail:.4f} P&L=${pnl:.2f}")
                    pos.remaining_shares = 0
                    events.append({"ts": now_est.strftime("%H:%M:%S"), "type": "trail_112",
                                  "symbol": pos.symbol, "price": trail, "pnl": pnl})

            elif pos.reached_150:
                trail = pos.highest * (1 - config.TRAILING_STOP_PCT_150)
                if cur_price <= trail:
                    pnl = (trail - pos.entry_price) * pos.remaining_shares
                    log(f"[DRY TRAIL_150] {pos.symbol} @ ${trail:.4f} P&L=${pnl:.2f}")
                    pos.remaining_shares = 0
                    events.append({"ts": now_est.strftime("%H:%M:%S"), "type": "trail_150",
                                  "symbol": pos.symbol, "price": trail, "pnl": pnl})

        # Daily loss check
        if daily_loss < 0 and abs(daily_loss) / capital > config.MAX_DAILY_LOSS_PCT:
            daily_stopped = True
            log(f"[DRY CIRCUIT BREAKER] daily loss ${daily_loss:.2f} > {config.MAX_DAILY_LOSS_PCT:.0%} of ${capital}")

        # Status
        open_pos = [p for p in positions if p.remaining_shares > 0]
        log(f"Poll {poll_count}: {len(open_pos)} open positions, {daily_trades} trades, "
            f"scan={scan_time:.0f}s, equity=${capital:.2f}")

        time.sleep(30)

    # ── Summary ──
    log("")
    log("=" * 60)
    log("DRY-RUN Summary")
    log(f"Scan time: {scan_time:.1f}s | Data feed: {DATA_FEED.value}")
    log(f"Candidates: {[c['symbol'] for c in candidates]}")
    log(f"Total events: {len(events)}")
    for e in events:
        log(f"  {e['ts']} {e['type']:10s} {e['symbol']} ${e.get('price',0):.4f}")

    result = {
        "date": str(dt.date.today()),
        "scan_time_s": round(scan_time, 1),
        "data_feed": DATA_FEED.value,
        "candidates": candidates,
        "events": events,
        "total_polls": poll_count,
    }
    with open(_RESULT_FILE, "w") as f:
        json.dump(result, f, indent=2, default=str)
    log(f"Results saved to {_RESULT_FILE}")


if __name__ == "__main__":
    main()
