"""Pre-market gap scanner using Alpaca historical data."""

import pandas as pd
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
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
        paper=True,
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
    """
    start = date - pd.Timedelta(days=7)  # look back to ensure we get prev bar
    end = date + pd.Timedelta(days=1)

    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        adjustment=Adjustment.RAW,
        feed=DataFeed.IEX,
    )

    try:
        bars = client.get_stock_bars(request)
    except Exception as e:
        print(f"  API error fetching bars: {e}")
        return pd.DataFrame(columns=["symbol", "prev_close", "open_price", "gap_pct", "prev_volume"])

    if bars.df.empty:
        return pd.DataFrame(columns=["symbol", "prev_close", "open_price", "gap_pct", "prev_volume"])

    df = bars.df

    results = []
    for symbol in symbols:
        try:
            sym_df = df[df.index.get_level_values("symbol") == symbol].copy()
            if len(sym_df) < 2:
                continue

            sym_df = sym_df.sort_index()

            # Find the bar for our target date
            target_mask = sym_df.index.get_level_values("timestamp").date == date.date()
            target_bar = sym_df[target_mask]
            if target_bar.empty:
                continue
            target_bar = target_bar.iloc[-1]

            # Find the previous bar (before target date)
            prev_mask = sym_df.index.get_level_values("timestamp").date < date.date()
            prev_bars = sym_df[prev_mask]
            if prev_bars.empty:
                continue
            prev_bar = prev_bars.iloc[-1]

            prev_close = prev_bar["close"]
            open_price = target_bar["open"]
            prev_volume = prev_bar["volume"]

            if prev_close <= 0:
                continue

            gap_pct = (open_price / prev_close) - 1.0

            # Apply filters
            if gap_pct < config.GAP_THRESHOLD:
                continue
            if prev_volume < config.MIN_VOLUME:
                continue
            if not (config.PRICE_MIN <= open_price <= config.PRICE_MAX):
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
