"""stonewang_daytrade_rtg_1.0 — Red-to-Green Volume Breakout Live Trading.

Key design vs v1.0:
  - Pre-market scan at 09:20, NO 09:31 rescan — start monitoring at 09:30
  - Top 5 candidates by RVOL (not 20)
  - Entry: RTG (close > open + vol > 2x prior) or GapGo (2-bar breakout)
  - Exit: 5% stop, 20% target, 15-min time limit, 5% trailing after +8%
  - Market orders only (no OCO/bracket)
  - Position: $100-150 per stock, max 3 concurrent
"""

import re
import json
import time
import datetime as dt
from zoneinfo import ZoneInfo
from dataclasses import dataclass
import threading
from collections import deque
from uuid import uuid4

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import Adjustment, DataFeed

import importlib.util, sys, os

_ver_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_ver_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

_spec = importlib.util.spec_from_file_location("config", os.path.join(_ver_dir, "config.py"))
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)
sys.modules["config"] = config

from scanner import get_tradable_symbols, scan_gaps_batch, get_data_client

_EST = ZoneInfo("America/New_York")
_LEV_PATTERN = re.compile(r"(2X|3X|BULL|BEAR)$", re.IGNORECASE)
_LEV_PREFIXES = (
    "TQQQ", "SQQQ", "FNGU", "FNGD", "SOXL", "SOXS", "TECL", "TECS",
    "UDOW", "SDOW", "SPXU", "UPRO", "TNA", "TZA", "NUGT", "DUST",
    "JNUG", "JDST", "BOIL", "KOLD", "DRN", "DRV", "LABU", "LABD",
    "CURE", "YINN", "YANG", "UMDD", "SMDD", "CONL", "NAIL", "WEBL",
    "MSTU", "MSTZ", "UGL", "GLL", "DGP", "DGZ", "AXTU", "RDWU",
)

_ALPACA_PAPER = getattr(config, "ALPACA_PAPER", False)
trading_client = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=_ALPACA_PAPER)
data_client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)

_log_file = None
_state_file = os.path.join(_parent_dir, "live_state.json")


def is_leveraged_etf(symbol):
    if _LEV_PATTERN.search(symbol):
        return True
    if symbol.endswith(("BULL", "BEAR")):
        return True
    return any(symbol.startswith(p) for p in _LEV_PREFIXES)


def get_rvol_sizing(rvol, equity):
    tiers = getattr(config, "RVOL_SIZING_TIERS", [(10.0, 0.50), (5.0, 0.30), (0.0, 0.15)])
    for rvol_min, pct in tiers:
        if rvol >= rvol_min:
            return round(equity * pct, 2)
    return round(equity * 0.15, 2)


def get_rvol_exit_params(rvol):
    tiers = getattr(config, "RVOL_EXIT_TIERS", [
        (10.0, 0.07, 0.30, 0.05, 0.03),
        (5.0,  0.05, 0.20, 0.05, 0.03),
        (0.0,  0.03, 0.10, 0.04, 0.02),
    ])
    for rvol_min, stop, target, trail_act, trail in tiers:
        if rvol >= rvol_min:
            return stop, target, trail_act, trail
    return 0.05, 0.20, 0.05, 0.03


def log(msg):
    ts = dt.datetime.now(_EST).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    if _log_file:
        _log_file.write(line + "\n")
        _log_file.flush()


@dataclass
class Position:
    symbol: str
    shares: int
    entry_price: float
    entry_ts: float = 0.0
    open_price: float = 0.0
    gap_pct: float = 0.0
    signal_type: str = ""
    highest: float = 0.0
    trail_active: bool = False
    rvol: float = 0.0
    stop_pct: float = 0.0
    target_pct: float = 0.0
    trail_activate_pct: float = 0.0
    trail_pct: float = 0.0


class BarAccumulator:
    def __init__(self):
        self._bars = {}
        self._current = {}
        self._lock = threading.Lock()

    def add_bar(self, symbol, bar_dict):
        with self._lock:
            if symbol not in self._bars:
                self._bars[symbol] = deque(maxlen=500)
            self._bars[symbol].append(dict(bar_dict))

    def add_trade(self, symbol, price, size, ts):
        with self._lock:
            if symbol not in self._bars:
                self._bars[symbol] = deque(maxlen=500)
            bar_ts = ts.replace(second=0, microsecond=0)
            if symbol not in self._current:
                self._current[symbol] = {
                    "open": price, "high": price, "low": price,
                    "close": price, "volume": size, "timestamp": bar_ts,
                }
            else:
                cur = self._current[symbol]
                if bar_ts != cur["timestamp"]:
                    self._bars[symbol].append(dict(cur))
                    cur.update(open=price, high=price, low=price, close=price, volume=size, timestamp=bar_ts)
                else:
                    cur["high"] = max(cur["high"], price)
                    cur["low"] = min(cur["low"], price)
                    cur["close"] = price
                    cur["volume"] += size

    def get_1min_bars(self, symbol):
        with self._lock:
            bars = list(self._bars.get(symbol, []))
            if symbol in self._current:
                bars.append(dict(self._current[symbol]))
            return bars


_stream_state = {"running": False, "last_bar_ts": time.time(), "symbols": set()}
_accumulator = BarAccumulator()
_ws_stream = None


async def _on_bar(bar):
    _stream_state["last_bar_ts"] = time.time()
    sym = bar.symbol
    ts = bar.timestamp
    if hasattr(ts, "timestamp"):
        ts = dt.datetime.fromtimestamp(ts.timestamp(), tz=_EST)
    _accumulator.add_bar(sym, {
        "open": float(bar.open), "high": float(bar.high),
        "low": float(bar.low), "close": float(bar.close),
        "volume": int(bar.volume), "timestamp": ts,
    })


async def _on_trade(trade):
    _stream_state["last_bar_ts"] = time.time()
    sym = trade.symbol
    ts = trade.timestamp
    if hasattr(ts, "timestamp"):
        ts = dt.datetime.fromtimestamp(ts.timestamp(), tz=_EST)
    _accumulator.add_trade(sym, float(trade.price), int(trade.size), ts)


def start_ws_stream(symbols):
    global _ws_stream
    if _stream_state["running"]:
        return
    try:
        from alpaca.data.live.stock import StockDataStream
        _ws_stream = StockDataStream(
            config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY,
            feed=getattr(config, "DATA_FEED", "sip"),
        )
        for sym in symbols:
            _ws_stream.subscribe_bars(_on_bar, sym)
            _ws_stream.subscribe_trades(_on_trade, sym)
        _stream_state["symbols"] = set(symbols)
        _stream_state["running"] = True
        _stream_state["last_bar_ts"] = time.time()

        def _run():
            try:
                _ws_stream.run()
            except Exception as e:
                log(f"WebSocket error: {e}")
            _stream_state["running"] = False

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        log(f"WebSocket stream started for {len(symbols)} symbols")
    except Exception as e:
        log(f"WebSocket start failed: {e}")


def restart_ws_stream(symbols):
    _stream_state["running"] = False
    if _ws_stream:
        try:
            _ws_stream.stop()
        except Exception:
            pass
    time.sleep(2)
    start_ws_stream(symbols)


def place_buy_market(symbol, shares):
    """Submit market buy order. Returns (order, None, None) on success or (None, None, reason) on rejection."""
    if config.DRY_RUN:
        oid = f"DRY-{uuid4().hex[:8]}"
        log(f"[DRY] BUY {symbol} {shares}sh")
        return type("Order", (), {"id": oid})(), None, None
    try:
        req = MarketOrderRequest(symbol=symbol, qty=shares, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        order = trading_client.submit_order(req)
        log(f"BUY order submitted: {symbol} {shares}sh, id={order.id}")
        return order, None, None
    except Exception as e:
        log(f"BUY rejected: {symbol} {shares}sh - {e}")
        return None, None, str(e)


def wait_order_filled(order_id, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            order = trading_client.get_order_by_id(str(order_id))
            if order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                return int(float(order.filled_qty)), float(order.filled_avg_price or 0)
            if order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
                return 0, 0.0
        except Exception:
            pass
        time.sleep(1)
    return 0, 0.0


def place_sell_market(symbol, shares):
    if config.DRY_RUN:
        log(f"[DRY] SELL {symbol} {shares}sh")
        return shares, 0.0
    try:
        req = MarketOrderRequest(symbol=symbol, qty=shares, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
        order = trading_client.submit_order(req)
        filled, price = wait_order_filled(str(order.id), timeout=30)
        return filled, price
    except Exception as e:
        log(f"SELL failed: {symbol} {shares}sh. - {e}")
        return 0, 0.0


def force_sell_position(symbol, shares):
    try:
        trading_client.close_position(symbol_or_symbol=symbol, qty=str(shares))
        # Wait for fill confirmation (up to 30s)
        time.sleep(1)
        for _ in range(30):
            try:
                pos = trading_client.get_open_position(symbol)
                remaining = int(float(pos.qty))
                if remaining <= 0:
                    break
            except Exception:
                break  # Position not found = fully closed
            time.sleep(1)
        # Get fill price from recent closed order
        fill_price = 0.0
        try:
            orders = trading_client.get_orders_for_symbol(symbol)
            for o in orders:
                if o.side == OrderSide.SELL and o.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                    filled_qty = int(float(o.filled_qty))
                    if filled_qty >= shares:
                        fill_price = float(o.filled_avg_price or 0)
                        break
        except Exception:
            pass
        return shares, fill_price
    except Exception as e:
        log(f"Force close failed: {symbol} - {e}")
        return 0, 0.0


def save_state(state):
    with open(_state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)


def scan_gaps(target_date):
    log(f"Scanning for gap stocks on {target_date.date()}...")
    symbols = get_tradable_symbols()
    symbols = [s for s in symbols if not is_leveraged_etf(s)]
    log(f"After leveraged ETF filter: {len(symbols)} symbols")
    results = scan_gaps_batch(data_client, target_date, symbols)
    if results.empty:
        log("No gap stocks found")
        return []
    results = results.sort_values("prev_volume", ascending=False)
    candidates = []
    for _, row in results.head(config.MAX_CANDIDATES * 2).iterrows():
        rvol = row.get("rvol", 0)
        if rvol <= 0 and "prev_volume" in row:
            avg_vol = row.get("avg_volume_20d", 0)
            rvol = row["prev_volume"] / avg_vol if avg_vol > 0 else 0
        candidates.append({
            "symbol": row["symbol"], "open_price": float(row["open_price"]),
            "prev_close": float(row["prev_close"]), "gap_pct": float(row["gap_pct"]),
            "rvol": float(rvol),
        })
    candidates.sort(key=lambda c: c["rvol"], reverse=True)
    candidates = candidates[:config.MAX_CANDIDATES]
    log(f"Top {len(candidates)} candidates by RVOL: {[c['symbol'] for c in candidates]}")
    for c in candidates:
        log(f"  {c['symbol']}: gap +{c['gap_pct']:.1%}, RVOL={c['rvol']:.1f}×, open=${c['open_price']:.4f}")
    return candidates


def backfill_1min_bars(symbols, target_date):
    mkt_open = pd.Timestamp(f"{target_date.date()} {config.MARKET_OPEN}", tz="America/New_York")
    now = pd.Timestamp.now(tz="America/New_York")
    end = min(now, pd.Timestamp(f"{target_date.date()} {config.MARKET_CLOSE}", tz="America/New_York"))
    if end <= mkt_open:
        return
    for sym in symbols:
        try:
            req = StockBarsRequest(
                symbol_or_symbols=sym, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                start=mkt_open, end=end, adjustment=Adjustment.RAW,
                feed=getattr(config, "DATA_FEED_OBJ", DataFeed.SIP),
            )
            bars = data_client.get_stock_bars(req)
            if bars.df.empty:
                continue
            df = bars.df
            if "symbol" in df.columns:
                df = df[df["symbol"] == sym]
            for i in range(len(df)):
                bar = df.iloc[i]
                idx = df.index[i]
                ts = idx[1] if isinstance(idx, tuple) else idx
                ts = pd.Timestamp(ts)
                if ts.tzinfo is None:
                    ts = ts.tz_localize("UTC")
                ts = ts.tz_convert("America/New_York")
                _accumulator.add_bar(sym, {
                    "open": float(bar["open"]), "high": float(bar["high"]),
                    "low": float(bar["low"]), "close": float(bar["close"]),
                    "volume": int(bar["volume"]), "timestamp": ts,
                })
        except Exception:
            pass


def check_rtg_entry(symbol, open_price, bars):
    if len(bars) < 2:
        return 0.0, False, ""
    ew_start_h, ew_start_m = (int(x) for x in config.ENTRY_WINDOW_START.split(":"))
    ew_end_h, ew_end_m = (int(x) for x in config.ENTRY_WINDOW_END.split(":"))
    entry_start = dt.time(ew_start_h, ew_start_m)
    entry_end = dt.time(ew_end_h, ew_end_m)
    for i in range(1, len(bars)):
        bar = bars[i]
        prev = bars[i - 1]
        ts = bar.get("timestamp")
        if ts is None:
            continue
        bar_time = ts.time() if isinstance(ts, dt.datetime) else (ts.time() if hasattr(ts, "time") else None)
        if bar_time is None or not (entry_start <= bar_time <= entry_end):
            continue
        bc = bar["close"]
        bh = bar["high"]
        bv = bar["volume"]
        pv = prev["volume"]
        ph = prev["high"]
        po = prev["open"]
        pc = prev["close"]
        if bc > open_price and pv > 0 and bv >= config.RTG_VOLUME_MULT * pv and bv >= config.RTG_MIN_VOLUME:
            entry = round(open_price * 1.001, 4) if getattr(config, "RTG_ENTRY_AT_OPEN", True) else round(bc, 4)
            return entry, True, "rtg"
        if pc > po and pv >= config.GAPGO_MIN_FIRST_BAR_VOL and bh > ph and bv >= config.GAPGO_MIN_BREAKOUT_VOL:
            return round(ph, 4), True, "gapgo"
    return 0.0, False, ""


def _parse_time(t_str):
    h, m = (int(x) for x in t_str.split(":"))
    return dt.time(h, m)


def run_trading_day(target_date):
    log(f"Starting trading day: {target_date.date()} (close {config.MARKET_CLOSE}, force_close {config.FORCE_CLOSE_TIME})")
    try:
        acct = trading_client.get_account()
        equity = float(acct.equity)
    except Exception:
        equity = config.INITIAL_CAPITAL
    log(f"Account equity: ${equity:,.2f}")

    candidates = scan_gaps(target_date)
    if not candidates:
        log("No candidates, waiting for force close")
        _wait_until(target_date, _parse_time(config.FORCE_CLOSE_TIME))
        return

    syms = [c["symbol"] for c in candidates]
    backfill_1min_bars(syms, target_date)
    restart_ws_stream(syms)

    positions = []
    entry_checked = set()
    daily_trades = 0
    trades_detail = []
    daily_loss = 0.0
    max_daily_loss = equity * config.MAX_DAILY_LOSS_PCT
    entry_count = {}  # symbol -> count of entries (for re-entry)
    _last_exit_ts = {}  # symbol -> timestamp of last exit

    force_close_dt = dt.datetime.combine(target_date.date(), _parse_time(config.FORCE_CLOSE_TIME), tzinfo=_EST)
    entry_end_dt = dt.datetime.combine(target_date.date(), _parse_time(config.ENTRY_WINDOW_END), tzinfo=_EST)
    market_close_dt = dt.datetime.combine(target_date.date(), _parse_time(config.MARKET_CLOSE), tzinfo=_EST)

    state = {"version": config.VERSION_SHORT, "data_feed": config.DATA_FEED,
             "ws_connected": True, "daily_trades": 0, "candidates": candidates,
             "positions": [], "trades_detail": []}

    while True:
        now = dt.datetime.now(_EST)
        if now >= market_close_dt:
            break

        if now >= force_close_dt:
            log("Force close time reached!")
            for pos in positions[:]:
                sold, fill = force_sell_position(pos.symbol, pos.shares)
                if sold > 0:
                    pnl = (fill - pos.entry_price) * sold if fill > 0 else 0
                    trades_detail.append({"symbol": pos.symbol, "entry": pos.entry_price, "exit": fill,
                                          "shares": sold, "pnl": round(pnl, 2), "reason": "force_close",
                                          "trade_type": pos.signal_type})
                    daily_trades += 1
                    positions.remove(pos)
                    log(f";Force closed {pos.symbol}, P&L=${pnl:+,.2f}")
            break

        if max_daily_loss > 0 and daily_loss <= -max_daily_loss:
            log(f"Daily loss ${daily_loss:,.2f} exceeded limit")
            state["daily_stopped"] = True
            break

        # Exit monitoring
        for pos in positions[:]:
            bars = _accumulator.get_1min_bars(pos.symbol)
            if not bars:
                continue
            latest = bars[-1]
            bar_low = latest["low"]
            bar_high = latest["high"]
            cur_price = latest["close"]
            if bar_high > pos.highest:
                pos.highest = bar_high

            stop_price = round(pos.entry_price * (1 - pos.stop_pct), 4)
            reason = None
            if bar_low <= stop_price:
                reason = "stop_loss"
            else:
                if not pos.trail_active:
                    if pos.highest >= pos.entry_price * (1 + pos.trail_activate_pct):
                        pos.trail_active = True
                if pos.trail_active:
                    trail_stop = round(pos.highest * (1 - pos.trail_pct), 4)
                    if bar_low <= trail_stop:
                        reason = "trail_stop"
                if reason is None:
                    target_price = round(pos.entry_price * (1 + pos.target_pct), 4)
                    if bar_high >= target_price:
                        reason = "target"
                    elif time.time() - pos.entry_ts >= config.RTG_TIME_LIMIT_SEC:
                        reason = "time_limit"

            if reason is None:
                continue

            sold, fill = place_sell_market(pos.symbol, pos.shares)
            # Retry once if sell failed
            if sold <= 0:
                log(f"SELL failed for {pos.symbol}, retrying in 2s...")
                time.sleep(2)
                sold, fill = place_sell_market(pos.symbol, pos.shares)
            if sold > 0:
                if fill <= 0:
                    fill = cur_price
                pnl = round((fill - pos.entry_price) * sold, 2)
                trades_detail.append({"symbol": pos.symbol, "entry": pos.entry_price,
                                      "exit": round(fill, 4), "shares": sold, "pnl": pnl,
                                      "reason": reason, "trade_type": pos.signal_type})
                daily_loss += pnl
                daily_trades += 1
                positions.remove(pos)
                _last_exit_ts[pos.symbol] = time.time()
                log(f"EXIT {pos.symbol} {reason} ${fill:.4f}, P&L=${pnl:+,.2f}")

        # Entry monitoring
        if now < entry_end_dt and len(positions) < config.MAX_POSITIONS:
            for c in candidates:
                sym = c["symbol"]
                rvol = c.get("rvol", 0)
                if sym in entry_checked or any(p.symbol == sym for p in positions):
                    continue
                # Re-entry check
                is_reentry = sym in _last_exit_ts
                if is_reentry:
                    max_entries = 1 + getattr(config, "RTG_REENTRY_MAX", 1)
                    if entry_count.get(sym, 0) >= max_entries:
                        continue
                    if not getattr(config, "RTG_REENTRY_ALLOWED", False):
                        continue
                if config.MAX_DAILY_TRADES > 0 and daily_trades >= config.MAX_DAILY_TRADES:
                    break
                if max_daily_loss > 0 and daily_loss <= -max_daily_loss:
                    break
                open_price = c["open_price"]
                bars = _accumulator.get_1min_bars(sym)
                entry_price, confirmed, signal_type = check_rtg_entry(sym, open_price, bars)
                if not confirmed or entry_price <= 0:
                    continue
                # RVOL-weighted sizing
                if is_reentry:
                    reentry_pct = getattr(config, "RTG_REENTRY_SIZE_PCT", 0.50)
                    slot = max(config.MIN_POSITION_SIZE, get_rvol_sizing(rvol, equity) * reentry_pct)
                else:
                    slot = max(config.MIN_POSITION_SIZE, get_rvol_sizing(rvol, equity))
                shares = int(slot / entry_price)
                if shares <= 0:
                    entry_checked.add(sym)
                    continue
                order, _, reject = place_buy_market(sym, shares)
                if order is None:
                    log(f"Entry rejected: {sym} - {reject}")
                    entry_checked.add(sym)
                    continue
                filled, fill_price = wait_order_filled(str(order.id), timeout=15)
                if filled <= 0:
                    entry_checked.add(sym)
                    continue
                if fill_price <= 0:
                    fill_price = entry_price
                # Get adaptive exit params
                stop_p, target_p, trail_act_p, trail_p = get_rvol_exit_params(rvol)
                sig_label = signal_type + ("_re" if is_reentry else "")
                pos = Position(symbol=sym, shares=filled, entry_price=fill_price,
                               entry_ts=time.time(), open_price=open_price,
                               gap_pct=c["gap_pct"], signal_type=sig_label, highest=fill_price,
                               rvol=rvol, stop_pct=stop_p, target_pct=target_p,
                               trail_activate_pct=trail_act_p, trail_pct=trail_p)
                positions.append(pos)
                entry_checked.add(sym)
                entry_count[sym] = entry_count.get(sym, 0) + 1
                daily_trades += 1
                log(f"ENTRY {sym} [{sig_label}] {filled}sh @ ${fill_price:.4f} "
                    f"[RVOL={rvol:.1f}× stop={stop_p:.0%} tgt={target_p:.0%}]")

        # WS health
        if _stream_state["running"] and time.time() - _stream_state["last_bar_ts"] > 60:
            log("WebSocket: no bars for 60s, restarting...")
            restart_ws_stream(syms)

        # Save state
        state.update({
            "updated": dt.datetime.now().isoformat(), "ws_connected": _stream_state["running"],
            "daily_trades": daily_trades,
            "positions": [{"symbol": p.symbol, "shares": p.shares, "entry_price": p.entry_price,
                           "signal_type": p.signal_type} for p in positions],
            "trades_detail": trades_detail,
        })
        save_state(state)
        time.sleep(config.POLL_INTERVAL)

    # End of day
    log("=" * 60)
    log("Trading day complete!")
    final_equity = equity + daily_loss
    log(f"Equity: ${final_equity:,.2f} | Daily P&L: ${daily_loss:+,.2f} | Trades: {daily_trades}")
    log("=" * 60)

    report_dir = os.path.join(_ver_dir, "daily_reports")
    os.makedirs(report_dir, exist_ok=True)
    report = {
        "date": str(target_date.date()), "version": config.VERSION_SHORT,
        "account_equity_start": equity, "account_equity_end": final_equity,
        "daily_pnl": round(daily_loss, 2), "daily_trades": daily_trades,
        "candidates": candidates, "trades": trades_detail,
    }
    with open(os.path.join(report_dir, f"{target_date.date()}.json"), "w") as f:
        json.dump(report, f, indent=2, default=str)
    log(f"Daily report saved")

    state.update({"updated": dt.datetime.now().isoformat(), "daily_trades": daily_trades,
                  "positions": [], "trades_detail": trades_detail})
    save_state(state)


def _wait_until(target_date, target_time):
    target_dt = dt.datetime.combine(target_date.date(), target_time, tzinfo=_EST)
    while dt.datetime.now(_EST) < target_dt:
        time.sleep(60)


def get_next_trading_day():
    now = dt.datetime.now(_EST)
    for delta in range(1, 7):
        candidate = now + dt.timedelta(days=delta)
        if candidate.weekday() < 5:
            return pd.Timestamp(candidate.date(), tz="America/New_York")
    return None


def test_connectivity():
    log("Testing data connectivity...")
    try:
        req = StockBarsRequest(
            symbol_or_symbols="SPY", timeframe=TimeFrame.Day,
            start=pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(days=5),
            end=pd.Timestamp.now(tz="America/New_York"),
            feed=getattr(config, "DATA_FEED_OBJ", DataFeed.SIP),
        )
        bars = data_client.get_stock_bars(req)
        if not bars.df.empty:
            log(f"  SPY bar received, OK!")
        log("Connectivity OK!")
        return True
    except Exception as e:
        log(f"Connectivity failed: {e}")
        return False


def main():
    global _log_file
    _log_file = open(os.path.join(_ver_dir, "live_rtg.log"), "a")

    log(f"Using {config.DATA_FEED.upper()} data feed")
    log("=" * 60)
    log(f"stonewang RTG 1.0 Live Trading -- Concentrated RVOL Sizing")
    log(f"Entry: RTG (vol >= {config.RTG_VOLUME_MULT}x prior) / GapGo DISABLED")
    log(f"Exit: adaptive by RVOL tier | time {config.RTG_TIME_LIMIT_SEC}s")
    log(f"Window: {config.ENTRY_WINDOW_START}-{config.ENTRY_WINDOW_END} EST")
    log(f"Sizing: RVOL-weighted (80/50/30) | max {config.MAX_POSITIONS} concurrent | re-entry {'ON' if getattr(config, 'RTG_REENTRY_ALLOWED', False) else 'OFF'}")
    log("=" * 60)

    if not test_connectivity():
        log("Connectivity failed, exiting")
        return

    while True:
        now = dt.datetime.now(_EST)
        if now.weekday() >= 5:
            next_day = get_next_trading_day()
            log(f"Weekend. Next: {next_day.date()}")
            _smart_sleep_until(dt.datetime.combine(next_day.date(), dt.time(9, 20), tzinfo=_EST))
            continue

        market_open = dt.datetime.combine(now.date(), dt.time(9, 30), tzinfo=_EST)
        market_close = dt.datetime.combine(now.date(), dt.time(16, 0), tzinfo=_EST)
        if now >= market_close:
            next_day = get_next_trading_day()
            log(f"Market closed. Next: {next_day.date()}")
            _smart_sleep_until(dt.datetime.combine(next_day.date(), dt.time(9, 20), tzinfo=_EST))
            continue

        pre_open = dt.datetime.combine(now.date(), dt.time(9, 20), tzinfo=_EST)
        if now < pre_open:
            _smart_sleep_until(pre_open)

        target = pd.Timestamp(now.date(), tz="America/New_York")
        run_trading_day(target)

        next_day = get_next_trading_day()
        log(f"Next trading day: {next_day.date()}. Sleeping until 9:20...")
        _smart_sleep_until(dt.datetime.combine(next_day.date(), dt.time(9, 20), tzinfo=_EST))


def _smart_sleep_until(target_time):
    while True:
        remaining = (target_time - dt.datetime.now(_EST)).total_seconds()
        if remaining <= 0:
            break
        if remaining < 120:
            log(f"Starting in {remaining / 60:.1f} min...")
            time.sleep(max(1, remaining - 1))
        else:
            log(f"Next event in {int(remaining / 60)} min, sleeping...")
            time.sleep(600)


if __name__ == "__main__":
    main()
