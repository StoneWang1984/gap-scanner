"""Pre-market gap scanner using Alpaca historical data."""

import re
import pandas as pd
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import Adjustment, DataFeed

import config


def get_data_client():
    return StockHistoricalDataClient(
        api_key=config.ALPACA_API_KEY,
        secret_key=config.ALPACA_SECRET_KEY,
    )


def get_tradable_symbols() -> list[str]:
    """Get all active, tradable US stock symbols from Alpaca."""
    from alpaca.trading.client import TradingClient
    trading_client = TradingClient(
        api_key=config.ALPACA_API_KEY,
        secret_key=config.ALPACA_SECRET_KEY,
        paper=getattr(config, "ALPACA_PAPER", True),
    )
    assets = trading_client.get_all_assets()
    symbols = [
        a.symbol for a in assets
        if a.tradable and a.status == "active" and a.exchange in (
            "NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"
        )
    ]
    return symbols


def scan_gaps_for_symbols(
    client: StockHistoricalDataClient,
    date: pd.Timestamp,
    symbols: list[str],
) -> pd.DataFrame:
    """Scan for gap-up stocks among given symbols on a given date.

    Compares previous close to current day open. Returns DataFrame
    with columns: symbol, prev_close, open_price, gap_pct, prev_volume.

    If daily bar for today doesn't exist yet (pre-market), falls back
    to 1-min pre-market bars to estimate open price.
    """
    start = date - pd.Timedelta(days=7)  # look back to ensure we get prev bar
    end = date + pd.Timedelta(days=1)

    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        adjustment=Adjustment.RAW,
        feed=getattr(config, "DATA_FEED_OBJ", DataFeed.IEX),
    )

    try:
        bars = client.get_stock_bars(request)
    except Exception as e:
        print(f"  API error fetching bars: {e}")
        return pd.DataFrame(columns=["symbol", "prev_close", "open_price", "gap_pct", "prev_volume"])

    if bars.df.empty:
        return pd.DataFrame(columns=["symbol", "prev_close", "open_price", "gap_pct", "prev_volume"])

    df = bars.df

    # Check which symbols have today's daily bar
    symbols_with_today = set()
    symbols_without_today = []
    if not df.empty:
        today_mask = df.index.get_level_values("timestamp").date == date.date()
        symbols_with_today = set(df[today_mask].index.get_level_values("symbol"))

    for sym in symbols:
        if sym not in symbols_with_today:
            symbols_without_today.append(sym)

    # Fallback: use snapshot API for pre-market open price
    premarket_opens = {}
    if symbols_without_today:
        try:
            snap_req = StockSnapshotRequest(
                symbol_or_symbols=symbols_without_today,
                feed=getattr(config, "DATA_FEED_OBJ", DataFeed.IEX),
            )
            snapshots = client.get_stock_snapshot(snap_req)
            for sym, snap in snapshots.items():
                if snap and snap.latest_trade and snap.daily_bar:
                    premarket_opens[sym] = float(snap.latest_trade.price)
                    # Also store prev_close from snapshot if daily bar doesn't have it
                    if sym not in symbols_with_today:
                        premarket_opens[f"{sym}_prev_close"] = float(snap.daily_bar.close)
        except Exception as e:
            pass  # Snapshot not available, skip

    results = []
    for symbol in symbols:
        try:
            sym_df = df[df.index.get_level_values("symbol") == symbol].copy()

            # Find the previous bar (before target date) from daily bars
            prev_close = None
            prev_volume = 0
            open_price = None

            if not sym_df.empty:
                sym_df = sym_df.sort_index()
                target_mask = sym_df.index.get_level_values("timestamp").date == date.date()
                target_bar = sym_df[target_mask]

                prev_mask = sym_df.index.get_level_values("timestamp").date < date.date()
                prev_bars = sym_df[prev_mask]
                if not prev_bars.empty:
                    prev_close = prev_bars.iloc[-1]["close"]
                    prev_volume = prev_bars.iloc[-1]["volume"]

                if not target_bar.empty:
                    open_price = target_bar.iloc[-1]["open"]

            # Fallback: use snapshot for symbols without today's daily bar
            if open_price is None and symbol in premarket_opens:
                open_price = premarket_opens[symbol]
            if prev_close is None and f"{symbol}_prev_close" in premarket_opens:
                prev_close = premarket_opens[f"{symbol}_prev_close"]

            if open_price is None or prev_close is None or prev_close <= 0:
                continue

            if prev_close <= 0:
                continue

            gap_pct = (open_price / prev_close) - 1.0

            # Apply filters
            if gap_pct < config.GAP_THRESHOLD:
                continue
            if gap_pct > getattr(config, "GAP_MAX", 1.0):
                continue
            if prev_volume > 0 and prev_volume < config.MIN_VOLUME:
                continue
            dollar_volume = prev_close * prev_volume
            if dollar_volume < getattr(config, "MIN_DOLLAR_VOLUME", 0):
                continue
            if not (config.PRICE_MIN <= open_price <= config.PRICE_MAX):
                continue
            # Leveraged ETF filter
            lev_suffixes = getattr(config, "LEVERAGED_ETF_SUFFIXES", ())
            lev_prefixes = getattr(config, "LEVERAGED_ETF_PREFIXES", ())
            _LEV_PATTERN = re.compile(r'(2X|3X|BULL|BEAR)$', re.IGNORECASE)
            if _LEV_PATTERN.search(symbol):
                continue
            if any(symbol.startswith(p) for p in lev_prefixes):
                continue
            if any(symbol.endswith(s) for s in lev_suffixes):
                continue

            results.append({
                "symbol": symbol,
                "prev_close": prev_close,
                "open_price": open_price,
                "gap_pct": gap_pct,
                "prev_volume": prev_volume,
            })
        except (KeyError, IndexError):
            continue

    return pd.DataFrame(results)


def scan_gaps_batch(
    client: StockHistoricalDataClient,
    date: pd.Timestamp,
    symbols: list[str] | None = None,
    batch_size: int = 200,
) -> pd.DataFrame:
    """Scan in batches to avoid API rate limits and timeouts."""
    if symbols is None:
        print("  Loading tradable symbols...")
        symbols = get_tradable_symbols()
        print(f"  Found {len(symbols)} tradable symbols")

    all_results = []
    total_batches = (len(symbols) + batch_size - 1) // batch_size

    for i in range(0, len(symbols), batch_size):
        batch_num = i // batch_size + 1
        batch = symbols[i : i + batch_size]
        print(f"  Scanning batch {batch_num}/{total_batches} ({len(batch)} symbols)...")
        df = scan_gaps_for_symbols(client, date, batch)
        if not df.empty:
            all_results.append(df)

    if all_results:
        return pd.concat(all_results, ignore_index=True)
    return pd.DataFrame(columns=["symbol", "prev_close", "open_price", "gap_pct", "prev_volume"])
