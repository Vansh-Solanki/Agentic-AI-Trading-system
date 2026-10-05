import logging
import numpy as np
from data.storage.decision_store import get_open_trades
import core.config as cfg

logger = logging.getLogger(__name__)

_SECTOR_MAP: dict[str, str] = {
    # Technology
    "NVDA": "technology",
    "MSFT": "technology",
    "AAPL": "technology",
    "PLTR": "technology",
    # Financials
    "JPM":  "financials",
    "GS":   "financials",
    "V":    "financials",
    # Healthcare
    "LLY":  "healthcare",
    "UNH":  "healthcare",
    "ISRG": "healthcare",
    # Energy
    "XOM":  "energy",
    "NEE":  "energy",
    # Consumer
    "AMZN": "consumer",
    "WMT":  "consumer",
    "TSLA": "consumer",
    # Industrials
    "CAT":  "industrials",
    "BA":   "industrials",
    # Communication Services
    "META":  "communication",
    "GOOGL": "communication",
    # Materials
    "FCX":  "materials",
}

# z-scores for common confidence levels; avoid scipy dependency for a single lookup
_Z_SCORES = {0.90: 1.282, 0.95: 1.645, 0.99: 2.326}


def _z(confidence: float) -> float:
    if confidence in _Z_SCORES:
        return _Z_SCORES[confidence]
    try:
        from scipy.stats import norm
        return float(norm.ppf(confidence))
    except ImportError:
        return 1.645  # default to 95th percentile


def check_exposure_limits(
    ticker: str,
    quantity: float,
    entry_price: float,
    strategy_mode: str = "intraday",
) -> tuple[bool, str]:
    """Returns (allowed, reason)."""
    # All open trades aggregated across both lanes for portfolio-level risk counts
    all_open = get_open_trades()

    # Concurrent trade limit is shared across both modes (same account equity)
    if len(all_open) >= cfg.MAX_CONCURRENT_TRADES:
        return False, f"Max concurrent trades ({cfg.MAX_CONCURRENT_TRADES}) reached"

    # Per-lane duplicate check: one open position per (ticker, strategy_mode)
    lane_open = get_open_trades(strategy_mode=strategy_mode)
    duplicate = any(t["ticker"] == ticker for t in lane_open)
    if duplicate:
        return False, f"Already have an open {strategy_mode} position in {ticker}"

    return True, "Exposure within limits"


def _fetch_vol_and_price(ticker: str, fallback_price: float) -> tuple[float, float]:
    """
    Returns (daily_vol, current_price) for a ticker using 60-day realized vol.
    Falls back to (2% vol, fallback_price) on any data failure so callers
    always get a conservative estimate rather than a hard error.
    """
    from data.cache.market_data_cache import get_candles
    from core.constants import Timeframe

    daily_vol = 0.02
    price = fallback_price
    try:
        df = get_candles(ticker, Timeframe.D1)
        if not df.empty and len(df) >= 10:
            returns = df["Close"].pct_change().dropna().tail(60)
            vol = float(returns.std())
            if vol > 0 and not np.isnan(vol):
                daily_vol = vol
            price = float(df["Close"].iloc[-1])
    except Exception as exc:
        logger.warning("VaR: could not fetch data for %s (%s) — using 2%% vol / stored price fallback", ticker, exc)
    return daily_vol, price


def current_var_estimate(portfolio_value: float, confidence: float = 0.95) -> float:
    """
    1-day parametric VaR using 60-day realized volatility from actual open positions.
    Portfolio VaR = sum of individual position VaRs (conservative; ignores diversification).
    Returns 0.0 when no positions are open.
    """
    open_trades = get_open_trades()
    if not open_trades:
        return 0.0

    z = _z(confidence)
    total_var = 0.0

    for trade in open_trades:
        ticker = trade.get("ticker", "")
        quantity = float(trade.get("quantity") or 0)
        if quantity <= 0:
            continue

        daily_vol, current_price = _fetch_vol_and_price(ticker, float(trade.get("entry_price") or 0))
        position_value = quantity * current_price
        total_var += position_value * daily_vol * z
        logger.debug("VaR component %s: qty=%s price=%.2f vol=%.3f → $%.0f",
                     ticker, quantity, current_price, daily_vol, position_value * daily_vol * z)

    logger.debug("Portfolio VaR (%.0f%% confidence): $%.0f", confidence * 100, total_var)
    return total_var


def _candidate_var(ticker: str, quantity: float, price: float, confidence: float) -> float:
    """1-day parametric VaR for a single not-yet-open trade."""
    daily_vol, current_price = _fetch_vol_and_price(ticker, price)
    return quantity * current_price * daily_vol * _z(confidence)


def check_var_limit(
    portfolio_value: float,
    confidence: float = 0.95,
    candidate_ticker: str = "",
    candidate_qty: float = 0.0,
    candidate_price: float = 0.0,
) -> tuple[bool, str]:
    """
    Returns (within_limit, reason).
    Adds the candidate trade's own VaR to the existing portfolio VaR so incremental
    risk is checked before approving, not just the already-open position VaR.
    """
    existing_var = current_var_estimate(portfolio_value, confidence)
    cand_var = 0.0
    if candidate_ticker and candidate_qty > 0 and candidate_price > 0:
        cand_var = _candidate_var(candidate_ticker, candidate_qty, candidate_price, confidence)

    total_var = existing_var + cand_var
    risk_budget = portfolio_value * cfg.MAX_VAR_BUDGET_PCT
    if total_var > risk_budget:
        if cand_var > 0:
            return (
                False,
                f"Incremental VaR ${total_var:.0f} (existing ${existing_var:.0f} + "
                f"candidate ${cand_var:.0f}) exceeds budget ${risk_budget:.0f}",
            )
        return False, f"Portfolio VaR ${existing_var:.0f} exceeds daily risk budget ${risk_budget:.0f}"
    return True, f"VaR ${total_var:.0f} within budget ${risk_budget:.0f}"
