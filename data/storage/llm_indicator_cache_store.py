import logging
from datetime import datetime, timezone
from data.storage.db import get_connection

logger = logging.getLogger(__name__)


def get_cached_verdict(ticker: str, timeframe: str, indicator: str,
                        current_value: float, delta_threshold: float) -> str | None:
    """
    Returns the cached 'buy'/'sell'/'hold' verdict for (ticker, timeframe,
    indicator) if current_value hasn't moved more than delta_threshold since
    the last LLM call, else None — caller should make a fresh call and
    save_verdict() the result.
    """
    with get_connection() as conn:
        row = conn.execute(
            """SELECT last_value, last_verdict FROM llm_indicator_cache
               WHERE ticker = ? AND timeframe = ? AND indicator = ?""",
            (ticker, timeframe, indicator),
        ).fetchone()
    if row is None:
        return None
    if abs(row["last_value"] - current_value) > delta_threshold:
        return None
    return row["last_verdict"]


def save_verdict(ticker: str, timeframe: str, indicator: str, value: float, verdict: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO llm_indicator_cache
               (ticker, timeframe, indicator, last_value, last_verdict, last_call_at)
               VALUES (?,?,?,?,?,?)""",
            (ticker, timeframe, indicator, value, verdict, now),
        )
