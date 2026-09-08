"""Optimize trailing stop params for yesterday's trades using intraday bars.
Extended grid: wide 3-10, tight 1-5, after 1-5, time_limit 0/8/12/16.
"""

import os, sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed
from datetime import datetime
import pytz

API_KEY = os.getenv("ALPACA_API_KEY", "")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# Yesterday's trades: (symbol, entry_price, shares)
TRADES = [
    ("OESX",   15.8000, 2),
    ("DAMD",    1.5598, 1),
    ("TBI",     9.9661, 4),
    ("AMPU",    3.7250, 11),
    ("BJDX",    1.5392, 29),
    ("SHPU",   16.9675, 2),
    ("SWIM",    6.7188, 6),
    ("BLMN",   11.7800, 3),
    ("GSM",     3.8350, 2),
    ("PRGO",   13.1339, 1),
    ("GTE",    11.0200, 4),
    ("APPS",   13.7900, 3),
    ("KTUP",    7.7687, 5),
    ("AMPX.WS", 4.8201, 8),
    ("DIBS",    5.1275, 6),
]

est = pytz.timezone("US/Eastern")
start = datetime(2026, 8, 5, 9, 30, tzinfo=est)
end = datetime(2026, 8, 5, 16, 0, tzinfo=est)

def fetch_bars(symbol, start, end):
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=start, end=end, feed=DataFeed.SIP,
    )
    try:
        resp = client.get_stock_bars(req)
        data_dict = None
        for key, val in resp:
            if key == "data":
                data_dict = val
                break
        if data_dict is None:
            return []
        bars_list = data_dict.get(symbol, [])
        result = []
        for b in bars_list:
            if hasattr(b, "open"):
                result.append({"timestamp": b.timestamp, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume})
            elif isinstance(b, dict):
                result.append({"timestamp": b["timestamp"], "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"], "volume": b["volume"]})
        return result
    except Exception as e:
        print(f"  Error fetching {symbol}: {e}")
        return []

print("Fetching 1-min bars...")
all_bars = {}
for sym, entry, shrs in TRADES:
    bars = fetch_bars(sym, start, end)
    all_bars[sym] = bars
    print(f"  {sym}: {len(bars)} bars")

def simulate_phased_trail(entry_price, shares, bars, wide_pct, tight_pct, tighten_after_pct, time_limit_bars=8):
    if not bars:
        return 0, "no_data"
    entry_idx = 1
    for i in range(1, len(bars)):
        if bars[i]["low"] <= entry_price:
            entry_idx = i
            break
    peak = entry_price
    phase = "wide"
    trail_pct = wide_pct
    for bi in range(entry_idx, len(bars)):
        bar = bars[bi]
        high, low, close = bar["high"], bar["low"], bar["close"]
        if high > peak:
            peak = high
        gain_pct = (peak - entry_price) / entry_price * 100
        if phase == "wide" and gain_pct >= tighten_after_pct:
            phase = "tight"
            trail_pct = tight_pct
        stop_price = round(peak * (1 - trail_pct / 100), 2)
        if low <= stop_price:
            pnl = (stop_price - entry_price) * shares
            return round(pnl, 2), f"trailing_{phase}"
        bars_since_entry = bi - entry_idx
        if time_limit_bars > 0 and bars_since_entry >= time_limit_bars * 5:
            if close >= entry_price:
                pnl = (close - entry_price) * shares
                return round(pnl, 2), "time_limit"
    last_close = bars[-1]["close"]
    pnl = (last_close - entry_price) * shares
    return round(pnl, 2), "force_close"

# Grid search - extended
wide_range = [3, 4, 5, 6, 7, 8, 10]
tight_range = [1, 1.5, 2, 2.5, 3, 4, 5]
tighten_after_range = [1, 2, 3, 4, 5]
time_limit_range = [0, 8, 12, 16]  # 0=disabled, 8=40min, 12=60min, 16=80min

results = []
for wide in wide_range:
    for tight in tight_range:
        if tight >= wide:
            continue
        for tighten_after in tighten_after_range:
            if tighten_after >= wide:
                continue
            for tl in time_limit_range:
                total_pnl = 0
                details = []
                wins = 0
                losses = 0
                for sym, entry, shrs in TRADES:
                    bars = all_bars.get(sym, [])
                    pnl, reason = simulate_phased_trail(entry, shrs, bars, wide, tight, tighten_after, tl)
                    total_pnl += pnl
                    if pnl > 0:
                        wins += 1
                    elif pnl < 0:
                        losses += 1
                    details.append((sym, pnl, reason))
                win_rate = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
                results.append((total_pnl, win_rate, wins, losses, wide, tight, tighten_after, tl, details))

results.sort(key=lambda x: x[0], reverse=True)

print(f"\n{'='*100}")
print(f"{'Rank':<5} {'Wide':<6} {'Tight':<7} {'After':<7} {'TL':<5} {'Total PnL':<11} {'WR':<6} {'W':<4} {'L':<4}")
print(f"{'='*100}")
for i, (pnl, wr, w, l, wide, tight, ta, tl, _) in enumerate(results[:30]):
    tl_str = f"{tl*5}m" if tl > 0 else "off"
    marker = " <<<" if (wide == 10 and tight == 1 and ta == 2 and tl == 8) else ""
    print(f"{i+1:<5} {wide:<6.0f}% {tight:<6.1f}% {ta:<6.0f}% {tl_str:<5} ${pnl:<10.2f} {wr:<6.1f} {w:<4} {l:<4}{marker}")

# Show current params
print("\n--- Current params (10%→1% after 2%, TL 40min) ---")
for pnl, wr, w, l, wide, tight, ta, tl, details in results:
    if wide == 10 and tight == 1 and ta == 2 and tl == 8:
        for sym, s_pnl, reason in details:
            print(f"  {sym:<10} ${s_pnl:>7.2f}  {reason}")
        break

# Show top 5 detailed
print("\n--- Top 5 detailed ---")
for rank, (pnl, wr, w, l, wide, tight, ta, tl, details) in enumerate(results[:5], 1):
    tl_str = f"{tl*5}m" if tl > 0 else "off"
    print(f"\n#{rank}: wide={wide}%, tight={tight}%, after={ta}%, TL={tl_str} → ${pnl:.2f} (WR {wr:.0f}%)")
    for sym, s_pnl, reason in details:
        marker = "  <<< LOSS" if s_pnl < 0 else ""
        print(f"  {sym:<10} ${s_pnl:>7.2f}  {reason}{marker}")
