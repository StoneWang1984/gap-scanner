"""ADVB trade analysis: why no profit tier sell was placed."""
import sys; sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv('/Users/stonewang2014/gap-scanner/.env')
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed as DF
from alpaca.data.timeframe import TimeFrame
import os, datetime

api_key = os.getenv('ALPACA_API_KEY')
secret = os.getenv('ALPACA_SECRET_KEY')
data_client = StockHistoricalDataClient(api_key, secret)

entry_price = 15.9851

req = StockBarsRequest(symbol_or_symbols='ADVB', timeframe=TimeFrame.Day,
                       start=datetime.datetime(2026,7,22), end=datetime.datetime(2026,7,22,23,59), feed=DF.SIP)
bars = data_client.get_stock_bars(req)
prev_close = float(bars['ADVB'][0].close)

req = StockBarsRequest(symbol_or_symbols='ADVB', timeframe=TimeFrame.Day,
                       start=datetime.datetime(2026,7,23), end=datetime.datetime(2026,7,23,23,59), feed=DF.SIP)
bars = data_client.get_stock_bars(req)
open_price = float(bars['ADVB'][0].open)
day_high = float(bars['ADVB'][0].high)

gap_pct = (open_price - prev_close) / prev_close

retracements = [0.25, 0.50, 0.75, 1.00, 1.25, 1.50]
caps = [0.05, 0.10, 0.15, 0.20, 0.25, 0.35]

print("=" * 65)
print("ADVB why no profit tier sell was placed")
print("=" * 65)
print("prev_close: $%.2f  open: $%.2f  gap: +%.1f%%" % (prev_close, open_price, gap_pct*100))
print("entry: $%.4f  day_high: $%.2f (+%.1f%%)" % (entry_price, day_high, (day_high/entry_price-1)*100))
print()
print("-- 6 target tiers (all blocked by bug) --")
print()
for i, (r, c) in enumerate(zip(retracements, caps)):
    retracement_price = open_price + r * (prev_close - open_price)
    cap_price = entry_price * (1 + c)
    target = min(retracement_price, cap_price)
    if target < entry_price:
        desc = "below entry -> instant fill (skip-gap)"
    elif day_high >= target:
        desc = "price reached $20.87 -> should fill"
    else:
        desc = "price not reached"
    print("  T%d (%d%% retracement, %d%% cap): target=$%.2f | %s | NOT placed (stop held 8 shares)" % (i+1, int(r*100), int(c*100), target, desc))

print()
print("-- Root cause --")
print("Stop-limit order held ALL 8 shares via held_for_orders")
print("T1 sell 1 share -> Alpaca rejected: available=0, held=8")
print("T2-T6 same, all could not be placed")
print("Price rose to $20.87 (+30.6%) but NO sell orders active")
print("Price fell back, PULLBACK STOP, all sold @ $15.7845 -> loss $1.60")
print()
print("-- After fix --")
sold = 0
profit = 0
for i, (r, c) in enumerate(zip(retracements, caps)):
    retracement_price = open_price + r * (prev_close - open_price)
    cap_price = entry_price * (1 + c)
    target = min(retracement_price, cap_price)
    if target < entry_price or day_high >= target:
        sell_p = max(target, entry_price) if target >= entry_price else entry_price
        p = (sell_p - entry_price) * 1
        profit += p
        sold += 1
        print("  T%d @ $%.2f -> 1 share profit $%.2f" % (i+1, target, p))

rem = 8 - sold
print("  %d tiers sold, total profit $%.2f" % (sold, profit))
print("  Remaining %d shares protected by trailing stop" % rem)
print()
print("  Bug result: LOSS $1.60")
print("  Fix estimate: PROFIT $%.2f" % profit)
print("  Difference: $%.2f" % (profit + 1.60))
