"""Market calendar — Alpaca trading day lookup with caching."""

import datetime as dt
from zoneinfo import ZoneInfo

EST = ZoneInfo("America/New_York")
_CACHE: dict[str, dict] = {}


def _fetch_calendar(client, start: dt.date, end: dt.date) -> dict[str, dict]:
    """Fetch calendar from Alpaca and return {YYYY-MM-DD: {date, open, close, is_early_close}}."""
    from alpaca.trading.requests import GetCalendarRequest

    entries = client.get_calendar(GetCalendarRequest(start=start, end=end))
    result = {}
    for e in entries:
        d = str(e.date)
        # open/close may be datetime or string depending on SDK version
        open_t = e.open
        close_t = e.close
        if hasattr(open_t, "strftime"):
            open_str = open_t.strftime("%H:%M")
        else:
            open_str = str(open_t)[-8:-3] if len(str(open_t)) > 5 else str(open_t)
        if hasattr(close_t, "strftime"):
            close_str = close_t.strftime("%H:%M")
        else:
            close_str = str(close_t)[-8:-3] if len(str(close_t)) > 5 else str(close_t)
        result[d] = {
            "date": d,
            "open": open_str,
            "close": close_str,
            "is_early_close": close_str != "16:00",
        }
    return result


def _ensure_cache(client, target_date: dt.date):
    """Ensure cache covers the target date plus 30 days forward."""
    start = target_date
    end = target_date + dt.timedelta(days=45)
    cache_key = f"{start}_{end}"
    if cache_key in _CACHE:
        return
    data = _fetch_calendar(client, start, end)
    _CACHE[cache_key] = data
    # Also store by individual date for fast lookup
    for d, info in data.items():
        _CACHE[d] = info


def is_trading_day(client, date: dt.date | str) -> bool:
    """Check if a date is a trading day."""
    d = str(date) if isinstance(date, dt.date) else date
    _ensure_cache(client, dt.date.fromisoformat(d[:10]))
    return d in _CACHE and isinstance(_CACHE[d], dict) and "open" in _CACHE[d]


def get_trading_day_info(client, date: dt.date | str) -> dict | None:
    """Return trading day info or None if not a trading day.

    Returns: {"date": "2026-07-10", "open": "09:30", "close": "16:00", "is_early_close": False}
    """
    d = str(date) if isinstance(date, dt.date) else date
    _ensure_cache(client, dt.date.fromisoformat(d[:10]))
    entry = _CACHE.get(d)
    if entry and isinstance(entry, dict) and "open" in entry:
        return entry
    return None


def get_next_trading_day(client, after_date: dt.date | str) -> dict:
    """Return the next trading day info after the given date."""
    d = dt.date.fromisoformat(str(after_date)[:10])
    _ensure_cache(client, d + dt.timedelta(days=1))
    for i in range(1, 50):
        candidate = d + dt.timedelta(days=i)
        key = str(candidate)
        entry = _CACHE.get(key)
        if entry and isinstance(entry, dict) and "open" in entry:
            return entry
    # Fallback: fetch more
    _ensure_cache(client, d + dt.timedelta(days=1))
    for i in range(1, 50):
        candidate = d + dt.timedelta(days=i)
        key = str(candidate)
        entry = _CACHE.get(key)
        if entry and isinstance(entry, dict) and "open" in entry:
            return entry
    raise RuntimeError("No trading day found in next 50 days")


def calc_force_close_time(close_str: str, margin_min: int = 10) -> str:
    """Calculate force close time = close_time - margin_min.

    >>> calc_force_close_time("16:00")  # "15:50"
    >>> calc_force_close_time("13:00")  # "12:50"
    """
    h, m = int(close_str[:2]), int(close_str[3:5])
    total_min = h * 60 + m - margin_min
    return f"{total_min // 60:02d}:{total_min % 60:02d}"


def get_market_datetime(close_str: str, date_str: str, is_open: bool = False) -> dt.datetime:
    """Convert a market time string to an aware datetime in EST.

    is_open=True → use open time, False → use close time
    """
    time_str = close_str  # or open_str if needed
    h, m = int(time_str[:2]), int(time_str[3:5])
    d = dt.date.fromisoformat(date_str[:10])
    return dt.datetime(d.year, d.month, d.day, h, m, tzinfo=EST)
