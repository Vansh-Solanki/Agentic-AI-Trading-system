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


def get_open_trades(strategy_mode: str | None = None) -> list[dict]:
    with get_connection() as conn:
        if strategy_mode:
            rows = conn.execute(
                "SELECT * FROM trade_decisions WHERE status IN ('pending','approved','submitted') AND strategy_mode=?",
                (strategy_mode,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trade_decisions WHERE status IN ('pending','approved','submitted')"
            ).fetchall()
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
