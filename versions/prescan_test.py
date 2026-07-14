"""Pre-market scan test — verify SIP data quality at 9:20 EST.
Logs scan timing, candidate count, and price accuracy vs IEX.
Results saved to versions/prescan_test.log
"""

import time, datetime as dt, re, json
from zoneinfo import ZoneInfo
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

_LEV_PATTERN = re.compile(r'(2X|3X|BULL|BEAR)$', re.IGNORECASE)
def is_leveraged_etf(symbol):
    if _LEV_PATTERN.search(symbol): return True
    if len(symbol) > 3 and symbol[-1] in ('U','L'): return True
    if any(symbol.startswith(p) for p in ('TQQQ','SQQQ','UPRO','SPXU','TNA','TZA',
        'MSTU','MSTZ','CONL','NAIL','WEBL','FNGU','FNGD','SOXL','SOXS','TECL','TECS',
        'UDOW','SDOW','UMDD','SMDD','YINN','YANG','CURE','LABD','LABU','DRN','DRV',
        'DGP','DGZ','BOIL','KOLD','NUGT','DUST','JNUG','JDST','GLL','UGL')): return True
    return False

tc = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=config.ALPACA_PAPER)
dc = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)

_LOG_FILE = os.path.join(_ver_dir, "prescan_test.log")
_RESULT_FILE = os.path.join(_ver_dir, "prescan_result.json")

def log(msg):
    now = dt.datetime.now().strftime("%H:%M:%S")
    line = f"[{now}] {msg}"
    print(line, flush=True)
    with open(_LOG_FILE, "a") as f:
        f.write(line + "\n")


def scan_with_feed(feed_enum, symbols, feed_name):
    today = dt.date.today()
    yesterday = today - pd.Timedelta(days=5)
    end = pd.Timestamp(today, tz="America/New_York") + pd.Timedelta(days=1)
    batch_size = 500
    results = []
    t0 = time.time()

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        request = StockBarsRequest(
            symbol_or_symbols=batch, timeframe=TimeFrame.Day,
            start=yesterday, end=end, adjustment=Adjustment.RAW, feed=feed_enum,
        )
        try:
            bars = dc.get_stock_bars(request)
        except Exception as e:
            log(f"  API error: {str(e)[:60]}")
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

    elapsed = time.time() - t0
    results.sort(key=lambda x: x["gap_pct"], reverse=True)
    return results, elapsed


def main():
    log("=" * 60)
    log("Pre-market Scan Test — SIP vs IEX")
    log(f"Date: {dt.date.today()}")
    log(f"Config: GAP_THRESHOLD={config.GAP_THRESHOLD}, DATA_FEED={config.DATA_FEED}")
    log("=" * 60)

    # Get symbols
    t0 = time.time()
    symbols = get_tradable_symbols()
    symbols = [s for s in symbols if not is_leveraged_etf(s)]
    log(f"Loaded {len(symbols)} symbols in {time.time()-t0:.1f}s")

    # SIP scan
    log("")
    log(">>> SIP Scan <<<")
    sip_results, sip_time = scan_with_feed(DataFeed.SIP, symbols, "SIP")
    log(f"SIP: found {len(sip_results)} gaps in {sip_time:.1f}s")
    for r in sip_results[:10]:
        log(f"  {r['symbol']:6s} gap +{r['gap_pct']:.1%}  open=${r['open_price']:.4f}")

    # IEX scan
    log("")
    log(">>> IEX Scan <<<")
    iex_results, iex_time = scan_with_feed(DataFeed.IEX, symbols, "IEX")
    log(f"IEX: found {len(iex_results)} gaps in {iex_time:.1f}s")
    for r in iex_results[:10]:
        log(f"  {r['symbol']:6s} gap +{r['gap_pct']:.1%}  open=${r['open_price']:.4f}")

    # Comparison
    sip_map = {r['symbol']: r for r in sip_results}
    iex_map = {r['symbol']: r for r in iex_results}
    sip_only = set(sip_map.keys()) - set(iex_map.keys())
    iex_only = set(iex_map.keys()) - set(sip_map.keys())
    common = set(sip_map.keys()) & set(iex_map.keys())

    log("")
    log("=== Comparison ===")
    log(f"SIP only: {len(sip_only)} | IEX only: {len(iex_only)} | Common: {len(common)}")
    if sip_only:
        log(f"  SIP only: {sorted(sip_only)}")

    # Price comparison for common symbols
    if common:
        log("")
        log(f"{'Symbol':6s} {'SIP open':>10s} {'IEX open':>10s} {'Diff':>8s}")
        log("-" * 38)
        for sym in sorted(common, key=lambda s: abs(sip_map[s]['open_price'] - iex_map[s]['open_price']), reverse=True):
            sr = sip_map[sym]
            ir = iex_map[sym]
            diff = (sr['open_price'] - ir['open_price']) / ir['open_price'] * 100
            log(f"{sym:6s} ${sr['open_price']:9.4f} ${ir['open_price']:9.4f} {diff:+7.3f}%")

    # Refresh test: get snapshots at market open
    log("")
    est_now = dt.datetime.now(tz=ZoneInfo("America/New_York"))
    log(f"Current EST time: {est_now.strftime('%H:%M:%S')}")

    if sip_results:
        test_syms = [r['symbol'] for r in sip_results[:5]]
        try:
            snaps = dc.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=test_syms, feed=DataFeed.SIP))
            log("SIP snapshot test:")
            for sym, snap in snaps.items():
                if snap and snap.latest_trade:
                    log(f"  {sym}: ${float(snap.latest_trade.price):.4f}")
                if snap and snap.daily_bar:
                    log(f"  {sym} daily_bar: O={float(snap.daily_bar.open):.4f}")
        except Exception as e:
            log(f"SIP snapshot failed: {e}")

    # Save results
    result = {
        "date": str(dt.date.today()),
        "scan_time_est": est_now.strftime("%H:%M:%S"),
        "sip_gaps": len(sip_results),
        "iex_gaps": len(iex_results),
        "sip_only": sorted(sip_only),
        "iex_only": sorted(iex_only),
        "sip_time_s": round(sip_time, 1),
        "iex_time_s": round(iex_time, 1),
        "sip_candidates": sip_results[:20],
        "iex_candidates": iex_results[:20],
    }
    with open(_RESULT_FILE, "w") as f:
        json.dump(result, f, indent=2, default=str)
    log(f"\nResults saved to {_RESULT_FILE}")
    log("=" * 60)


if __name__ == "__main__":
    main()
