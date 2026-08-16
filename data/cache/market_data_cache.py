import logging
from datetime import datetime
import pandas as pd
from core.constants import Timeframe
from data.feeds.price_feed import fetch_candles

logger = logging.getLogger(__name__)

# Structure: { ticker: { timeframe: (DataFrame, fetched_at) } }
_cache: dict[str, dict[str, tuple[pd.DataFrame, datetime]]] = {}

# Cache TTL in seconds per timeframe
_TTL: dict[Timeframe, int] = {
    Timeframe.M1:  60,
    Timeframe.M5:  300,
    Timeframe.M15: 900,
    Timeframe.M30: 1800,
    Timeframe.H1:  3600,
    Timeframe.H4:  14400,
    Timeframe.D1:  86400,
    Timeframe.W1:  604800,
    Timeframe.MN1: 2592000,
}


def get_candles(ticker: str, timeframe: Timeframe, force_refresh: bool = False) -> pd.DataFrame:
    now = datetime.utcnow()
    cached = _cache.get(ticker, {}).get(timeframe.value)

    if cached and not force_refresh:
        df, fetched_at = cached
        age = (now - fetched_at).total_seconds()
        if age < _TTL[timeframe]:
            return df

    logger.debug("Fetching candles for %s @ %s", ticker, timeframe.value)
    df = fetch_candles(ticker, timeframe)
    _cache.setdefault(ticker, {})[timeframe.value] = (df, now)
    return df


def invalidate(ticker: str, timeframe: Timeframe | None = None) -> None:
    if ticker not in _cache:
        return
    if timeframe:
        _cache[ticker].pop(timeframe.value, None)
    else:
        _cache.pop(ticker, None)


def warm_up(tickers: list[str], timeframes: list[Timeframe]) -> None:
    """Pre-fill cache at startup."""
    for ticker in tickers:
        for tf in timeframes:
            try:
                get_candles(ticker, tf, force_refresh=False)
            except Exception:
                logger.exception("Cache warm-up failed for %s @ %s", ticker, tf.value)
