import json
from datetime import datetime, timezone
from core.models import AgentOutput, TradePlan
from core.constants import TradeStatus
from agents.base_agent import BaseAgent
from execution.order_router import route_order
from data.storage.decision_store import (
    save_trade_plan,
    update_trade_status,
    update_order_result,
    set_client_order_id,
    get_open_filled_entry_trade,
)

# Session 52 (Bug 6): a broker order-acceptance status counts as a genuine,
# bookable fill. 'filled' is Alpaca's real status; 'stub_filled' only exists
# behind ALLOW_STUB_BROKER=true (tests/backtests) and is treated the same way
# here so stub-mode runs still produce usable trade rows.
_FILLED_STATUSES = {"filled", "stub_filled"}


class ExecutionAgent(BaseAgent):
    """Submits trade plans directly to the broker (fully autonomous)."""

    @property
    def name(self) -> str:
        return "ExecutionAgent"

    def run(self, ticker: str, **kwargs) -> AgentOutput:
        plan: TradePlan | None = kwargs.get("plan")
        all_signals = kwargs.get("all_signals") or []
        signal_snapshot = json.dumps([
            {"source": s.source.value, "signal_type": s.signal_type.value, "confidence": s.confidence}
            for s in all_signals
        ]) if all_signals else None

        if plan is None:
            return AgentOutput(agent_name=self.name, ticker=ticker, signals=[],
                               summary="No plan to execute.")

        # Hardening follow-up to Bug 6: plan.created_at defaults to when
        # TradePlanAgent built the plan, which can be a meaningful amount of
        # time before this save (the Critic LLM call sits in between). Every
        # order_sync.py age rule (entry timeout, stale-404, hard age cap)
        # measures from created_at, so it must reflect submission time, not
        # plan-build time, or every age budget silently loses however long
        # the Critic call took. Confirmed safe against created_at's only two
        # trade_decisions readers (dashboard/views/performance.py's history
        # sort/cumulative-PnL chart, decision_store.get_trade_history()'s
        # ORDER BY) — both just need consistent chronological ordering across
        # trades, which a few-seconds-later timestamp per trade preserves.
        plan.created_at = datetime.now(timezone.utc)
        trade_id = save_trade_plan(plan, signal_snapshot=signal_snapshot)

        # Build client_order_id encoding the lane so reconciliation can match fills
        # per (ticker, strategy_mode) without ambiguity when both lanes are open.
        # Entry (new long BUY or new short SELL):  "entry-{trade_id}-{strategy_mode}"
        # Exit  (long close SELL or short close BUY): "exit-{entry_trade_id}-{strategy_mode}"
        # The distinction is presence of an existing open position, not the order side.
        entry = get_open_filled_entry_trade(ticker, plan.strategy_mode)
        if entry:
            client_order_id = f"exit-{entry['id']}-{plan.strategy_mode}"
        else:
            client_order_id = f"entry-{trade_id}-{plan.strategy_mode}"

        set_client_order_id(trade_id, client_order_id)

        try:
            fill = route_order(plan, client_order_id=client_order_id)
            status = fill.get("status")
            filled_price = fill.get("filled_price")
            filled_qty = fill.get("filled_qty") or 0.0
            # Session 52 (Bug 6, item 1): route_order() used to be treated as
            # always meaning FILLED. It almost never does at submit time — a
            # limit order is only 'new'/'accepted' until the broker actually
            # matches it, sometimes minutes or days later. Only book FILLED
            # when the broker has already reported a real price and quantity;
            # otherwise the order is merely SUBMITTED, and
            # execution/order_sync.py finalizes its real outcome later.
            if status in _FILLED_STATUSES and filled_price is not None and filled_qty > 0:
                update_order_result(
                    trade_id,
                    TradeStatus.FILLED.value,
                    filled_price=filled_price,
                    filled_at=fill.get("filled_at"),
                    filled_quantity=filled_qty,
                )
                summary = f"Order FILLED — {plan.side.value} {filled_qty} {ticker} @ {filled_price}"
            else:
                update_trade_status(trade_id, TradeStatus.SUBMITTED.value)
                summary = (
                    f"Order SUBMITTED ({status}) — {plan.side.value} {plan.quantity} {ticker}, "
                    f"awaiting fill (client_order_id={client_order_id})"
                )
        except Exception as e:
            update_trade_status(trade_id, TradeStatus.FAILED.value)
            summary = f"Order submission FAILED: {e}"
            self.logger.exception("Order routing failed for trade_id=%d", trade_id)

        output = AgentOutput(agent_name=self.name, ticker=ticker, signals=[], summary=summary,
                             raw_data={"trade_id": trade_id, "client_order_id": client_order_id})
        self._log_output(output)
        return output
