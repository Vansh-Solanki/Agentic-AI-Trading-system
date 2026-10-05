import logging
from datetime import datetime, timezone
from core.models import TradePlan
from core.constants import OrderType
from execution.broker.alpaca import AlpacaBroker

logger = logging.getLogger(__name__)

_broker = AlpacaBroker()


def route_order(plan: TradePlan, client_order_id: str | None = None) -> dict:
    """
    Routes the trade plan to the broker.
    Returns the broker's order-acceptance state, not a fill: {order_id, status,
    filled_price, filled_qty, filled_at}. filled_price/filled_at are None/0 unless
    Alpaca already reports a real fill at submit time (rare) — the normal case is
    status='new'/'accepted' with no fill yet. Callers (agents/execution.py) must
    check `status`/`filled_price` before treating this as a fill; the order's
    eventual real outcome is finalized later by execution/order_sync.py.
    client_order_id is tagged on the Alpaca order so reconciliation/order_sync can
    match orders to specific DB trade rows per (ticker, strategy_mode) lane.
    """
    logger.info(
        "Routing order: %s %s x%s | entry=%s SL=%s TP=%s | coid=%s",
        plan.side.value, plan.ticker, plan.quantity,
        plan.entry_price, plan.stop_loss, plan.take_profit,
        client_order_id,
    )

    if plan.order_type == OrderType.MARKET:
        return _broker.submit_market_order(plan, client_order_id)
    elif plan.order_type == OrderType.LIMIT:
        return _broker.submit_limit_order(plan, client_order_id)
    elif plan.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
        return _broker.submit_stop_order(plan, client_order_id)
    else:
        raise ValueError(f"Unsupported order type: {plan.order_type}")
