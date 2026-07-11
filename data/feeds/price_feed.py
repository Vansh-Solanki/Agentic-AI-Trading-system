import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable
import requests
import pandas as pd
from core.config import POLYGON_API_KEY
from core.constants import Timeframe
from core.models import MarketSnapshot

logger = logging.getLogger(__name__)

# yfinance's cookie/crumb negotiation is not thread-safe — concurrent calls
# (this app runs pipelines via ThreadPoolExecutor) race to establish the
# session crumb and some lose with a 401 "Invalid Crumb". Serialize all
# yfinance calls to avoid the race.
_yf_lock = threading.Lock()

# ── Polygon ────────────────────────────────────────────────────────────────────

_POLYGON_BASE = "https://api.polygon.io"

_POLYGON_TIMESPAN: dict[Timeframe, tuple[int, str]] = {
    Timeframe.M1:  (1,  "minute"),
    Timeframe.M5:  (5,  "minute"),
    Timeframe.M15: (15, "minute"),
    Timeframe.M30: (30, "minute"),
    Timeframe.H1:  (1,  "hour"),
    Timeframe.H4:  (4,  "hour"),
    Timeframe.D1:  (1,  "day"),
    Timeframe.W1:  (1,  "week"),
    Timeframe.MN1: (1,  "month"),
}

_POLYGON_LOOKBACK_DAYS: dict[Timeframe, int] = {
    Timeframe.M1:  7,
    Timeframe.M5:  60,
    Timeframe.M15: 60,
    Timeframe.M30: 60,
    Timeframe.H1:  730,
    Timeframe.H4:  730,
    Timeframe.D1:  1825,
    Timeframe.W1:  3650,
    Timeframe.MN1: 3650,
}


def _polygon_fetch_candles(ticker: str, timeframe: Timeframe) -> pd.DataFrame:
    multiplier, timespan = _POLYGON_TIMESPAN[timeframe]
    lookback = _POLYGON_LOOKBACK_DAYS[timeframe]
    to_dt   = datetime.now(timezone.utc)
    from_dt = to_dt - timedelta(days=lookback)

    url = (
        f"{_POLYGON_BASE}/v2/aggs/ticker/{ticker}/range"
        f"/{multiplier}/{timespan}"
        f"/{from_dt.strftime('%Y-%m-%d')}/{to_dt.strftime('%Y-%m-%d')}"
    )
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": POLYGON_API_KEY}

    results: list[dict] = []
    while url:
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
        except Exception:
            logger.exception("Polygon candle request failed for %s @ %s", ticker, timeframe)
            return pd.DataFrame()

        body = resp.json()
        results.extend(body.get("results") or [])
        next_url = body.get("next_url")
        url    = next_url
        params = {"apiKey": POLYGON_API_KEY} if next_url else {}

    if not results:
        logger.warning("Polygon returned no candles for %s @ %s", ticker, timeframe)
        return pd.DataFrame()

    df = pd.DataFrame(results)
    df["timestamp"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.set_index("timestamp").rename(columns={
        "o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume",
    })[["Open", "High", "Low", "Close", "Volume"]]
    return df


def _polygon_fetch_snapshot(ticker: str) -> MarketSnapshot | None:
    url = f"{_POLYGON_BASE}/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}"
    try:
        resp = requests.get(url, params={"apiKey": POLYGON_API_KEY}, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("ticker", {})
        last_trade = data.get("lastTrade") or data.get("day") or {}
        price  = float(last_trade.get("p") or last_trade.get("c") or 0)
        volume = float((data.get("day") or {}).get("v") or 0)
        return MarketSnapshot(ticker=ticker, price=price, volume=volume, bid=None, ask=None)
    except Exception:
        logger.exception("Polygon snapshot failed for %s", ticker)
        return None

# ── yfinance fallback ──────────────────────────────────────────────────────────

_YF_INTERVAL: dict[Timeframe, str] = {
    Timeframe.M1:  "1m",
    Timeframe.M5:  "5m",
    Timeframe.M15: "15m",
    Timeframe.M30: "30m",
    Timeframe.H1:  "1h",
    Timeframe.H4:  "1h",
    Timeframe.D1:  "1d",
    Timeframe.W1:  "1wk",
    Timeframe.MN1: "1mo",
}

_YF_PERIOD: dict[Timeframe, str] = {
    Timeframe.M1:  "7d",
    Timeframe.M5:  "60d",
    Timeframe.M15: "60d",
    Timeframe.M30: "60d",
    Timeframe.H1:  "730d",
    Timeframe.H4:  "730d",
    Timeframe.D1:  "5y",
    Timeframe.W1:  "10y",
    Timeframe.MN1: "10y",
}


def _reset_yf_crumb() -> None:
    """Force yfinance to re-mint its cookie/crumb.

    yfinance caches the crumb in-process for the life of the run. Once Yahoo
    invalidates it (e.g. after a burst of requests), yfinance has no built-in
    recovery — every subsequent call keeps reusing the same stale crumb and
    fails with 401 "Invalid Crumb" forever. Clearing it here forces a fresh
    cookie/crumb handshake on the next call.
    """
    from yfinance.data import YfData
    yd = YfData()
    with yd._cookie_lock:
        yd._crumb = None
        yd._cookie = None


def _yf_fetch_candles(ticker: str, timeframe: Timeframe) -> pd.DataFrame:
    import yfinance as yf
    interval = _YF_INTERVAL[timeframe]
    period   = _YF_PERIOD[timeframe]
    with _yf_lock:
        df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=True)
        if df.empty:
            logger.warning("yfinance returned empty for %s @ %s — resetting crumb and retrying", ticker, timeframe.value)
            _reset_yf_crumb()
            df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=True)
    if df.empty:
        return df
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def _yf_fetch_snapshot(ticker: str) -> MarketSnapshot:
    import yfinance as yf
    with _yf_lock:
        info = yf.Ticker(ticker).fast_info
        try:
            price = info.last_price
            volume = info.three_month_average_volume
        except Exception:
            logger.warning("yfinance snapshot failed for %s — resetting crumb and retrying", ticker)
            _reset_yf_crumb()
            info = yf.Ticker(ticker).fast_info
            price = info.last_price
            volume = info.three_month_average_volume
    return MarketSnapshot(
        ticker=ticker,
        price=float(price or 0),
        volume=float(volume or 0),
        bid=None,
        ask=None,
    )

# ── Public API ─────────────────────────────────────────────────────────────────


def fetch_candles(ticker: str, timeframe: Timeframe) -> pd.DataFrame:
    """Return OHLCV DataFrame. Uses Polygon when key is set, else yfinance."""
    if POLYGON_API_KEY:
        df = _polygon_fetch_candles(ticker, timeframe)
        if not df.empty:
            return df
        logger.warning("Polygon returned empty for %s @ %s — falling back to yfinance", ticker, timeframe)
    return _yf_fetch_candles(ticker, timeframe)


def fetch_snapshot(ticker: str) -> MarketSnapshot:
    """Fetch a real-time quote. Uses Polygon when key is set, else yfinance."""
    if POLYGON_API_KEY:
        snap = _polygon_fetch_snapshot(ticker)
        if snap is not None:
            return snap
    return _yf_fetch_snapshot(ticker)


class PriceFeedStream:
    """Minimal polling-based price stream."""

    def __init__(self, tickers: list[str], interval_seconds: int = 60):
        self.tickers = tickers
        self.interval_seconds = interval_seconds
        self._callbacks: list[Callable[[MarketSnapshot], None]] = []

    def subscribe(self, callback: Callable[[MarketSnapshot], None]) -> None:
        self._callbacks.append(callback)

    def _emit(self, snapshot: MarketSnapshot) -> None:
        for cb in self._callbacks:
            try:
                cb(snapshot)
            except Exception:
                logger.exception("Price feed callback error")

    def poll_once(self) -> None:
        for ticker in self.tickers:
            try:
                snap = fetch_snapshot(ticker)
                self._emit(snap)
            except Exception:
                logger.exception("Failed to fetch snapshot for %s", ticker)
