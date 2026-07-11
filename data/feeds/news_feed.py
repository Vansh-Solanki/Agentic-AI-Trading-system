import logging
import threading
import time
import requests
from datetime import datetime, timedelta, timezone
from core.config import FINNHUB_API_KEY, BENZINGA_API_KEY
from core.models import NewsItem

logger = logging.getLogger(__name__)

# Session 34 Bug B interim fix: WebSocket news streaming (subscribe-news) is not
# available on the current Finnhub plan (confirmed via scripts/test_finnhub_ws.py).
# Without it, every NewsSentimentAgent cycle across 20 tickers x up to 5 parallel
# Phase-1 threads was hammering Finnhub with REST calls, causing free-tier rate
# limiting to hang connections instead of returning a clean 429 — the hang then
# blew through the orchestrator's 30s Phase-1 timeout for every ticker in the
# batch. Fixed with a hard concurrency cap on Finnhub calls plus a short-TTL
# cache so the same ticker/window isn't re-fetched more than once per 5 minutes.
_finnhub_semaphore = threading.Semaphore(2)
_news_cache: dict[tuple[str, int], tuple[float, list[NewsItem]]] = {}
_news_cache_lock = threading.Lock()
_NEWS_CACHE_TTL_SECONDS = 300

# ── Benzinga ───────────────────────────────────────────────────────────────────

_BENZINGA_URL = "https://api.benzinga.com/api/v2/news"


def _fetch_ticker_news_benzinga(ticker: str, from_dt: datetime, to_dt: datetime) -> list[NewsItem]:
    params = {
        "token":         BENZINGA_API_KEY,
        "tickers":       ticker,
        "dateFrom":      from_dt.strftime("%Y-%m-%d"),
        "dateTo":        to_dt.strftime("%Y-%m-%d"),
        "displayOutput": "abstract",
        "pageSize":      50,
    }
    try:
        # Benzinga defaults to XML; JSON must be negotiated via Accept header.
        resp = requests.get(_BENZINGA_URL, params=params, headers={"Accept": "application/json"}, timeout=10)
        resp.raise_for_status()
        articles = resp.json()
    except Exception:
        logger.exception("Benzinga news request failed for %s", ticker)
        return []

    items: list[NewsItem] = []
    for a in articles:
        raw_date = a.get("created") or a.get("updated") or ""
        try:
            published = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        except Exception:
            published = datetime.now(timezone.utc)

        stocks = a.get("stocks") or []
        tickers = [s["name"] for s in stocks if s.get("name")] or [ticker]

        items.append(NewsItem(
            headline=a.get("title", ""),
            source="benzinga",
            url=a.get("url", ""),
            published_at=published,
            tickers=tickers,
            summary=a.get("teaser", "") or "",
        ))
    return items

# ── Finnhub fallback ───────────────────────────────────────────────────────────

_FINNHUB_NEWS_URL = "https://finnhub.io/api/v1/company-news"


def _fetch_ticker_news_finnhub(ticker: str, from_dt: datetime, to_dt: datetime) -> list[NewsItem]:
    if not FINNHUB_API_KEY:
        logger.warning("No news API key set (BENZINGA_API_KEY or FINNHUB_API_KEY) — skipping fetch")
        return []

    params = {
        "symbol": ticker,
        "from":   from_dt.strftime("%Y-%m-%d"),
        "to":     to_dt.strftime("%Y-%m-%d"),
        "token":  FINNHUB_API_KEY,
    }
    try:
        with _finnhub_semaphore:
            resp = requests.get(_FINNHUB_NEWS_URL, params=params, timeout=10)
        resp.raise_for_status()
        articles = resp.json()
    except Exception:
        logger.exception("Finnhub news request failed for %s", ticker)
        return []

    items: list[NewsItem] = []
    for a in articles:
        published = datetime.fromtimestamp(a.get("datetime", 0), tz=timezone.utc)
        items.append(NewsItem(
            headline=a.get("headline", ""),
            source=a.get("source", ""),
            url=a.get("url", ""),
            published_at=published,
            tickers=[ticker],
            summary=a.get("summary", "") or "",
        ))
    return items

# ── Dispatcher ─────────────────────────────────────────────────────────────────


def _fetch_ticker_news(ticker: str, from_dt: datetime, to_dt: datetime) -> list[NewsItem]:
    # Cache key buckets by (ticker, lookback window) rather than exact timestamps —
    # from_dt/to_dt shift by seconds on every call ("now"), so caching on the exact
    # window would never hit. Freshness only needs to be within the TTL anyway.
    hours_back = round((to_dt - from_dt).total_seconds() / 3600)
    cache_key = (ticker, hours_back)
    now = time.monotonic()

    with _news_cache_lock:
        cached = _news_cache.get(cache_key)
        if cached and now - cached[0] < _NEWS_CACHE_TTL_SECONDS:
            return cached[1]

    if BENZINGA_API_KEY:
        items = _fetch_ticker_news_benzinga(ticker, from_dt, to_dt)
        if items:
            with _news_cache_lock:
                _news_cache[cache_key] = (now, items)
            return items
        logger.warning("Benzinga returned no items for %s — falling back to Finnhub", ticker)

    items = _fetch_ticker_news_finnhub(ticker, from_dt, to_dt)
    with _news_cache_lock:
        _news_cache[cache_key] = (now, items)
    return items

# ── Public API ─────────────────────────────────────────────────────────────────


def fetch_news(tickers: list[str], hours_back: int = 24, max_articles: int = 50) -> list[NewsItem]:
    """Fetch recent news. Uses Benzinga when key is set, else Finnhub."""
    to_dt   = datetime.now(timezone.utc)
    from_dt = to_dt - timedelta(hours=hours_back)

    items: list[NewsItem] = []
    for ticker in tickers:
        items.extend(_fetch_ticker_news(ticker, from_dt, to_dt))

    items.sort(key=lambda i: i.published_at, reverse=True)
    return items[:max_articles]


def fetch_premarket_news(tickers: list[str]) -> list[NewsItem]:
    """Fetch overnight / pre-market news (last 12 hours)."""
    return fetch_news(tickers, hours_back=12)


def fetch_intraday_news(tickers: list[str]) -> list[NewsItem]:
    """Fetch very recent news for spike detection (last 1 hour)."""
    return fetch_news(tickers, hours_back=1, max_articles=20)
