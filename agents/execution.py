import json
from core.models import AgentOutput, TradePlan
from core.constants import TradeStatus
from agents.base_agent import BaseAgent
from execution.order_router import route_order
from data.storage.decision_store import (
    save_trade_plan,
    update_trade_status,
    set_client_order_id,
    get_open_filled_entry_trade,
)


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
            update_trade_status(
                trade_id,
                TradeStatus.FILLED.value,
                filled_price=fill.get("filled_price"),
                filled_at=fill.get("filled_at"),
            )
            summary = f"Order FILLED — {plan.side.value} {plan.quantity} {ticker} @ {fill.get('filled_price')}"
        except Exception as e:
            update_trade_status(trade_id, TradeStatus.FAILED.value)
            summary = f"Order submission FAILED: {e}"
            self.logger.exception("Order routing failed for trade_id=%d", trade_id)

        output = AgentOutput(agent_name=self.name, ticker=ticker, signals=[], summary=summary,
                             raw_data={"trade_id": trade_id, "client_order_id": client_order_id})
        self._log_output(output)
        return output
