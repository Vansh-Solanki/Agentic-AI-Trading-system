from datetime import datetime, timezone
from data.storage.db import get_connection


def upsert_memory(ticker: str, mode: str, key: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO stock_memory (ticker, mode, key, value, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(ticker, mode, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (ticker, mode, key, value, datetime.now(timezone.utc).isoformat()),
        )


def get_memory(ticker: str, mode: str, key: str) -> str | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM stock_memory WHERE ticker=? AND mode=? AND key=?",
            (ticker, mode, key),
        ).fetchone()
    return row["value"] if row else None
