import logging
from datetime import date
from data.storage.decision_store import get_trade_history

logger = logging.getLogger(__name__)

MAX_DAILY_LOSS_PCT     = 0.03  # 3% of portfolio
MAX_CONSECUTIVE_LOSSES = 4
_FALLBACK_PORTFOLIO_VALUE = 100_000.0


def _get_portfolio_value() -> float:
    """Fetch live paper portfolio value from Alpaca; fall back to $100k if unavailable."""
    try:
        from execution.broker.alpaca import AlpacaBroker
        return AlpacaBroker().get_account()["portfolio_value"]
    except Exception as exc:
        logger.warning("Could not fetch Alpaca portfolio value (%s) — using fallback $%.0f", exc, _FALLBACK_PORTFOLIO_VALUE)
        return _FALLBACK_PORTFOLIO_VALUE


def check_circuit_breakers() -> tuple[bool, str]:
    """
    Returns (triggered, reason).
    Checks daily loss limit and consecutive loss streaks.
    Portfolio value is fetched live from Alpaca on every call.
    """
    portfolio_value = _get_portfolio_value()
    today = date.today().isoformat()
    trades = get_trade_history(limit=20)

    # Daily P&L check
    today_pnl = sum(
        t["pnl"] or 0.0
        for t in trades
        if t.get("filled_at", "")[:10] == today and t["pnl"] is not None
    )
    if today_pnl < -(portfolio_value * MAX_DAILY_LOSS_PCT):
        reason = f"Daily loss limit hit: {today_pnl:.2f} ({today_pnl/portfolio_value:.1%})"
        logger.warning("CIRCUIT BREAKER: %s", reason)
        return True, reason

    # Consecutive loss streak
    closed = [t for t in trades if t["status"] == "filled" and t["pnl"] is not None]
    streak = 0
    for t in reversed(closed):
        if (t["pnl"] or 0) < 0:
            streak += 1
        else:
            break
    if streak >= MAX_CONSECUTIVE_LOSSES:
        reason = f"Consecutive loss streak: {streak} trades"
        logger.warning("CIRCUIT BREAKER: %s", reason)
        return True, reason

    return False, ""
