"""
Circuit breaker repair. Covers:
  - daily loss computed from real broker equity/last_equity (realized +
    unrealized), not just closed-trade pnl
  - consecutive-loss streak from status='closed' entry rows, today's
    America/New_York trading date only, newest-first
  - result caching (thread-safe, one broker call per TTL window)
  - tripped breaker blocks only new entries — maintenance/reconciliation/
    SL-TP monitoring and order sync keep running regardless

Temporary SQLite only (never trading_agent.db), fake broker, no network.
"""
from datetime import datetime, timedelta, timezone

import pytest

import risk.circuit_breakers as cb_mod
import data.storage.db as db
from risk.circuit_breakers import check_circuit_breakers


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_trading_agent.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_schema()
    return db_path


@pytest.fixture(autouse=True)
def reset_cb_cache(monkeypatch):
    """Each test gets a clean circuit-breaker cache — otherwise test order
    would leak cached results across tests."""
    monkeypatch.setattr(cb_mod, "_cached_result", None)
    monkeypatch.setattr(cb_mod, "_cache_timestamp", 0.0)
    monkeypatch.setattr(cb_mod, "_last_logged_state", None)


class FakeBroker:
    def __init__(self, equity=100_000.0, last_equity=100_000.0, raise_exc=None):
        self.equity = equity
        self.last_equity = last_equity
        self.raise_exc = raise_exc
        self.call_count = 0

    def get_account(self):
        self.call_count += 1
        if self.raise_exc:
            raise self.raise_exc
        return {
            "portfolio_value": self.equity, "cash": self.equity,
            "equity": self.equity, "last_equity": self.last_equity,
        }


def _use_fake_broker(monkeypatch, broker):
    monkeypatch.setattr(cb_mod, "_get_broker", lambda: broker)


def _market_open(monkeypatch, is_open: bool):
    monkeypatch.setattr("agents.orchestrator._is_market_open", lambda: is_open)


def _insert_closed_entry(pnl: float, closed_at: datetime, ticker="AAPL", trade_id_hint=1):
    with db.get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO trade_decisions
               (ticker, side, order_type, quantity, entry_price, stop_loss, take_profit,
                confidence, reasoning, status, created_at, metadata, strategy_mode,
                client_order_id, pnl, closed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ticker, "buy", "limit", 10, 100.0, 98.0, 104.0, 0.6, "test",
             "closed", closed_at.isoformat(), "{}", "intraday",
             f"entry-{trade_id_hint}-intraday", pnl, closed_at.isoformat()),
        )
        return cur.lastrowid


_NY_NOON_TODAY = None  # computed per-test via helper below


def _ny_today_at(hour: int, minute: int = 0):
    from zoneinfo import ZoneInfo
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    return now_ny.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _ny_yesterday_at(hour: int, minute: int = 0):
    return _ny_today_at(hour, minute) - timedelta(days=1)


# ─────────────────────────────────────────────────────────────────────────────
# Daily loss (from equity/last_equity)
# ─────────────────────────────────────────────────────────────────────────────

class TestDailyLoss:

    def test_trips_at_3_1_percent_loss(self, temp_db, monkeypatch):
        _market_open(monkeypatch, True)
        _use_fake_broker(monkeypatch, FakeBroker(equity=96_900.0, last_equity=100_000.0))  # -3.1%
        triggered, reason = check_circuit_breakers()
        assert triggered is True
        assert "Daily P/L" in reason

    def test_does_not_trip_at_2_9_percent_loss(self, temp_db, monkeypatch):
        _market_open(monkeypatch, True)
        _use_fake_broker(monkeypatch, FakeBroker(equity=97_100.0, last_equity=100_000.0))  # -2.9%
        triggered, reason = check_circuit_breakers()
        assert triggered is False

    def test_unrealized_only_loss_counts_with_no_closed_trades(self, temp_db, monkeypatch):
        # No trades in the DB at all — purely unrealized equity drop.
        _market_open(monkeypatch, True)
        _use_fake_broker(monkeypatch, FakeBroker(equity=95_000.0, last_equity=100_000.0))  # -5%
        triggered, reason = check_circuit_breakers()
        assert triggered is True
        assert "Daily P/L" in reason

    def test_broker_error_fails_closed(self, temp_db, monkeypatch):
        _market_open(monkeypatch, True)
        _use_fake_broker(monkeypatch, FakeBroker(raise_exc=RuntimeError("timeout")))
        triggered, reason = check_circuit_breakers()
        assert triggered is True
        assert reason == "cannot verify daily P/L"

    def test_missing_last_equity_fails_closed(self, temp_db, monkeypatch):
        _market_open(monkeypatch, True)
        broker = FakeBroker(equity=100_000.0, last_equity=100_000.0)
        monkeypatch.setattr(cb_mod, "_get_broker", lambda: broker)
        # Simulate the broker genuinely not reporting last_equity.
        orig_get_account = broker.get_account
        monkeypatch.setattr(broker, "get_account", lambda: {**orig_get_account(), "last_equity": None})
        triggered, reason = check_circuit_breakers()
        assert triggered is True
        assert reason == "cannot verify daily P/L"

    def test_outside_market_hours_reports_not_tripped(self, temp_db, monkeypatch):
        _market_open(monkeypatch, False)
        # Equity drop that WOULD trip if the market were open.
        _use_fake_broker(monkeypatch, FakeBroker(equity=90_000.0, last_equity=100_000.0))
        triggered, reason = check_circuit_breakers()
        assert triggered is False


# ─────────────────────────────────────────────────────────────────────────────
# Consecutive loss streak
# ─────────────────────────────────────────────────────────────────────────────

class TestConsecutiveLossStreak:

    def test_four_losses_today_trips(self, temp_db, monkeypatch):
        _market_open(monkeypatch, False)  # isolate from the daily-loss leg
        for i in range(4):
            _insert_closed_entry(-10.0, _ny_today_at(10, i), trade_id_hint=i + 1)
        triggered, reason = check_circuit_breakers()
        assert triggered is True
        assert "streak" in reason.lower()

    def test_three_losses_plus_a_win_does_not_trip(self, temp_db, monkeypatch):
        _market_open(monkeypatch, False)
        _insert_closed_entry(50.0, _ny_today_at(9), trade_id_hint=1)
        for i in range(3):
            _insert_closed_entry(-10.0, _ny_today_at(10, i), trade_id_hint=i + 2)
        triggered, reason = check_circuit_breakers()
        assert triggered is False

    def test_win_then_four_losses_newest_first_trips(self, temp_db, monkeypatch):
        _market_open(monkeypatch, False)
        _insert_closed_entry(50.0, _ny_today_at(9), trade_id_hint=1)       # oldest: win
        for i in range(4):
            _insert_closed_entry(-10.0, _ny_today_at(10, i), trade_id_hint=i + 2)  # newest 4: losses
        triggered, reason = check_circuit_breakers()
        assert triggered is True

    def test_four_losses_then_win_does_not_trip(self, temp_db, monkeypatch):
        _market_open(monkeypatch, False)
        for i in range(4):
            _insert_closed_entry(-10.0, _ny_today_at(9, i), trade_id_hint=i + 1)  # oldest 4: losses
        _insert_closed_entry(50.0, _ny_today_at(11), trade_id_hint=5)             # newest: win
        triggered, reason = check_circuit_breakers()
        assert triggered is False

    def test_yesterdays_losses_not_counted(self, temp_db, monkeypatch):
        _market_open(monkeypatch, False)
        for i in range(4):
            _insert_closed_entry(-10.0, _ny_yesterday_at(10, i), trade_id_hint=i + 1)
        triggered, reason = check_circuit_breakers()
        assert triggered is False

    def test_status_closed_rows_are_counted_not_filled(self, temp_db, monkeypatch):
        """Regression for the original bug: it filtered status=='filled',
        but closed trades are status=='closed'."""
        _market_open(monkeypatch, False)
        for i in range(4):
            tid = _insert_closed_entry(-10.0, _ny_today_at(10, i), trade_id_hint=i + 1)
            with db.get_connection() as conn:
                row = conn.execute("SELECT status FROM trade_decisions WHERE id=?", (tid,)).fetchone()
                assert row["status"] == "closed"
        triggered, reason = check_circuit_breakers()
        assert triggered is True


# ─────────────────────────────────────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────────────────────────────────────

class TestCache:

    def test_two_calls_within_ttl_make_one_broker_call(self, temp_db, monkeypatch):
        _market_open(monkeypatch, True)
        broker = FakeBroker(equity=100_000.0, last_equity=100_000.0)
        _use_fake_broker(monkeypatch, broker)

        check_circuit_breakers()
        check_circuit_breakers()

        assert broker.call_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator behavior while tripped
# ─────────────────────────────────────────────────────────────────────────────

class TestOrchestratorBehaviorWhileTripped:

    def test_run_maintenance_runs_sync_reconcile_and_sltp_even_when_tripped(self, temp_db, monkeypatch):
        from agents.orchestrator import Orchestrator
        from core.config import TradingMode

        calls = []
        monkeypatch.setattr("agents.orchestrator.sync_broker_orders", lambda: calls.append("sync"))
        monkeypatch.setattr("agents.orchestrator._is_market_open", lambda: True)
        monkeypatch.setattr("agents.orchestrator.check_circuit_breakers", lambda: (True, "daily loss"))
        monkeypatch.setattr("execution.broker.alpaca.AlpacaBroker.get_account",
                             lambda self: {"portfolio_value": 100_000.0})

        orch = Orchestrator(TradingMode.INTRADAY)
        monkeypatch.setattr(orch, "_reconcile_closed_trades", lambda: calls.append("reconcile") or {})
        monkeypatch.setattr(orch, "check_sl_tp_exits", lambda: calls.append("sltp"))

        orch.run_maintenance()

        assert calls == ["sync", "reconcile", "sltp"]

    def test_run_all_while_tripped_runs_only_tickers_with_open_entry(self, temp_db, monkeypatch):
        from agents.orchestrator import Orchestrator
        from core.config import TradingMode, WATCHLIST

        monkeypatch.setattr("agents.orchestrator.sync_broker_orders", lambda: None)
        monkeypatch.setattr("agents.orchestrator._is_market_open", lambda: True)
        monkeypatch.setattr("agents.orchestrator.check_circuit_breakers", lambda: (True, "daily loss"))
        monkeypatch.setattr("execution.broker.alpaca.AlpacaBroker.get_account",
                             lambda self: {"portfolio_value": 100_000.0})
        monkeypatch.setattr("agents.orchestrator.warm_up", lambda tickers, timeframes: None)

        held_ticker = WATCHLIST[0]
        with db.get_connection() as conn:
            conn.execute(
                """INSERT INTO trade_decisions
                   (ticker, side, order_type, quantity, entry_price, stop_loss, take_profit,
                    confidence, reasoning, status, created_at, metadata, strategy_mode, client_order_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (held_ticker, "buy", "limit", 10, 100.0, 98.0, 104.0, 0.6, "test",
                 "filled", datetime.now(timezone.utc).isoformat(), "{}", "intraday",
                 "entry-900-intraday"),
            )

        orch = Orchestrator(TradingMode.INTRADAY)
        monkeypatch.setattr(orch, "_reconcile_closed_trades", lambda: {})
        monkeypatch.setattr(orch, "check_sl_tp_exits", lambda: None)

        ran_tickers = []
        monkeypatch.setattr(orch, "run_ticker",
                             lambda ticker, *a, **kw: (ran_tickers.append(ticker), {})[1])

        orch.run_all()

        assert ran_tickers == [held_ticker]

    def test_run_all_premarket_only_ignores_breaker(self, temp_db, monkeypatch):
        from agents.orchestrator import Orchestrator
        from core.config import TradingMode, WATCHLIST

        monkeypatch.setattr("agents.orchestrator.sync_broker_orders", lambda: None)
        monkeypatch.setattr("agents.orchestrator._is_market_open", lambda: False)  # irrelevant for premarket_only
        cb_calls = []
        monkeypatch.setattr("agents.orchestrator.check_circuit_breakers",
                             lambda: (cb_calls.append(1), (True, "daily loss"))[1])
        monkeypatch.setattr("execution.broker.alpaca.AlpacaBroker.get_account",
                             lambda self: {"portfolio_value": 100_000.0})
        monkeypatch.setattr("agents.orchestrator.warm_up", lambda tickers, timeframes: None)

        orch = Orchestrator(TradingMode.INTRADAY)
        monkeypatch.setattr(orch, "_reconcile_closed_trades", lambda: {})
        monkeypatch.setattr(orch, "check_sl_tp_exits", lambda: None)

        ran_tickers = []
        monkeypatch.setattr(orch, "run_ticker",
                             lambda ticker, *a, **kw: (ran_tickers.append(ticker), {})[1])

        orch.run_all(premarket_only=True)

        assert cb_calls == []  # check_circuit_breakers() never called for premarket_only
        assert set(ran_tickers) == set(WATCHLIST)  # full watchlist runs, breaker ignored


# ─────────────────────────────────────────────────────────────────────────────
# validate_plan() still blocks new entries while tripped
# ─────────────────────────────────────────────────────────────────────────────

class TestValidatePlanStillBlocksEntries:

    def test_validate_plan_rejects_new_entry_while_tripped(self, temp_db, monkeypatch):
        from agents.risk_manager import RiskManagerAgent
        from core.config import TradingMode
        from core.constants import OrderSide, OrderType
        from core.models import TradePlan

        monkeypatch.setattr("agents.risk_manager.check_circuit_breakers", lambda: (True, "daily loss"))

        plan = TradePlan(
            ticker="AAPL", side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity=10, entry_price=200.0, stop_loss=198.0, take_profit=204.0,
            confidence=0.6, reasoning="test",
        )
        risk = RiskManagerAgent(TradingMode.INTRADAY)
        approved, reason = risk.validate_plan(plan, 100_000.0)
        assert approved is False
        assert "Circuit breaker" in reason
