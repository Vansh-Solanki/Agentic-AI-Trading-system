import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo
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


_NY_TZ = ZoneInfo("America/New_York")


def _flatten_yf_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Session 52 (Bug 7): current yfinance versions return a two-level
    (Price, Ticker) column MultiIndex from yf.download() even for a single
    ticker. Every downstream consumer (signals/, agents/, risk/, news/)
    expects a flat Open/High/Low/Close/Volume frame — matching what the
    Polygon path already returns — so df["Close"] is a plain Series, not a
    one-column DataFrame. Pure function, no I/O, so it's unit-testable
    without network access. Deliberately keyed off `isinstance(..., MultiIndex)`
    rather than yf.download()'s own multi_level_index/group_by kwargs, whose
    names and defaults have changed across yfinance versions — this works
    regardless of which version produced the frame.
    """
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    return df


def _resample_1h_to_4h_session(df: pd.DataFrame) -> pd.DataFrame:
    """
    Session 52 (Bug 7): yfinance has no native 4-hour interval, so the H4
    path used to silently request "1h" and return 1-hour bars labeled as
    4-hour (confirmed live: index spaced exactly one hour apart). This builds
    real session-aligned 4-hour bars out of 1-hour bars instead of resampling
    on a fixed UTC clock grid, which would misalign with the exchange session
    (9:30 AM ET open) and drift across DST transitions.

    Rule: convert to America/New_York, drop NaN-Close rows (a yfinance
    quirk), then for each local calendar date split into two groups — bars
    before 13:30 local (group 1: a normal day's 9:30-12:30) and bars at/after
    13:30 (group 2: 13:30-15:30+). An early-close day with no afternoon bars
    naturally produces only group 1 for that date. Each group aggregates with
    the standard OHLCV combine rule (Open=first, High=max, Low=min,
    Close=last, Volume=sum) and is labeled with its first bar's timestamp.

    The most recent group is kept even if it's still forming (e.g. only one
    or two of the expected ~4 hourly bars have printed so far) — consistent
    with how the 5m/15m/1h paths already return their latest, possibly-
    incomplete bar rather than waiting for it to close.

    The Polygon path (data/feeds/price_feed.py's primary path, not currently
    in use — no POLYGON_API_KEY is set) requests native 4-hour bars directly
    from Polygon and is NOT touched by this function; Polygon's own 4-hour
    bar alignment (likely a fixed UTC/exchange-time grid, not session-half
    aligned) can differ from what this produces.
    """
    if df.empty:
        return df
    df = df[df["Close"].notna()]
    if df.empty:
        return df

    local_index = df.index.tz_convert(_NY_TZ)
    is_second_half = (local_index.hour > 13) | ((local_index.hour == 13) & (local_index.minute >= 30))

    df = df.copy()
    df["_date"] = local_index.date
    df["_half"] = is_second_half

    rows = []
    for _, group in df.groupby(["_date", "_half"], sort=True):
        rows.append({
            "timestamp": group.index[0],
            "Open":   group["Open"].iloc[0],
            "High":   group["High"].max(),
            "Low":    group["Low"].min(),
            "Close":  group["Close"].iloc[-1],
            "Volume": group["Volume"].sum(),
        })
    out = pd.DataFrame(rows).set_index("timestamp").sort_index()
    out.index = pd.to_datetime(out.index, utc=True)
    return out[["Open", "High", "Low", "Close", "Volume"]]


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
    # Session 52 (Bug 7): yfinance has no native 4-hour interval. H4 now
    # fetches real 1-hour bars and builds session-aligned 4-hour bars from
    # them (_resample_1h_to_4h_session) instead of returning 1-hour bars
    # mislabeled as 4-hour.
    fetch_timeframe = Timeframe.H1 if timeframe == Timeframe.H4 else timeframe
    interval = _YF_INTERVAL[fetch_timeframe]
    period   = _YF_PERIOD[fetch_timeframe]
    with _yf_lock:
        df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=True)
        df = _flatten_yf_columns(df)
        if df.empty:
            logger.warning("yfinance returned empty for %s @ %s — resetting crumb and retrying",
                            ticker, fetch_timeframe.value)
            _reset_yf_crumb()
            df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=True)
            df = _flatten_yf_columns(df)
    if df.empty:
        return df
    df.index = pd.to_datetime(df.index, utc=True)
    if timeframe == Timeframe.H4:
        df = _resample_1h_to_4h_session(df)
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
