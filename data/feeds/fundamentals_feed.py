import logging
import time
from core.models import FundamentalData
from data.feeds.price_feed import _yf_lock, _reset_yf_crumb

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 6 * 3600  # 6 hours; fundamentals barely move intraday
_cache: dict[str, tuple[float, FundamentalData]] = {}


def _fetch_fundamentals_live(ticker: str) -> FundamentalData:
    try:
        import yfinance as yf
        with _yf_lock:
            info = yf.Ticker(ticker).info
            if not info:
                logger.warning("yfinance fundamentals empty for %s — resetting crumb and retrying", ticker)
                _reset_yf_crumb()
                info = yf.Ticker(ticker).info
        raw_de = info.get("debtToEquity")
        return FundamentalData(
            ticker=ticker,
            pe_ratio=info.get("trailingPE"),
            pb_ratio=info.get("priceToBook"),
            revenue_growth=info.get("revenueGrowth"),
            earnings_growth=info.get("earningsGrowth"),
            # yfinance returns D/E as a percentage-like figure (e.g. 145.0 = 1.45x); normalize here
            debt_to_equity=raw_de / 100.0 if raw_de is not None else None,
            free_cash_flow=info.get("freeCashflow"),
            market_cap=info.get("marketCap"),
            sector=info.get("sector", ""),
        )
    except Exception:
        logger.exception("Fundamental fetch failed for %s", ticker)
        return FundamentalData(
            ticker=ticker, pe_ratio=None, pb_ratio=None,
            revenue_growth=None, earnings_growth=None,
            debt_to_equity=None, free_cash_flow=None,
            market_cap=None, sector="",
        )


def fetch_fundamentals(ticker: str) -> FundamentalData:
    now = time.monotonic()
    cached = _cache.get(ticker)
    if cached is not None:
        ts, data = cached
        if now - ts < _CACHE_TTL_SECONDS:
            return data
    data = _fetch_fundamentals_live(ticker)
    _cache[ticker] = (now, data)
    return data
