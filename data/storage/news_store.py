import json
import logging
from datetime import datetime, timedelta, timezone
from core.models import NewsItem
from data.storage.db import get_connection

logger = logging.getLogger(__name__)


def save_news(items: list[NewsItem]) -> None:
    if not items:
        return
    rows = [
        (
            item.headline,
            item.source,
            item.url,
            item.published_at.isoformat(),
            json.dumps(item.tickers),
            item.sentiment_score,
            item.layer,
            item.summary,
            json.dumps(item.metadata),
            datetime.now(timezone.utc).isoformat(),
        )
        for item in items
    ]
    with get_connection() as conn:
        conn.executemany(
            """INSERT OR IGNORE INTO news
               (headline, source, url, published_at, tickers, sentiment, layer, summary, metadata, stored_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
    logger.debug("Saved %d news items", len(items))


def get_recent_news(ticker: str, layer: int | None = None, limit: int = 50) -> list[dict]:
    query = "SELECT * FROM news WHERE tickers LIKE ? "
    params: list = [f'%"{ticker}"%']
    if layer is not None:
        query += "AND layer = ? "
        params.append(layer)
    query += "ORDER BY published_at DESC LIMIT ?"
    params.append(limit)
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# ── news_events (Session 34: cross-session persistence + market-hours SQLite reads) ────


def save_news_event(
    item: NewsItem,
    sentiment_score: float | None = None,
    sentiment_label: str | None = None,
    horizon: str | None = None,
) -> None:
    """
    Persist a FinBERT-scored / Ollama-classified news item to news_events.
    Idempotent: (ticker, headline, published_at) is UNIQUE — re-fetching the
    same article across cycles is a silent no-op, not a duplicate row.
    Writes one row per ticker the article is tagged with.
    """
    now = datetime.now(timezone.utc).isoformat()
    score = sentiment_score if sentiment_score is not None else item.sentiment_score
    with get_connection() as conn:
        for ticker in item.tickers:
            conn.execute(
                """INSERT OR IGNORE INTO news_events
                   (ticker, headline, source, published_at, sentiment_score,
                    sentiment_label, horizon, raw_summary, stored_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    ticker,
                    item.headline,
                    item.source,
                    item.published_at.isoformat(),
                    score,
                    sentiment_label,
                    horizon,
                    item.summary,
                    now,
                ),
            )


def get_recent_news_events(ticker: str, hours_back: int = 24) -> list[dict]:
    """Returns news_events rows for a ticker published within the lookback window, newest first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM news_events
               WHERE ticker = ? AND published_at >= ?
               ORDER BY published_at DESC""",
            (ticker, cutoff),
        ).fetchall()
    return [dict(r) for r in rows]


# ── premarket_checkpoints (Session 36: crash-resilient pre-market batch) ───────


def is_premarket_checkpoint_complete(ticker: str, batch_date: str) -> bool:
    """True if the pre-market batch already finished this ticker for batch_date (YYYY-MM-DD, ET)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM premarket_checkpoints WHERE ticker = ? AND batch_date = ?",
            (ticker, batch_date),
        ).fetchone()
    return row is not None


def save_premarket_checkpoint(ticker: str, batch_date: str) -> None:
    """Record that the pre-market batch finished this ticker for batch_date — lets a restart skip it."""
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO premarket_checkpoints (ticker, batch_date, completed_at)
               VALUES (?,?,?)""",
            (ticker, batch_date, now),
        )
