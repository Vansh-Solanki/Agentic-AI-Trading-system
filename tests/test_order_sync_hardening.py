"""
Hardening follow-up to Bug 6 (order lifecycle). Covers the four gaps found
in review:
  0. core.models._utcnow() is already timezone-aware (verified, not fixed).
  1. ExecutionAgent now stamps created_at at save time, not plan-build time.
  2. Orphan rows (NULL client_order_id, never reached the broker) age out to FAILED.
  3. MAX_ORDER_AGE_HOURS hard cap: entries get a cancel request, exits only
     warn (never auto-cancel); an order already pending_cancel doesn't get a
     second cancel request.

Temporary SQLite only (never trading_agent.db), fake broker, no network.
"""
from datetime import datetime, timedelta, timezone

import pytest

import core.config as cfg
import data.storage.db as db
from core.constants import TradeStatus
from data.storage.decision_store import get_trade_by_id
import execution.order_sync as order_sync_mod
from execution.order_sync import sync_broker_orders


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_trading_agent.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_schema()
    return db_path


def _insert_row(ticker, status, client_order_id, strategy_mode="intraday",
                 entry_price=100.0, quantity=10, side="buy", age_minutes=0.0):
    created_at = (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).isoformat()
    with db.get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO trade_decisions
               (ticker, side, order_type, quantity, entry_price, stop_loss, take_profit,
                confidence, reasoning, status, created_at, metadata, strategy_mode, client_order_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ticker, side, "limit", quantity, entry_price, entry_price * 0.99, entry_price * 1.02,
             0.6, "test row", status, created_at, "{}", strategy_mode, client_order_id),
        )
        return cur.lastrowid


class FakeBroker:
    def __init__(self):
        self._states: dict[str, object] = {}
        self.cancelled: list[str] = []

    def set_state(self, coid, status=None, filled_qty=0.0, filled_avg_price=None, filled_at=None):
        self._states[coid] = {
            "order_id": f"order-{coid}", "status": status, "filled_qty": filled_qty,
            "filled_avg_price": filled_avg_price, "filled_at": filled_at or datetime.now(timezone.utc),
        }

    def get_order_state(self, coid):
        return self._states.get(coid)

    def cancel_order(self, coid):
        self.cancelled.append(coid)


@pytest.fixture
def fake_broker(monkeypatch):
    broker = FakeBroker()
    monkeypatch.setattr(order_sync_mod, "AlpacaBroker", lambda: broker)
    return broker


# ─────────────────────────────────────────────────────────────────────────────
# 0. core.models._utcnow() is already timezone-aware
# ─────────────────────────────────────────────────────────────────────────────

def test_utcnow_is_timezone_aware():
    from core.models import _utcnow
    now = _utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


# ─────────────────────────────────────────────────────────────────────────────
# 1. ExecutionAgent stamps created_at at save time
# ─────────────────────────────────────────────────────────────────────────────

class TestCreatedAtFromSubmission:

    def test_execution_agent_overrides_stale_plan_created_at(self, temp_db, monkeypatch):
        from agents.execution import ExecutionAgent
        from core.config import TradingMode
        from core.constants import OrderSide, OrderType
        from core.models import TradePlan

        old_time = datetime.now(timezone.utc) - timedelta(hours=3)
        plan = TradePlan(
            ticker="AAPL", side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity=10, entry_price=200.0, stop_loss=198.0, take_profit=204.0,
            confidence=0.6, reasoning="test", strategy_mode="intraday",
            created_at=old_time,
        )
        monkeypatch.setattr(
            "agents.execution.route_order",
            lambda plan, client_order_id=None: {
                "order_id": "abc", "status": "new", "filled_price": None,
                "filled_qty": 0.0, "filled_at": None,
            },
        )
        agent = ExecutionAgent(TradingMode.INTRADAY)
        output = agent.run("AAPL", plan=plan)
        row = get_trade_by_id(output.raw_data["trade_id"])

        stored = datetime.fromisoformat(row["created_at"])
        if stored.tzinfo is None:
            stored = stored.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - stored).total_seconds()
        assert age_seconds < 5, "created_at should reflect submission time, not the 3-hour-old plan.created_at"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Orphan rows (NULL client_order_id)
# ─────────────────────────────────────────────────────────────────────────────

class TestOrphanRows:

    def test_old_orphan_marked_failed(self, temp_db, fake_broker):
        trade_id = _insert_row("AAPL", "pending", None, age_minutes=cfg.STALE_ORDER_MINUTES + 1)
        sync_broker_orders()
        assert get_trade_by_id(trade_id)["status"] == TradeStatus.FAILED.value

    def test_young_orphan_untouched(self, temp_db, fake_broker):
        trade_id = _insert_row("AAPL", "pending", None, age_minutes=1)
        sync_broker_orders()
        assert get_trade_by_id(trade_id)["status"] == "pending"


# ─────────────────────────────────────────────────────────────────────────────
# 3. MAX_ORDER_AGE_HOURS hard cap
# ─────────────────────────────────────────────────────────────────────────────

class TestHardAgeCap:

    def test_entry_past_hard_cap_gets_cancel_request(self, temp_db, fake_broker):
        age_min = cfg.MAX_ORDER_AGE_HOURS * 60 + 5
        trade_id = _insert_row("NVDA", "submitted", "entry-99-intraday", age_minutes=age_min)
        fake_broker.set_state("entry-99-intraday", status="new", filled_qty=0)
        sync_broker_orders()
        assert "entry-99-intraday" in fake_broker.cancelled
        assert get_trade_by_id(trade_id)["status"] == "submitted"  # not finalized yet, just cancel-requested

    def test_exit_past_hard_cap_never_cancelled(self, temp_db, fake_broker):
        entry_id = _insert_row("BA", "filled", None, quantity=20)
        with db.get_connection() as conn:
            conn.execute("UPDATE trade_decisions SET client_order_id=? WHERE id=?",
                         (f"entry-{entry_id}-intraday", entry_id))
        exit_coid = f"exit-{entry_id}-intraday"
        age_min = cfg.MAX_ORDER_AGE_HOURS * 60 + 5
        _insert_row("BA", "submitted", exit_coid, quantity=20, side="sell", age_minutes=age_min)
        fake_broker.set_state(exit_coid, status="new", filled_qty=0)

        sync_broker_orders()

        assert exit_coid not in fake_broker.cancelled
        # The exit row itself should still be SUBMITTED (not cancelled, not failed).
        with db.get_connection() as conn:
            exit_row = conn.execute(
                "SELECT status FROM trade_decisions WHERE client_order_id=?", (exit_coid,)
            ).fetchone()
        assert exit_row["status"] == "submitted"

    def test_pending_cancel_does_not_get_second_cancel_request(self, temp_db, fake_broker):
        trade_id = _insert_row("NVDA", "submitted", "entry-100-intraday",
                                age_minutes=cfg.ENTRY_ORDER_TIMEOUT_MINUTES + 1)
        fake_broker.set_state("entry-100-intraday", status="pending_cancel", filled_qty=0)
        sync_broker_orders()
        assert "entry-100-intraday" not in fake_broker.cancelled
        assert get_trade_by_id(trade_id)["status"] == "submitted"
