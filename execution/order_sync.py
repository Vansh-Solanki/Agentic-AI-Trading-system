"""
Session 52 (Bug 6), Part C: order lifecycle sync.

agents/execution.py now records SUBMITTED (not a fabricated FILLED) for any
order the broker hasn't actually filled yet. Something has to go check back
on those orders later and finalize their real outcome — that's this module.

Runs against every row still in (pending, approved, submitted) across BOTH
lanes and BOTH entry/exit rows (get_pending_orders() — deliberately not
get_open_trades(), which excludes exit rows on purpose for Bug 5's "what
positions are open" question; this module needs exit rows too). Idempotent:
re-running it against an already-resolved row is a no-op, because that row
is no longer in get_pending_orders()'s result once its status leaves
pending/approved/submitted.
"""
import logging
import threading
from datetime import datetime, timezone

from core.config import ENTRY_ORDER_TIMEOUT_MINUTES, STALE_ORDER_MINUTES, MAX_ORDER_AGE_HOURS
from core.constants import TradeStatus
from data.storage.decision_store import (
    get_pending_orders,
    get_trade_by_id,
    update_trade_status,
    update_order_result,
)
from execution.broker.alpaca import AlpacaBroker

logger = logging.getLogger(__name__)

# Non-blocking: if two callers (e.g. the maintenance thread and run_all() on
# the main thread) both reach sync_broker_orders() around the same time, the
# second one just skips rather than double-processing the same rows. Safe
# because the next pass (seconds away) will pick up anything the first call
# didn't finish.
_sync_lock = threading.Lock()

_TERMINAL_NO_FILL = ("canceled", "expired")

# Statuses expected while an order is genuinely still working. Anything else
# reaching the "still open" branch is logged once per row (not every pass —
# run_maintenance() calls this every INTRADAY_CYCLE_SECS, so an un-deduped
# warning would flood the logs for however long the order sits in that state).
_KNOWN_STILL_OPEN_STATUSES = {
    "new", "accepted", "partially_filled", "pending_new",
    "accepted_for_bidding", "pending_cancel",
}

# In-memory dedup for the "unrecognized status" warning below — per trade_id,
# not persisted, so it resets on process restart. That's fine: it only exists
# to stop the same warning re-firing every ~60s for the life of one order,
# not to build a permanent audit trail (the log line itself still happens at
# least once, and the real state is always in the DB row, not this set).
_warned_unrecognized_status: set[int] = set()


def _age_minutes(created_at_iso: str) -> float:
    created = datetime.fromisoformat(created_at_iso)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() / 60.0


def sync_broker_orders() -> None:
    """Reconcile every in-flight trade_decisions row against its real broker
    order state. Safe to call every maintenance/run_all cycle."""
    if not _sync_lock.acquire(blocking=False):
        logger.debug("Order sync already in progress on another thread — skipping this call.")
        return
    try:
        _run_sync()
    finally:
        _sync_lock.release()


def _run_sync() -> None:
    try:
        broker = AlpacaBroker()
    except Exception as exc:
        logger.warning("Order sync: could not initialize broker this pass — will retry next cycle: %s", exc)
        return

    for row in get_pending_orders():
        # One bad row must not stop the rest of the batch.
        try:
            _sync_one(broker, row)
        except Exception:
            logger.exception(
                "Order sync: unexpected error on trade_id=%s — leaving unchanged, will retry next cycle.",
                row.get("id"),
            )


def _sync_one(broker: AlpacaBroker, row: dict) -> None:
    trade_id = row["id"]
    coid = row.get("client_order_id")
    is_exit = bool(coid) and coid.startswith("exit-")

    if not coid:
        # ExecutionAgent assigns client_order_id synchronously right after
        # the insert — a NULL here is normally just this process's own
        # write/read race window. But if it's still NULL after
        # STALE_ORDER_MINUTES, the process almost certainly died between
        # save_trade_plan() and set_client_order_id()/route_order() — this
        # row never reached the broker and never will on its own. Mark it
        # FAILED instead of leaving it to sit in get_pending_orders() forever
        # (previously: unconditional `return`, no age check at all).
        if _age_minutes(row["created_at"]) > STALE_ORDER_MINUTES:
            update_trade_status(trade_id, TradeStatus.FAILED.value)
            logger.warning(
                "[trade_id=%s] Order sync: no client_order_id assigned after %d+ minutes — "
                "orphaned before reaching the broker, marked FAILED.", trade_id, STALE_ORDER_MINUTES,
            )
        return

    try:
        state = broker.get_order_state(coid)
    except Exception as exc:
        # Transient broker/network error — never fail a row for this, retry
        # next pass. Only a confirmed 404 (state is None) means "not found."
        logger.warning("[trade_id=%s] Order sync: broker error checking %s — leaving unchanged: %s",
                        trade_id, coid, exc)
        return

    if state is None:
        if _age_minutes(row["created_at"]) > STALE_ORDER_MINUTES:
            update_trade_status(trade_id, TradeStatus.FAILED.value)
            logger.warning(
                "[trade_id=%s] Order sync: no broker order found for %s after %d+ minutes — marked FAILED.",
                trade_id, coid, STALE_ORDER_MINUTES,
            )
        # else: younger than STALE_ORDER_MINUTES — likely just hasn't
        # propagated to the broker's lookup index yet. Leave it.
        return

    status = state["status"]
    filled_qty = state["filled_qty"] or 0.0
    requested_qty = row["quantity"]

    if status == "filled":
        update_order_result(
            trade_id, TradeStatus.FILLED.value,
            filled_price=state["filled_avg_price"], filled_at=state["filled_at"],
            filled_quantity=filled_qty,
        )
        logger.info("[trade_id=%s] Order sync: %s FILLED @ %s x%s.",
                    trade_id, coid, state["filled_avg_price"], filled_qty)
        return

    if status in _TERMINAL_NO_FILL:
        if filled_qty > 0:
            update_order_result(
                trade_id, TradeStatus.FILLED.value,
                filled_price=state["filled_avg_price"], filled_at=state["filled_at"],
                filled_quantity=filled_qty,
            )
            logger.info("[trade_id=%s] Order sync: %s %s with partial fill x%s — marked FILLED.",
                        trade_id, coid, status, filled_qty)
            if is_exit and filled_qty < requested_qty:
                _reduce_entry_after_partial_exit(coid, filled_qty)
        else:
            update_trade_status(trade_id, TradeStatus.CANCELLED.value)
            logger.info("[trade_id=%s] Order sync: %s %s with no fill — marked CANCELLED.",
                        trade_id, coid, status)
        return

    if status == "rejected":
        update_trade_status(trade_id, TradeStatus.FAILED.value)
        logger.warning("[trade_id=%s] Order sync: %s REJECTED by broker — marked FAILED.", trade_id, coid)
        return

    # Still open at the broker. age_minutes feeds two independent checks below:
    # the normal entry timeout (minutes) and the hard age cap (hours) — the
    # cap is a backstop that fires the same entry action if the timeout were
    # ever misconfigured away, and is the only age check that applies to exits
    # at all (as a non-cancelling warning).
    age_minutes = _age_minutes(row["created_at"])
    hard_cap_minutes = MAX_ORDER_AGE_HOURS * 60

    if is_exit:
        # Exits are never auto-cancelled at any age — cancelling a working
        # exit would leave the position unprotected; that's SL/TP retry
        # logic's job, not this sync's. Past the hard cap, just warn loudly,
        # every pass (unlike the unrecognized-status warning below, this one
        # is deliberately not deduped — an unprotected position deserves a
        # recurring signal, not a single log line that scrolls away).
        if age_minutes > hard_cap_minutes:
            logger.warning(
                "[trade_id=%s] Order sync: exit %s still open after %.1f hours (status=%s) — "
                "position may be unprotected. Not auto-cancelling.", trade_id, coid, age_minutes / 60, status,
            )
        elif status not in _KNOWN_STILL_OPEN_STATUSES:
            _warn_unrecognized_status_once(trade_id, coid, status)
        return

    # Entry: cancel once it's past whichever threshold is smaller/fires
    # first — in practice ENTRY_ORDER_TIMEOUT_MINUTES (minutes) always does.
    if age_minutes > ENTRY_ORDER_TIMEOUT_MINUTES or age_minutes > hard_cap_minutes:
        if status == "pending_cancel":
            # Already asked the broker to cancel this on an earlier pass —
            # resending cancel_order() every ~60s here was the original bug
            # (context item 3): it does nothing useful and just adds noise/
            # API calls while waiting for the broker to actually finish
            # cancelling.
            logger.debug(
                "[trade_id=%s] Order sync: %s already pending_cancel — not re-requesting.", trade_id, coid
            )
        else:
            try:
                broker.cancel_order(coid)
                logger.info(
                    "[trade_id=%s] Order sync: entry %s still open past %d minutes — cancel requested, "
                    "will finalize next pass.", trade_id, coid, ENTRY_ORDER_TIMEOUT_MINUTES,
                )
            except Exception:
                logger.exception(
                    "[trade_id=%s] Order sync: cancel request failed for %s — will retry next pass.",
                    trade_id, coid,
                )
        return

    if status not in _KNOWN_STILL_OPEN_STATUSES:
        _warn_unrecognized_status_once(trade_id, coid, status)


def _warn_unrecognized_status_once(trade_id: int, coid: str, status: str) -> None:
    if trade_id in _warned_unrecognized_status:
        return
    _warned_unrecognized_status.add(trade_id)
    logger.warning(
        "[trade_id=%s] Order sync: %s has broker status '%s', which this sync does not explicitly "
        "handle — leaving it in the 'still open' bucket. Logged once for this row.",
        trade_id, coid, status,
    )


def _reduce_entry_after_partial_exit(exit_coid: str, filled_qty: float) -> None:
    """An exit order that partially filled and then died (canceled/expired)
    closed part of the position. The exit row itself is already correctly
    marked FILLED with the partial quantity by the caller — this reduces the
    corresponding entry row's remaining open quantity by that same amount, so
    get_open_trades()/get_filled_trades() keep reporting accurate exposure
    for what's actually still open."""
    entry_id = _entry_id_from_exit_coid(exit_coid)
    if entry_id is None:
        logger.warning("Order sync: could not parse entry id out of exit client_order_id %s — "
                        "cannot reduce entry quantity.", exit_coid)
        return
    entry = get_trade_by_id(entry_id)
    if entry is None:
        logger.warning("Order sync: entry trade_id=%s (from exit %s) not found — "
                        "cannot reduce entry quantity.", entry_id, exit_coid)
        return
    new_qty = max(entry["quantity"] - filled_qty, 0.0)
    update_order_result(entry_id, TradeStatus.FILLED.value, filled_quantity=new_qty)
    logger.warning(
        "[entry trade_id=%s] Position quantity reduced %s -> %s after exit %s partially filled "
        "(%s) and then died — position is only partially closed.",
        entry_id, entry["quantity"], new_qty, exit_coid, filled_qty,
    )


def _entry_id_from_exit_coid(coid: str | None) -> int | None:
    if not coid or not coid.startswith("exit-"):
        return None
    try:
        return int(coid.split("-")[1])
    except (IndexError, ValueError):
        return None
