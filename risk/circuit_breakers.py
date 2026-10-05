import logging
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from core.config import MAX_DAILY_LOSS_PCT, MAX_CONSECUTIVE_LOSSES, CIRCUIT_BREAKER_CACHE_SECONDS
from data.storage.decision_store import get_closed_entries_since

logger = logging.getLogger(__name__)

_NY_TZ = ZoneInfo("America/New_York")

# Lazy-initialized singleton, same pattern as execution/order_router.py's
# _broker — check_circuit_breakers() runs once per ticker's risk validation
# AND once per maintenance-loop pass, so constructing a fresh AlpacaBroker()
# on every call was re-initializing a broker connection many times per cycle.
_broker = None


def _get_broker():
    global _broker
    if _broker is None:
        from execution.broker.alpaca import AlpacaBroker
        _broker = AlpacaBroker()
    return _broker


# ── Cache ─────────────────────────────────────────────────────────────────────
# check_circuit_breakers() is called once per ticker's risk validation (up to
# 20x/cycle) plus once per maintenance pass — caching means at most one real
# broker account call + one DB query per CIRCUIT_BREAKER_CACHE_SECONDS window,
# not one per call. Logging only fires on a state transition (tripped <->
# clear), not on every cache refresh, so a sustained trip doesn't spam the log
# every 30 seconds for however long it lasts.
_cache_lock = threading.Lock()
_cached_result: tuple[bool, str] | None = None
_cache_timestamp: float = 0.0
_last_logged_state: bool | None = None


def _trading_day_start_utc_iso() -> str:
    """Midnight America/New_York, today, expressed as a UTC ISO string —
    comparable against closed_at (also UTC ISO) for 'closed today' in NY
    trading-day terms rather than the machine's local date or UTC date."""
    now_ny = datetime.now(_NY_TZ)
    midnight_ny = now_ny.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_ny.astimezone(timezone.utc).isoformat()


def _check_consecutive_losses() -> tuple[bool, str]:
    """
    Trip when the most recent MAX_CONSECUTIVE_LOSSES closed entries today
    (America/New_York trading date) are all losses. Fewer than that many
    trades today never trips. A win anywhere in the most recent
    MAX_CONSECUTIVE_LOSSES resets the count to 0 at that point (the loop
    below stops counting at the first non-loss it sees, scanning
    newest-first).

    Only counts trades closed since today's NY midnight, so the streak
    resets automatically at the next trading day — no explicit reset logic
    needed.
    """
    trades = get_closed_entries_since(_trading_day_start_utc_iso())  # newest-first
    streak = 0
    for t in trades:
        if (t["pnl"] or 0) < 0:
            streak += 1
        else:
            break
    if streak >= MAX_CONSECUTIVE_LOSSES:
        return True, f"Consecutive loss streak: {streak} trades closed today"
    return False, ""


def _check_daily_loss() -> tuple[bool, str]:
    """
    Trip when (equity - last_equity) / last_equity <= -MAX_DAILY_LOSS_PCT.
    `equity` includes unrealized P/L on open positions; `last_equity` is
    Alpaca's prior-close equity — this is deliberately not limited to
    realized/closed-trade pnl, unlike the old get_trade_history()-based
    check, which missed unrealized losses entirely and also miscounted
    realized ones (see module docstring history in progress.md).

    Fails CLOSED (returns triggered=True) if the broker call fails or
    equity/last_equity come back missing or zero — "cannot verify daily P/L"
    is treated the same as "daily P/L limit breached" for the purpose of
    blocking new entries, since silently treating "I don't know" as "we're
    fine" is the wrong direction for a risk gate to fail in. This result is
    cached the same as any other, so it's re-checked (not stuck) once the
    cache window expires.
    """
    try:
        account = _get_broker().get_account()
    except Exception as exc:
        logger.warning("CIRCUIT BREAKER: cannot verify daily P/L (broker error: %s) — failing closed.", exc)
        return True, "cannot verify daily P/L"

    equity = account.get("equity")
    last_equity = account.get("last_equity")
    if not equity or not last_equity:
        logger.warning(
            "CIRCUIT BREAKER: cannot verify daily P/L (equity=%s, last_equity=%s from broker) — failing closed.",
            equity, last_equity,
        )
        return True, "cannot verify daily P/L"

    daily_pl_pct = (equity - last_equity) / last_equity
    if daily_pl_pct <= -MAX_DAILY_LOSS_PCT:
        return True, (
            f"Daily P/L {daily_pl_pct:.2%} breached -{MAX_DAILY_LOSS_PCT:.0%} limit "
            f"(equity=${equity:.2f}, last_equity=${last_equity:.2f})"
        )
    return False, ""


def _evaluate() -> tuple[bool, str]:
    streak_triggered, streak_reason = _check_consecutive_losses()
    if streak_triggered:
        return True, streak_reason

    # Daily loss needs a live, meaningful equity reading — only evaluated
    # while the market is open; otherwise report not tripped for this check
    # (the streak check above still applies regardless of market hours).
    # Local import: agents.orchestrator imports this module at module level,
    # so a top-level import here would be circular.
    from agents.orchestrator import _is_market_open
    if not _is_market_open():
        return False, ""

    return _check_daily_loss()


def check_circuit_breakers() -> tuple[bool, str]:
    """
    Returns (triggered, reason). Cached for CIRCUIT_BREAKER_CACHE_SECONDS;
    thread-safe (one real evaluation runs at a time, every caller within the
    cache window reads the same cached result).
    """
    global _cached_result, _cache_timestamp, _last_logged_state

    with _cache_lock:
        now = time.monotonic()
        if _cached_result is not None and now - _cache_timestamp < CIRCUIT_BREAKER_CACHE_SECONDS:
            return _cached_result

    result = _evaluate()

    with _cache_lock:
        _cached_result = result
        _cache_timestamp = time.monotonic()
        triggered, reason = result
        if triggered != _last_logged_state:
            if triggered:
                logger.warning("CIRCUIT BREAKER TRIPPED: %s", reason)
            else:
                logger.info("Circuit breaker cleared.")
            _last_logged_state = triggered

    return result
