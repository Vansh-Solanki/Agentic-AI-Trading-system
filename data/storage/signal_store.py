import json
import logging
from core.models import Signal
from data.storage.db import get_connection

logger = logging.getLogger(__name__)


def save_signal(signal: Signal) -> None:
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO signals
               (ticker, source, signal_type, timeframe, confidence, value, reasoning, metadata, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                signal.ticker,
                signal.source.value,
                signal.signal_type.value,
                signal.timeframe.value,
                signal.confidence,
                signal.value,
                signal.reasoning,
                json.dumps(signal.metadata),
                signal.timestamp.isoformat(),
            ),
        )


def save_signals(signals: list[Signal]) -> None:
    for s in signals:
        save_signal(s)


def get_signals(ticker: str, limit: int = 100) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE ticker=? ORDER BY created_at DESC LIMIT ?",
            (ticker, limit),
        ).fetchall()
    return [dict(r) for r in rows]
