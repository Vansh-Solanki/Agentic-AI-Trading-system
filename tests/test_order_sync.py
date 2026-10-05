"""
Tests for Bug 6: orders were recorded as FILLED the moment they were
submitted, with a fabricated fill price (the planned entry price, not
anything the broker actually reported).

Covers:
  - agents/execution.py: submit now records SUBMITTED (not FILLED) unless the
    broker already reports a real fill.
  - execution/order_sync.py: the new idempotent sync pass that finalizes
    in-flight orders against a fake broker (no network, no real Alpaca calls).
  - execution/broker/alpaca.py: limit price buffer math, and the fail-closed
    ALLOW_STUB_BROKER behavior on init failure.
  - Bug 5 interaction: a SUBMITTED entry still blocks a duplicate in the same
    (ticker, lane) and still allows the other lane.
  - Bug 6 Part C wiring: sync runs even when the circuit breaker is tripped.

Uses a temporary SQLite file (monkeypatches data.storage.db.DB_PATH), never
trading_agent.db. No network calls anywhere in this file.
"""
from datetime import datetime, timedelta, timezone

import pytest

import core.config as cfg
import data.storage.db as db
from core.constants import TradeStatus
from data.storage.decision_store import (
    get_open_trades, get_filled_trades, get_trade_by_id, has_pending_exit,
)
from execution.broker.alpaca import AlpacaBroker
import execution.order_sync as order_sync_mod
from execution.order_sync import sync_broker_orders


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_trading_agent.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_schema()
    return db_path


def _insert_row(ticker, status, client_order_id, strategy_mode="intraday",
                 entry_price=100.0, quantity=10, side="buy", age_minutes=0.0,
                 metadata="{}"):
    created_at = (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).isoformat()
    with db.get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO trade_decisions
               (ticker, side, order_type, quantity, entry_price, stop_loss, take_profit,
                confidence, reasoning, status, created_at, metadata, strategy_mode, client_order_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ticker, side, "limit", quantity, entry_price, entry_price * 0.99, entry_price * 1.02,
             0.6, "test row", status, created_at, metadata, strategy_mode, client_order_id),
        )
        return cur.lastrowid


class FakeBroker:
    """Stands in for AlpacaBroker in order_sync tests. No network calls."""

    def __init__(self):
        self._states: dict[str, object] = {}
        self.cancelled: list[str] = []

    def set_state(self, coid, status=None, filled_qty=0.0, filled_avg_price=None,
                  filled_at=None, missing=False, error=None):
        if missing:
            self._states[coid] = "__404__"
        elif error is not None:
            self._states[coid] = error
        else:
            self._states[coid] = {
                "order_id": f"order-{coid}",
                "status": status,
                "filled_qty": filled_qty,
                "filled_avg_price": filled_avg_price,
                "filled_at": filled_at or datetime.now(timezone.utc),
            }

    def get_order_state(self, coid):
        state = self._states.get(coid, "__404__")
        if state == "__404__":
            return None
        if isinstance(state, Exception):
            raise state
        return state

    def cancel_order(self, coid):
        self.cancelled.append(coid)


@pytest.fixture
def fake_broker(monkeypatch):
    broker = FakeBroker()
    monkeypatch.setattr(order_sync_mod, "AlpacaBroker", lambda: broker)
    return broker


# ─────────────────────────────────────────────────────────────────────────────
# 1 & 2: submit -> SUBMITTED, sync -> FILLED
# ─────────────────────────────────────────────────────────────────────────────

class TestSubmitAndSync:

    def test_submit_records_submitted_not_fabricated_fill(self, temp_db, monkeypatch):
        from agents.execution import ExecutionAgent
        from core.config import TradingMode
        from core.constants import OrderSide, OrderType
        from core.models import TradePlan

        plan = TradePlan(
            ticker="AAPL", side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity=10, entry_price=200.0, stop_loss=198.0, take_profit=204.0,
            confidence=0.6, reasoning="test", strategy_mode="intraday",
        )
        # Broker accepted the order but hasn't filled it yet — the normal case.
        monkeypatch.setattr(
            "agents.execution.route_order",
            lambda plan, client_order_id=None: {
                "order_id": "abc123", "status": "new",
                "filled_price": None, "filled_qty": 0.0, "filled_at": None,
            },
        )
        agent = ExecutionAgent(TradingMode.INTRADAY)
        output = agent.run("AAPL", plan=plan)
        trade_id = output.raw_data["trade_id"]

        row = get_trade_by_id(trade_id)
        assert row["status"] == TradeStatus.SUBMITTED.value
        assert row["filled_price"] is None
        assert row["quantity"] == 10

    def test_sync_promotes_submitted_to_filled(self, temp_db, fake_broker):
        trade_id = _insert_row("AAPL", "submitted", "entry-77-intraday", strategy_mode="intraday", quantity=10)
        fake_broker.set_state("entry-77-intraday", status="filled", filled_qty=10, filled_avg_price=201.23)

        sync_broker_orders()

        row = get_trade_by_id(trade_id)
        assert row["status"] == TradeStatus.FILLED.value
        assert row["filled_price"] == pytest.approx(201.23)
        assert row["quantity"] == 10
        assert any(t["id"] == trade_id for t in get_filled_trades(strategy_mode="intraday"))


# ─────────────────────────────────────────────────────────────────────────────
# 3 & 4: entry timeout -> cancel -> finalize (partial fill vs. no fill)
# ─────────────────────────────────────────────────────────────────────────────

class TestEntryTimeout:

    def test_partial_fill_then_timeout_finalizes_filled_with_partial_qty(self, temp_db, fake_broker):
        trade_id = _insert_row("NVDA", "submitted", "entry-5-intraday", strategy_mode="intraday",
                                quantity=100, age_minutes=cfg.ENTRY_ORDER_TIMEOUT_MINUTES + 1)
        # Pass 1: still open (partially_filled), old enough -> cancel requested.
        fake_broker.set_state("entry-5-intraday", status="partially_filled", filled_qty=30)
        sync_broker_orders()
        assert "entry-5-intraday" in fake_broker.cancelled
        row = get_trade_by_id(trade_id)
        assert row["status"] == TradeStatus.SUBMITTED.value  # not finalized yet

        # Pass 2: broker now reports it died (canceled) with the partial fill intact.
        fake_broker.set_state("entry-5-intraday", status="canceled", filled_qty=30, filled_avg_price=450.0)
        sync_broker_orders()

        row = get_trade_by_id(trade_id)
        assert row["status"] == TradeStatus.FILLED.value
        assert row["quantity"] == 30
        import json
        assert json.loads(row["metadata"])["requested_quantity"] == 100

    def test_no_fill_after_timeout_becomes_cancelled_and_not_open(self, temp_db, fake_broker):
        trade_id = _insert_row("NVDA", "submitted", "entry-6-intraday", strategy_mode="intraday",
                                quantity=100, age_minutes=cfg.ENTRY_ORDER_TIMEOUT_MINUTES + 1)
        fake_broker.set_state("entry-6-intraday", status="new", filled_qty=0)
        sync_broker_orders()
        assert "entry-6-intraday" in fake_broker.cancelled

        fake_broker.set_state("entry-6-intraday", status="canceled", filled_qty=0)
        sync_broker_orders()

        row = get_trade_by_id(trade_id)
        assert row["status"] == TradeStatus.CANCELLED.value
        assert not any(t["id"] == trade_id for t in get_open_trades())

    def test_young_entry_not_cancelled_even_if_open(self, temp_db, fake_broker):
        trade_id = _insert_row("NVDA", "submitted", "entry-8-intraday", strategy_mode="intraday",
                                quantity=100, age_minutes=1)
        fake_broker.set_state("entry-8-intraday", status="new", filled_qty=0)
        sync_broker_orders()
        assert "entry-8-intraday" not in fake_broker.cancelled
        row = get_trade_by_id(trade_id)
        assert row["status"] == TradeStatus.SUBMITTED.value


# ─────────────────────────────────────────────────────────────────────────────
# 5: rejected / expired
# ─────────────────────────────────────────────────────────────────────────────

class TestRejectedExpired:

    def test_rejected_marks_failed(self, temp_db, fake_broker):
        trade_id = _insert_row("XOM", "submitted", "entry-9-intraday", strategy_mode="intraday")
        fake_broker.set_state("entry-9-intraday", status="rejected", filled_qty=0)
        sync_broker_orders()
        assert get_trade_by_id(trade_id)["status"] == TradeStatus.FAILED.value

    def test_expired_zero_fill_marks_cancelled(self, temp_db, fake_broker):
        trade_id = _insert_row("XOM", "submitted", "entry-10-intraday", strategy_mode="intraday")
        fake_broker.set_state("entry-10-intraday", status="expired", filled_qty=0)
        sync_broker_orders()
        assert get_trade_by_id(trade_id)["status"] == TradeStatus.CANCELLED.value


# ─────────────────────────────────────────────────────────────────────────────
# 6: transient broker error leaves the row untouched
# ─────────────────────────────────────────────────────────────────────────────

class TestTransientError:

    def test_transient_broker_error_leaves_row_unchanged_and_does_not_raise(self, temp_db, fake_broker):
        trade_id = _insert_row("META", "submitted", "entry-11-intraday", strategy_mode="intraday")
        fake_broker.set_state("entry-11-intraday", error=RuntimeError("connection reset"))
        sync_broker_orders()  # must not raise
        row = get_trade_by_id(trade_id)
        assert row["status"] == TradeStatus.SUBMITTED.value
        assert "entry-11-intraday" not in fake_broker.cancelled


# ─────────────────────────────────────────────────────────────────────────────
# 7: 404 handling, stale vs. young
# ─────────────────────────────────────────────────────────────────────────────

class TestNotFound:

    def test_404_older_than_stale_window_marks_failed(self, temp_db, fake_broker):
        trade_id = _insert_row("GOOGL", "submitted", "entry-12-intraday", strategy_mode="intraday",
                                age_minutes=cfg.STALE_ORDER_MINUTES + 1)
        # No set_state call -> get_order_state returns None (404) by default.
        sync_broker_orders()
        assert get_trade_by_id(trade_id)["status"] == TradeStatus.FAILED.value

    def test_404_younger_than_stale_window_untouched(self, temp_db, fake_broker):
        trade_id = _insert_row("GOOGL", "submitted", "entry-13-intraday", strategy_mode="intraday",
                                age_minutes=1)
        sync_broker_orders()
        assert get_trade_by_id(trade_id)["status"] == TradeStatus.SUBMITTED.value


# ─────────────────────────────────────────────────────────────────────────────
# 8: exit rows
# ─────────────────────────────────────────────────────────────────────────────

class TestExitRows:

    def test_exit_fills_with_real_price(self, temp_db, fake_broker):
        entry_id = _insert_row("BA", "filled", None, strategy_mode="intraday", quantity=20)
        # Assign the entry's real client_order_id now that we know its id.
        with db.get_connection() as conn:
            conn.execute("UPDATE trade_decisions SET client_order_id=? WHERE id=?",
                         (f"entry-{entry_id}-intraday", entry_id))
        exit_coid = f"exit-{entry_id}-intraday"
        exit_id = _insert_row("BA", "submitted", exit_coid, strategy_mode="intraday", quantity=20, side="sell")

        fake_broker.set_state(exit_coid, status="filled", filled_qty=20, filled_avg_price=195.5)
        sync_broker_orders()

        row = get_trade_by_id(exit_id)
        assert row["status"] == TradeStatus.FILLED.value
        assert row["filled_price"] == pytest.approx(195.5)

    def test_exit_dies_unfilled_becomes_cancelled_and_unblocks_retry(self, temp_db, fake_broker):
        entry_id = _insert_row("BA", "filled", None, strategy_mode="intraday", quantity=20)
        with db.get_connection() as conn:
            conn.execute("UPDATE trade_decisions SET client_order_id=? WHERE id=?",
                         (f"entry-{entry_id}-intraday", entry_id))
        exit_coid = f"exit-{entry_id}-intraday"
        _insert_row("BA", "submitted", exit_coid, strategy_mode="intraday", quantity=20, side="sell")

        assert has_pending_exit(entry_id, "intraday") is True

        fake_broker.set_state(exit_coid, status="canceled", filled_qty=0)
        sync_broker_orders()

        assert has_pending_exit(entry_id, "intraday") is False


# ─────────────────────────────────────────────────────────────────────────────
# 9: idempotency
# ─────────────────────────────────────────────────────────────────────────────

class TestIdempotent:

    def test_sync_run_twice_gives_same_result(self, temp_db, fake_broker):
        trade_id = _insert_row("AAPL", "submitted", "entry-20-intraday", strategy_mode="intraday", quantity=10)
        fake_broker.set_state("entry-20-intraday", status="filled", filled_qty=10, filled_avg_price=199.9)

        sync_broker_orders()
        first = dict(get_trade_by_id(trade_id))

        sync_broker_orders()
        second = dict(get_trade_by_id(trade_id))

        assert first == second


# ─────────────────────────────────────────────────────────────────────────────
# 10: broker init failure fail-closed
# ─────────────────────────────────────────────────────────────────────────────

class TestBrokerInitFailClosed:

    def test_init_failure_raises_by_default(self, monkeypatch):
        monkeypatch.setattr("execution.broker.alpaca.ALLOW_STUB_BROKER", False)

        def _boom(*args, **kwargs):
            raise RuntimeError("no credentials")
        import alpaca.trading.client as alpaca_client_mod
        monkeypatch.setattr(alpaca_client_mod.TradingClient, "__init__", _boom)

        with pytest.raises(RuntimeError):
            AlpacaBroker()

    def test_init_failure_falls_back_to_stub_when_allowed(self, monkeypatch):
        monkeypatch.setattr("execution.broker.alpaca.ALLOW_STUB_BROKER", True)

        def _boom(*args, **kwargs):
            raise RuntimeError("no credentials")
        import alpaca.trading.client as alpaca_client_mod
        monkeypatch.setattr(alpaca_client_mod.TradingClient, "__init__", _boom)

        broker = AlpacaBroker()
        assert broker._available is False
        from core.models import TradePlan
        from core.constants import OrderSide, OrderType
        plan = TradePlan(ticker="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET,
                          quantity=1, entry_price=100.0, stop_loss=99.0, take_profit=101.0,
                          confidence=0.5, reasoning="x")
        result = broker.submit_market_order(plan)
        assert result["status"] == "stub_filled"


# ─────────────────────────────────────────────────────────────────────────────
# 11: limit price buffer math
# ─────────────────────────────────────────────────────────────────────────────

class TestLimitPriceMath:

    def _broker_with_fake_client(self, monkeypatch):
        broker = AlpacaBroker.__new__(AlpacaBroker)  # bypass __init__ / real TradingClient
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide as AlpacaOrderSide, TimeInForce
        broker._LimitOrderRequest = LimitOrderRequest
        broker._OrderSide = AlpacaOrderSide
        broker._TimeInForce = TimeInForce
        broker._available = True

        captured = {}

        class _FakeOrder:
            id = "fake-id"
            status = "new"
            filled_qty = None
            filled_avg_price = None
            filled_at = None

        class _FakeClient:
            def submit_order(self, req):
                captured["req"] = req
                return _FakeOrder()
        broker.client = _FakeClient()
        return broker, captured

    def test_buy_limit_moves_price_up(self, monkeypatch):
        from core.models import TradePlan
        from core.constants import OrderSide, OrderType
        broker, captured = self._broker_with_fake_client(monkeypatch)
        plan = TradePlan(ticker="AAPL", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                          quantity=10, entry_price=100.00, stop_loss=99.0, take_profit=102.0,
                          confidence=0.5, reasoning="x")
        broker.submit_limit_order(plan)
        req = captured["req"]
        assert req.limit_price == pytest.approx(100.20)
        assert req.time_in_force == broker._TimeInForce.DAY

    def test_sell_limit_moves_price_down(self, monkeypatch):
        from core.models import TradePlan
        from core.constants import OrderSide, OrderType
        broker, captured = self._broker_with_fake_client(monkeypatch)
        plan = TradePlan(ticker="AAPL", side=OrderSide.SELL, order_type=OrderType.LIMIT,
                          quantity=10, entry_price=100.00, stop_loss=101.0, take_profit=98.0,
                          confidence=0.5, reasoning="x")
        broker.submit_limit_order(plan)
        req = captured["req"]
        assert req.limit_price == pytest.approx(99.80)
        assert req.time_in_force == broker._TimeInForce.DAY


# ─────────────────────────────────────────────────────────────────────────────
# 12: Bug 5 interaction — SUBMITTED entries still block duplicates
# ─────────────────────────────────────────────────────────────────────────────

class TestSubmittedBlocksDuplicateEntry:

    def test_submitted_entry_blocks_same_ticker_same_lane(self, temp_db):
        from risk.portfolio_exposure import check_exposure_limits
        _insert_row("AAPL", "submitted", "entry-30-intraday", strategy_mode="intraday", quantity=10)

        allowed, reason = check_exposure_limits("AAPL", 5, 200.0, "intraday")
        assert allowed is False
        assert "AAPL" in reason

    def test_submitted_entry_allows_other_lane(self, temp_db):
        from risk.portfolio_exposure import check_exposure_limits
        _insert_row("AAPL", "submitted", "entry-31-intraday", strategy_mode="intraday", quantity=10)

        allowed, reason = check_exposure_limits("AAPL", 5, 200.0, "long_term")
        assert allowed is True


# ─────────────────────────────────────────────────────────────────────────────
# 13: sync runs even when the circuit breaker is tripped
# ─────────────────────────────────────────────────────────────────────────────

class TestSyncRunsDespiteCircuitBreaker:

    def test_run_maintenance_calls_sync_before_circuit_breaker_check(self, temp_db, monkeypatch):
        from agents.orchestrator import Orchestrator
        from core.config import TradingMode

        calls = []
        monkeypatch.setattr("agents.orchestrator.sync_broker_orders", lambda: calls.append("sync"))
        monkeypatch.setattr("agents.orchestrator._is_market_open", lambda: True)
        monkeypatch.setattr("agents.orchestrator.check_circuit_breakers", lambda: (True, "daily loss limit"))

        orch = Orchestrator(TradingMode.INTRADAY)
        orch.run_maintenance()

        assert calls == ["sync"]
