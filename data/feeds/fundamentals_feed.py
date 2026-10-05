import logging
import math
import os
import time
from core.models import FundamentalData
from data.feeds.price_feed import _yf_lock, _reset_yf_crumb

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 6 * 3600  # 6 hours; fundamentals barely move intraday
_cache: dict[str, tuple[float, FundamentalData]] = {}


def _clean(x: float | None) -> float | None:
    """Pandas/yfinance sometimes hands back NaN or +/-inf for a cell that's
    technically 'present' but empty — treat those exactly like a real None
    everywhere in this file, so nothing but a real number or a clean None
    ever reaches FundamentalData downstream."""
    if x is None:
        return None
    try:
        if math.isnan(x) or math.isinf(x):
            return None
    except TypeError:
        return None
    return x


def _de_from_statements(t) -> float | None:
    """Fallback: compute D/E from the raw balance sheet when info['debtToEquity'] is None.
    Same yfinance object, no extra API call/key. Handles the label variants yfinance
    has used across versions (they renamed several rows between releases)."""
    try:
        bs = t.balance_sheet
        if bs is None or bs.empty:
            return None
        latest = bs.iloc[:, 0]  # most recent period, first column

        total_debt = None
        for label in ("Total Debt", "TotalDebt"):
            if label in latest.index and latest[label] is not None:
                total_debt = float(latest[label])
                break
        if total_debt is None:
            std = 0.0
            ltd = 0.0
            for label in ("Current Debt", "Short Long Term Debt", "Short Term Debt"):
                if label in latest.index and latest[label] is not None:
                    std = float(latest[label])
                    break
            for label in ("Long Term Debt", "LongTermDebt"):
                if label in latest.index and latest[label] is not None:
                    ltd = float(latest[label])
                    break
            total_debt = std + ltd
            if total_debt == 0.0:
                total_debt = None

        equity = None
        for label in ("Stockholders Equity", "Total Stockholder Equity", "Common Stock Equity"):
            if label in latest.index and latest[label] is not None:
                equity = float(latest[label])
                break

        if total_debt is None or not equity:
            return None
        return _clean(total_debt / equity)
    except Exception:
        logger.exception("D/E statement fallback failed")
        return None


def _earnings_growth_from_statements(t) -> float | None:
    """Fallback: compute trailing YoY net income growth from the income statement
    when info['earningsGrowth'] is None (common after a prior-year loss/writedown)."""
    try:
        inc = t.income_stmt
        if inc is None or inc.empty or "Net Income" not in inc.index:
            return None
        row = inc.loc["Net Income"]
        if len(row) < 2:
            return None
        latest, prior = float(row.iloc[0]), float(row.iloc[1])
        if prior == 0:
            return None
        return _clean((latest - prior) / abs(prior))
    except Exception:
        logger.exception("Earnings growth statement fallback failed")
        return None


def _finnhub_fallback(ticker: str) -> dict:
    """Last-resort fallback via Finnhub's basic financials endpoint, only called
    for whichever specific field(s) are still missing after the yfinance layers."""
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        return {}
    try:
        import requests
        resp = requests.get(
            "https://finnhub.io/api/v1/stock/metric",
            params={"symbol": ticker, "metric": "all", "token": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        metric = resp.json().get("metric", {})
        result = {}
        # Finnhub reports totalDebt/totalEquity already as a ratio (x), not a percentage
        de = _clean(metric.get("totalDebt/totalEquityAnnual"))
        if de is not None:
            result["debt_to_equity"] = de
        eg = _clean(metric.get("epsGrowth"))
        if eg is not None:
            result["earnings_growth"] = eg / 100.0
        return result
    except Exception:
        logger.exception("Finnhub fundamentals fallback failed for %s", ticker)
        return {}


def _fetch_fundamentals_live(ticker: str) -> FundamentalData:
    try:
        import yfinance as yf
        with _yf_lock:
            t = yf.Ticker(ticker)
            info = t.info
            if not info:
                logger.warning("yfinance fundamentals empty for %s — resetting crumb and retrying", ticker)
                _reset_yf_crumb()
                t = yf.Ticker(ticker)
                info = t.info

            raw_de = _clean(info.get("debtToEquity"))
            # yfinance returns D/E as a percentage-like figure (e.g. 145.0 = 1.45x); normalize here
            debt_to_equity = raw_de / 100.0 if raw_de is not None else None
            earnings_growth = _clean(info.get("earningsGrowth"))

            # Layer 2: compute from raw financial statements (still yfinance, still automated)
            if debt_to_equity is None:
                debt_to_equity = _de_from_statements(t)
            if earnings_growth is None:
                earnings_growth = _earnings_growth_from_statements(t)

        # Layer 3: Finnhub, only for whatever is still missing after both yfinance layers
        if debt_to_equity is None or earnings_growth is None:
            fh = _finnhub_fallback(ticker)
            if debt_to_equity is None and "debt_to_equity" in fh:
                debt_to_equity = fh["debt_to_equity"]
                logger.info("[%s] D/E filled via Finnhub fallback", ticker)
            if earnings_growth is None and "earnings_growth" in fh:
                earnings_growth = fh["earnings_growth"]
                logger.info("[%s] Earnings growth filled via Finnhub fallback", ticker)

        if debt_to_equity is None:
            logger.warning("[%s] D/E unavailable across yfinance info, statements, and Finnhub", ticker)
        if earnings_growth is None:
            logger.warning("[%s] Earnings growth unavailable across yfinance info, statements, and Finnhub", ticker)

        return FundamentalData(
            ticker=ticker,
            pe_ratio=info.get("trailingPE"),
            pb_ratio=info.get("priceToBook"),
            revenue_growth=info.get("revenueGrowth"),
            earnings_growth=earnings_growth,
            debt_to_equity=debt_to_equity,
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