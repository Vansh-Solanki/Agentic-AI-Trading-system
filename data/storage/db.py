import sqlite3
import logging
from pathlib import Path
from core.config import DB_PATH

logger = logging.getLogger(__name__)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema() -> None:
    """Create all tables if they don't exist."""
    with get_connection() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT NOT NULL,
            source      TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            timeframe   TEXT NOT NULL,
            confidence  REAL NOT NULL,
            value       REAL,
            reasoning   TEXT,
            metadata    TEXT,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trade_decisions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker       TEXT NOT NULL,
            side         TEXT NOT NULL,
            order_type   TEXT NOT NULL,
            quantity     REAL NOT NULL,
            entry_price  REAL,
            stop_loss    REAL NOT NULL,
            take_profit  REAL NOT NULL,
            confidence   REAL NOT NULL,
            reasoning    TEXT,
            status       TEXT NOT NULL DEFAULT 'pending',
            created_at   TEXT NOT NULL,
            filled_at    TEXT,
            filled_price REAL,
            pnl          REAL,
            closed_at    TEXT,
            metadata     TEXT
        );

        CREATE TABLE IF NOT EXISTS news (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            headline      TEXT NOT NULL,
            source        TEXT,
            url           TEXT,
            published_at  TEXT NOT NULL,
            tickers       TEXT NOT NULL,
            sentiment     REAL,
            layer         INTEGER NOT NULL DEFAULT 2,
            summary       TEXT,
            metadata      TEXT,
            stored_at     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS stock_memory (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker     TEXT NOT NULL,
            mode       TEXT NOT NULL,
            key        TEXT NOT NULL,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(ticker, mode, key)
        );

        CREATE TABLE IF NOT EXISTS agent_weights (
            mode       TEXT NOT NULL,
            source     TEXT NOT NULL,
            weight     REAL NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (mode, source)
        );

        CREATE TABLE IF NOT EXISTS critic_log (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker           TEXT NOT NULL,
            strategy_mode    TEXT NOT NULL,
            timestamp        TEXT NOT NULL,
            approved         INTEGER NOT NULL,
            confidence       REAL,
            reasoning        TEXT,
            biggest_risk     TEXT,
            failure_scenario TEXT,
            entry_price      REAL,
            stop_loss        REAL,
            take_profit      REAL,
            rr_ratio         REAL,
            trade_id         INTEGER
        );

        CREATE TABLE IF NOT EXISTS hold_decisions (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker         TEXT NOT NULL,
            strategy_mode  TEXT NOT NULL,
            signal_type    TEXT NOT NULL,
            confidence     REAL,
            regime         TEXT,
            reasoning      TEXT,
            entry_price    REAL,
            current_price  REAL,
            unrealized_pnl REAL,
            timestamp      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS news_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT NOT NULL,
            headline        TEXT NOT NULL,
            source          TEXT,
            published_at    TEXT NOT NULL,
            sentiment_score REAL,
            sentiment_label TEXT,
            horizon         TEXT,
            raw_summary     TEXT,
            stored_at       TEXT NOT NULL,
            UNIQUE(ticker, headline, published_at)
        );

        CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);
        CREATE INDEX IF NOT EXISTS idx_decisions_ticker ON trade_decisions(ticker);
        CREATE INDEX IF NOT EXISTS idx_news_published ON news(published_at);
        CREATE INDEX IF NOT EXISTS idx_critic_log_ticker ON critic_log(ticker);
        CREATE INDEX IF NOT EXISTS idx_hold_decisions_ticker ON hold_decisions(ticker);
        CREATE INDEX IF NOT EXISTS idx_news_events_ticker_published ON news_events(ticker, published_at);
        """)

        # Idempotent migrations — each wrapped individually so one failure doesn't block others
        for migration in (
            "ALTER TABLE trade_decisions ADD COLUMN strategy_mode TEXT NOT NULL DEFAULT 'intraday'",
            "ALTER TABLE trade_decisions ADD COLUMN client_order_id TEXT",
            "ALTER TABLE trade_decisions ADD COLUMN signal_snapshot TEXT",
            "ALTER TABLE trade_decisions ADD COLUMN closed_at TEXT",
        ):
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # column already exists

    logger.info("Database schema initialized at %s", DB_PATH)
