"""
Tests for Bug 5: get_open_trades() excluded filled positions, so VaR and
exposure checks never saw real open positions (only pending/approved/submitted
in-flight orders).

Uses a temporary SQLite file (never trading_agent.db) by monkeypatching
data.storage.db.DB_PATH before calling init_schema(). Rows are inserted
directly via sqlite3 so each test controls status/client_order_id precisely,
independent of save_trade_plan()/ExecutionAgent.

Run: pytest tests/test_open_trades.py -v
"""
from datetime import datetime, timezone

import pytest

import core.config as cfg
import data.storage.db as db
from data.storage.decision_store import get_open_trades
from risk.portfolio_exposure import current_var_estimate, check_exposure_limits


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_trading_agent.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_schema()
    return db_path


def _insert(conn, ticker, status, client_order_id, strategy_mode="intraday",
            entry_price=100.0, quantity=10, side="buy"):
    conn.execute(
        """INSERT INTO trade_decisions
           (ticker, side, order_type, quantity, entry_price, stop_loss, take_profit,
            confidence, reasoning, status, created_at, metadata, strategy_mode, client_order_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ticker, side, "limit", quantity, entry_price, entry_price * 0.99, entry_price * 1.02,
         0.6, "test row", status, datetime.now(timezone.utc).isoformat(), "{}",
         strategy_mode, client_order_id),
    )


def _seed_sample_rows(temp_db):
    """The 8 sample rows from the task spec, as a (ticker, status, client_order_id,
    strategy_mode, expect_counted_all, expect_counted_intraday) table."""
    rows = [
        ("A1", "pending",   None,                 "intraday",   True,  True),
        ("A2", "submitted", "entry-2-intraday",    "intraday",   True,  True),
        ("A3", "filled",    "entry-3-intraday",    "intraday",   True,  True),
        ("A3", "filled",    "exit-3-intraday",     "intraday",   False, False),
        ("A5", "closed",    "entry-5-intraday",    "intraday",   False, False),
        ("A6", "failed",    "entry-6-intraday",    "intraday",   False, False),
        ("A9", "submitted", "exit-9-intraday",     "intraday",   False, False),
        ("A7", "filled",    "entry-7-long_term",   "long_term",  True,  False),
    ]
    with db.get_connection() as conn:
        for ticker, status, coid, mode, *_ in rows:
            _insert(conn, ticker, status, coid, strategy_mode=mode)
    return rows


class TestGetOpenTrades:

    def test_sample_rows_all_lanes(self, temp_db):
        rows = _seed_sample_rows(temp_db)
        open_trades = get_open_trades()
        open_coids = {(t["ticker"], t["status"], t["client_order_id"]) for t in open_trades}

        for ticker, status, coid, mode, expect_all, _ in rows:
            key = (ticker, status, coid)
            if expect_all:
                assert key in open_coids, f"{key} should be counted (no lane filter)"
            else:
                assert key not in open_coids, f"{key} should NOT be counted"

        # Exactly 4 rows expected: A1, A2, A3(entry), A7(entry, long_term)
        assert len(open_trades) == 4

    def test_sample_rows_filtered_by_intraday_lane(self, temp_db):
        rows = _seed_sample_rows(temp_db)
        open_trades = get_open_trades(strategy_mode="intraday")
        open_coids = {(t["ticker"], t["status"], t["client_order_id"]) for t in open_trades}

        for ticker, status, coid, mode, _, expect_intraday in rows:
            key = (ticker, status, coid)
            if expect_intraday:
                assert key in open_coids, f"{key} should be counted under intraday filter"
            else:
                assert key not in open_coids, f"{key} should NOT be counted under intraday filter"

        # A7 is long_term — must be excluded when filtering by intraday
        assert not any(t["ticker"] == "A7" for t in open_trades)
        assert len(open_trades) == 3

    def test_null_client_order_id_is_counted_not_dropped(self, temp_db):
        """Guards against the NULL NOT LIKE x -> NULL trap silently excluding
        in-flight rows that haven't been assigned a client_order_id yet."""
        with db.get_connection() as conn:
            _insert(conn, "NULLCOID", "pending", None, strategy_mode="intraday")
        open_trades = get_open_trades()
        assert any(t["ticker"] == "NULLCOID" for t in open_trades)

    def test_filled_exit_row_does_not_double_count_position(self, temp_db):
        """A filled entry + its filled exit must net to ONE counted row, not two."""
        with db.get_connection() as conn:
            _insert(conn, "DBL", "filled", "entry-50-intraday", strategy_mode="intraday")
            _insert(conn, "DBL", "filled", "exit-50-intraday", strategy_mode="intraday")
        open_trades = get_open_trades()
        dbl_rows = [t for t in open_trades if t["ticker"] == "DBL"]
        assert len(dbl_rows) == 1
        assert dbl_rows[0]["client_order_id"] == "entry-50-intraday"


class TestVarAndExposureSeeFilledPositions:

    def test_current_var_estimate_positive_with_filled_entry(self, temp_db, monkeypatch):
        with db.get_connection() as conn:
            _insert(conn, "AAPL", "filled", "entry-100-intraday",
                    strategy_mode="intraday", entry_price=200.0, quantity=50)

        # Force the candle-fetch fallback path in _fetch_vol_and_price:
        # (daily_vol=0.02, price=fallback_price) so the VaR math is deterministic
        # without needing a real candle cache.
        def _boom(*args, **kwargs):
            raise RuntimeError("no candle cache in test")
        monkeypatch.setattr("data.cache.market_data_cache.get_candles", _boom)

        var = current_var_estimate(portfolio_value=100_000.0)
        assert var > 0

    def test_check_exposure_limits_rejects_at_concurrent_trade_cap(self, temp_db, monkeypatch):
        monkeypatch.setattr(cfg, "MAX_CONCURRENT_TRADES", 2)
        with db.get_connection() as conn:
            _insert(conn, "T1", "filled", "entry-1-intraday", strategy_mode="intraday")
            _insert(conn, "T2", "pending", None, strategy_mode="intraday")

        allowed, reason = check_exposure_limits("T3", 10, 100.0, "intraday")
        assert allowed is False
        assert "Max concurrent trades" in reason

    def test_check_exposure_limits_rejects_duplicate_filled_entry_same_lane(self, temp_db):
        with db.get_connection() as conn:
            _insert(conn, "AAPL", "filled", "entry-200-intraday",
                    strategy_mode="intraday", entry_price=200.0, quantity=10)

        allowed, reason = check_exposure_limits("AAPL", 5, 200.0, "intraday")
        assert allowed is False
        assert "AAPL" in reason

    def test_check_exposure_limits_allows_same_ticker_other_lane(self, temp_db):
        with db.get_connection() as conn:
            _insert(conn, "AAPL", "filled", "entry-201-intraday",
                    strategy_mode="intraday", entry_price=200.0, quantity=10)

        allowed, reason = check_exposure_limits("AAPL", 5, 200.0, "long_term")
        assert allowed is True
