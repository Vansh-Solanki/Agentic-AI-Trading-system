import json
import logging
from datetime import datetime, timezone
from core.models import TradePlan
from data.storage.db import get_connection

logger = logging.getLogger(__name__)


def save_trade_plan(plan: TradePlan, signal_snapshot: str | None = None) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO trade_decisions
               (ticker, side, order_type, quantity, entry_price, stop_loss, take_profit,
                confidence, reasoning, status, created_at, metadata, strategy_mode, client_order_id,
                signal_snapshot)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                plan.ticker,
                plan.side.value,
                plan.order_type.value,
                plan.quantity,
                plan.entry_price,
                plan.stop_loss,
                plan.take_profit,
                plan.confidence,
                plan.reasoning,
                plan.status.value,
                plan.created_at.isoformat(),
                json.dumps(plan.metadata),
                plan.strategy_mode,
                plan.client_order_id,  # NULL at insert time; set via set_client_order_id()
                signal_snapshot,
            ),
        )
        return cur.lastrowid


def set_client_order_id(trade_id: int, client_order_id: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE trade_decisions SET client_order_id=? WHERE id=?",
            (client_order_id, trade_id),
        )


def update_trade_status(trade_id: int, status: str, filled_price: float | None = None,
                        filled_at: datetime | None = None, pnl: float | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            """UPDATE trade_decisions
               SET status=?, filled_price=?, filled_at=?, pnl=?
               WHERE id=?""",
            (status, filled_price, filled_at.isoformat() if filled_at else None, pnl, trade_id),
        )


def get_trade_by_id(trade_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM trade_decisions WHERE id=?", (trade_id,)).fetchone()
    return dict(row) if row else None


def update_order_result(trade_id: int, status: str, filled_price: float | None = None,
                         filled_at: datetime | None = None, filled_quantity: float | None = None) -> None:
    """Session 52 (Bug 6): the one place that updates status, filled_price,
    filled_at, and quantity together after a broker sync — update_trade_status()
    above never touched quantity, which is how a partial fill used to stay
    recorded at the full originally-requested size.

    filled_price/filled_at use COALESCE (only overwrite when a real value is
    given) so a call that only changes quantity — e.g. reducing an entry row
    after a partially-filled exit dies — doesn't blank out a price/time
    already recorded by an earlier call.

    On a partial fill (filled_quantity given and different from the row's
    current quantity), the original requested quantity is preserved under
    metadata["requested_quantity"] before quantity is overwritten, so it isn't
    lost once quantity reflects what actually filled.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT quantity, metadata FROM trade_decisions WHERE id=?", (trade_id,)
        ).fetchone()
        if row is None:
            return
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        new_quantity = row["quantity"]
        if filled_quantity is not None and filled_quantity != row["quantity"]:
            meta.setdefault("requested_quantity", row["quantity"])
            new_quantity = filled_quantity
        conn.execute(
            """UPDATE trade_decisions
               SET status=?, filled_price=COALESCE(?, filled_price),
                   filled_at=COALESCE(?, filled_at), quantity=?, metadata=?
               WHERE id=?""",
            (status, filled_price, filled_at.isoformat() if filled_at else None,
             new_quantity, json.dumps(meta), trade_id),
        )


def get_pending_orders() -> list[dict]:
    """Return every row still in-flight at the broker: status in (pending,
    approved, submitted), both entries AND exits, both lanes. Used only by
    execution/order_sync.py to finalize real order state.

    Deliberately different from get_open_trades() (Bug 5's fix), which
    excludes exit rows because it answers "what positions are open" — this
    answers "what orders still need their lifecycle resolved," and an
    in-flight exit order needs that just as much as an in-flight entry.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM trade_decisions WHERE status IN ('pending','approved','submitted')"
        ).fetchall()
    return [dict(r) for r in rows]


def get_open_trades(strategy_mode: str | None = None) -> list[dict]:
    """Return every row that represents an open position or an in-flight order.

    "Open" is two disjoint groups, both required so risk checks see real exposure:
      - in-flight entries: status in (pending, approved, submitted), including rows
        whose client_order_id hasn't been assigned yet (NULL) — but never an exit row.
      - open positions: status='filled' AND client_order_id LIKE 'entry-%'. A filled
        exit row ("exit-{id}-{mode}") would otherwise double-count the same position
        it just closed, so it's excluded by requiring the 'entry-%' prefix.
    `client_order_id IS NULL OR ... NOT LIKE` (not just NOT LIKE) because SQLite's
    `NULL NOT LIKE x` evaluates to NULL, which silently drops the NULL rows instead
    of matching them.
    """
    query = """
        SELECT * FROM trade_decisions
        WHERE (
            (status IN ('pending','approved','submitted')
               AND (client_order_id IS NULL OR client_order_id NOT LIKE 'exit-%'))
            OR (status = 'filled' AND client_order_id LIKE 'entry-%')
        )
    """
    params: tuple = ()
    if strategy_mode:
        query += " AND strategy_mode=?"
        params = (strategy_mode,)
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_filled_trades(strategy_mode: str | None = None) -> list[dict]:
    """Return open entry rows (long BUY or short SELL) with status='filled'.

    Exit orders are excluded by requiring client_order_id LIKE 'entry-%'.
    This supports both long and short open positions.
    """
    with get_connection() as conn:
        if strategy_mode:
            rows = conn.execute(
                "SELECT * FROM trade_decisions WHERE status='filled' AND client_order_id LIKE 'entry-%' AND strategy_mode=?",
                (strategy_mode,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trade_decisions WHERE status='filled' AND client_order_id LIKE 'entry-%'"
            ).fetchall()
    return [dict(r) for r in rows]


def get_closed_entries_since(start_iso: str) -> list[dict]:
    """Closed entry rows (status='closed', client_order_id LIKE 'entry-%',
    pnl recorded) closed on or after `start_iso` (a UTC ISO timestamp string,
    same format as closed_at itself), newest first by closed_at.

    Used by risk/circuit_breakers.py's consecutive-loss-streak check — the
    original bug filtered on status='filled' (closed trades are 'closed')
    and iterated a newest-first list with reversed(), both of which this
    function's contract (status='closed', explicitly newest-first) makes the
    caller get right by construction rather than by convention.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM trade_decisions
               WHERE status='closed' AND client_order_id LIKE 'entry-%'
                 AND pnl IS NOT NULL AND closed_at >= ?
               ORDER BY closed_at DESC""",
            (start_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_open_filled_entry_trade(ticker: str, strategy_mode: str) -> dict | None:
    """Return the most recent filled entry trade for (ticker, strategy_mode), or None.

    Returns both long (BUY) and short (SELL) entries. Exit orders are excluded by
    requiring client_order_id LIKE 'entry-%'.
    """
    with get_connection() as conn:
        row = conn.execute(
            """SELECT * FROM trade_decisions
               WHERE ticker=? AND strategy_mode=? AND status='filled' AND client_order_id LIKE 'entry-%'
               ORDER BY filled_at DESC LIMIT 1""",
            (ticker, strategy_mode),
        ).fetchone()
    return dict(row) if row else None


def close_trade(trade_id: int, pnl: float | None, closed_at: datetime) -> None:
    """Mark a filled trade as closed and record its realized PnL."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE trade_decisions SET status='closed', pnl=?, closed_at=? WHERE id=?",
            (pnl, closed_at.isoformat(), trade_id),
        )


def get_trade_history(ticker: str | None = None, limit: int = 100) -> list[dict]:
    if ticker:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM trade_decisions WHERE ticker=? ORDER BY created_at DESC LIMIT ?",
                (ticker, limit),
            ).fetchall()
    else:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM trade_decisions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_closed_trades_for_weights(mode: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT pnl, signal_snapshot FROM trade_decisions
               WHERE status='closed' AND client_order_id LIKE 'entry-%' AND strategy_mode=?
               AND pnl IS NOT NULL AND signal_snapshot IS NOT NULL""",
            (mode,),
        ).fetchall()
    return [dict(r) for r in rows]


def has_pending_exit(entry_trade_id: int, strategy_mode: str) -> bool:
    """Return True if a non-terminal exit order already exists for this entry trade.

    Checks client_order_id only — not side — so it catches both SELL (long close)
    and BUY (short close) exit orders. Prevents double-submission when the SL/TP
    loop fires faster than reconciliation can mark the entry as closed.
    """
    exit_coid = f"exit-{entry_trade_id}-{strategy_mode}"
    with get_connection() as conn:
        row = conn.execute(
            """SELECT id FROM trade_decisions
               WHERE client_order_id=?
               AND status NOT IN ('failed','cancelled','closed')
               LIMIT 1""",
            (exit_coid,),
        ).fetchone()
    return row is not None


def get_agent_weights(mode: str) -> dict[str, float]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT source, weight FROM agent_weights WHERE mode=?", (mode,)
        ).fetchall()
    return {r["source"]: r["weight"] for r in rows}


def save_critic_verdict(
    ticker: str,
    strategy_mode: str,
    approved: bool,
    confidence: float | None,
    reasoning: str | None,
    biggest_risk: str | None,
    failure_scenario: str | None,
    entry_price: float | None,
    stop_loss: float | None,
    take_profit: float | None,
    rr_ratio: float | None,
    trade_id: int | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO critic_log
               (ticker, strategy_mode, timestamp, approved, confidence, reasoning,
                biggest_risk, failure_scenario, entry_price, stop_loss, take_profit,
                rr_ratio, trade_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ticker, strategy_mode, now, int(approved), confidence, reasoning,
             biggest_risk, failure_scenario, entry_price, stop_loss, take_profit,
             rr_ratio, trade_id),
        )


def save_hold_decision(
    ticker: str,
    strategy_mode: str,
    signal_type: str,
    confidence: float | None,
    regime: str | None,
    reasoning: str | None,
    entry_price: float | None,
    current_price: float | None,
    unrealized_pnl: float | None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO hold_decisions
               (ticker, strategy_mode, signal_type, confidence, regime, reasoning,
                entry_price, current_price, unrealized_pnl, timestamp)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (ticker, strategy_mode, signal_type, confidence, regime, reasoning,
             entry_price, current_price, unrealized_pnl, now),
        )


def save_agent_weights(mode: str, weights: dict[str, float]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        for source, weight in weights.items():
            conn.execute(
                "INSERT OR REPLACE INTO agent_weights (mode, source, weight, updated_at) VALUES (?,?,?,?)",
                (mode, source, weight, now),
            )
