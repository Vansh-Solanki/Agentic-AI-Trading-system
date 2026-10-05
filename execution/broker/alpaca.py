import logging
from datetime import datetime, timezone
from core.config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL, PAPER_TRADING,
    ENTRY_LIMIT_BUFFER_PCT, ALLOW_STUB_BROKER,
)
from core.models import TradePlan

logger = logging.getLogger(__name__)


class AlpacaBroker:
    def __init__(self):
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, StopOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus
            from alpaca.common.exceptions import APIError
            self.client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_TRADING)
            self._MarketOrderRequest = MarketOrderRequest
            self._LimitOrderRequest  = LimitOrderRequest
            self._StopOrderRequest   = StopOrderRequest
            self._OrderSide          = OrderSide
            self._TimeInForce        = TimeInForce
            self._OrderStatus        = OrderStatus
            self._APIError           = APIError
            self._available          = True
            logger.info("Alpaca broker initialized (paper=%s)", PAPER_TRADING)
        except Exception as exc:
            # Session 52 (Bug 6, item 5): this used to catch every failure
            # (missing package or bad credentials alike) and silently fall
            # back to stub mode, which fabricates fills — a broker that can't
            # actually be reached must not pretend trades are happening.
            # Fail closed by default; stub mode is now an explicit opt-in via
            # ALLOW_STUB_BROKER, for tests and backtests only.
            if not ALLOW_STUB_BROKER:
                logger.error(
                    "Alpaca broker init failed (%s) and ALLOW_STUB_BROKER is not set — refusing to start.", exc
                )
                raise
            logger.warning(
                "Alpaca broker init failed (%s) — ALLOW_STUB_BROKER=true, continuing in STUB mode. "
                "No real orders will be placed; fills are fabricated.", exc
            )
            self._available = False

    def submit_market_order(self, plan: TradePlan, client_order_id: str | None = None) -> dict:
        if not self._available:
            return self._stub_fill(plan)
        kw = {"client_order_id": client_order_id} if client_order_id else {}
        req = self._MarketOrderRequest(
            symbol=plan.ticker,
            qty=plan.quantity,
            side=self._OrderSide.BUY if plan.side.value == "buy" else self._OrderSide.SELL,
            time_in_force=self._TimeInForce.DAY,
            **kw,
        )
        order = self.client.submit_order(req)
        return self._order_to_fill_dict(order)

    def submit_limit_order(self, plan: TradePlan, client_order_id: str | None = None) -> dict:
        if not self._available:
            return self._stub_fill(plan)
        kw = {"client_order_id": client_order_id} if client_order_id else {}
        last_price = plan.entry_price or 0.0
        # Session 52 (Bug 6, item 4): a limit pinned exactly at last close
        # under GTC could sit unfilled for days and then execute at a price
        # that no longer reflects reality. Move the limit a small buffer past
        # last price in the marketable direction (buy above, sell below) and
        # use DAY instead of GTC — the order-sync pass (execution/order_sync.py)
        # cancels it if it's still open past ENTRY_ORDER_TIMEOUT_MINUTES anyway.
        if plan.side.value == "buy":
            limit_price = round(last_price * (1 + ENTRY_LIMIT_BUFFER_PCT), 2)
        else:
            limit_price = round(last_price * (1 - ENTRY_LIMIT_BUFFER_PCT), 2)
        req = self._LimitOrderRequest(
            symbol=plan.ticker,
            qty=plan.quantity,
            side=self._OrderSide.BUY if plan.side.value == "buy" else self._OrderSide.SELL,
            time_in_force=self._TimeInForce.DAY,
            limit_price=limit_price,
            **kw,
        )
        order = self.client.submit_order(req)
        return self._order_to_fill_dict(order)

    def submit_stop_order(self, plan: TradePlan, client_order_id: str | None = None) -> dict:
        if not self._available:
            return self._stub_fill(plan)
        kw = {"client_order_id": client_order_id} if client_order_id else {}
        req = self._StopOrderRequest(
            symbol=plan.ticker,
            qty=plan.quantity,
            side=self._OrderSide.BUY if plan.side.value == "buy" else self._OrderSide.SELL,
            time_in_force=self._TimeInForce.GTC,
            # Alpaca rejects sub-penny stop prices (e.g. 322.17999267578125,
            # a float precision artifact from upstream candle data) with
            # "invalid stop_price ... sub-penny increment does not fulfill
            # minimum pricing criteria." Round at this final point, right
            # before the value leaves the app — nothing downstream adjusts it
            # further.
            stop_price=round(plan.entry_price, 2),
            **kw,
        )
        order = self.client.submit_order(req)
        return self._order_to_fill_dict(order)

    def _order_to_fill_dict(self, order) -> dict:
        """
        Session 52 (Bug 6, item 1): route_order()'s return value used to
        always report a fill — `filled_avg_price or plan.entry_price` meant
        an order that only got *accepted* (filled_avg_price is None at submit
        time for every limit order) was recorded as filled at the planned
        price, which never happened. This reports only what Alpaca actually
        says right now; callers (agents/execution.py) decide SUBMITTED vs
        FILLED from `status`/`filled_price`, and execution/order_sync.py
        finalizes it later once the broker actually reports a fill.
        """
        status = order.status.value if hasattr(order.status, "value") else str(order.status)
        filled_qty = float(order.filled_qty) if order.filled_qty else 0.0
        filled_price = float(order.filled_avg_price) if order.filled_avg_price else None
        return {
            "order_id": str(order.id),
            "status": status,
            "filled_price": filled_price,
            "filled_qty": filled_qty,
            "filled_at": order.filled_at,
        }

    def get_order_state(self, client_order_id: str) -> dict | None:
        """
        Return the broker's current view of an order by client_order_id, as
        {order_id, status, filled_qty, filled_avg_price, filled_at}.

        Returns None only when Alpaca reports the order does not exist
        (HTTP 404) — the normal case for a row whose client_order_id hasn't
        reached Alpaca yet, or was never actually submitted. Any other error
        (network, auth, 5xx) raises, so execution/order_sync.py can tell
        "confirmed not found" apart from "could not check right now" and
        leave the row untouched on the latter rather than wrongly failing it.
        """
        if not self._available:
            return None
        try:
            order = self.client.get_order_by_client_id(client_order_id)
        except self._APIError as exc:
            if exc.status_code == 404:
                return None
            raise
        return {
            "order_id": str(order.id),
            "status": order.status.value if hasattr(order.status, "value") else str(order.status),
            "filled_qty": float(order.filled_qty) if order.filled_qty else 0.0,
            "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else None,
            "filled_at": order.filled_at,
        }

    def cancel_order(self, client_order_id: str) -> None:
        """Cancel a working order by client_order_id. Raises if the broker
        has no record of it or the cancel request itself fails — callers
        (order_sync.py) catch and retry next pass rather than treating a
        failed cancel as success."""
        if not self._available:
            logger.info("STUB CANCEL: %s (no real order placed)", client_order_id)
            return
        order = self.client.get_order_by_client_id(client_order_id)
        self.client.cancel_order_by_id(order.id)

    def get_positions(self) -> list[dict]:
        if not self._available:
            return []
        positions = self.client.get_all_positions()
        return [p.__dict__ for p in positions]

    def get_fill_by_client_order_id(self, client_order_id: str) -> float | None:
        """
        Return the filled_avg_price of a SELL order identified by client_order_id,
        or None if the order does not exist or is not yet filled.
        Used by reconciliation to detect per-lane position closures.
        """
        if not self._available:
            return None
        try:
            order = self.client.get_order_by_client_id(client_order_id)
            if order and order.status == self._OrderStatus.FILLED and order.filled_avg_price:
                return float(order.filled_avg_price)
        except Exception:
            # Not found (404) is the normal case when the position is still open
            pass
        return None

    def get_open_symbols(self) -> set[str]:
        """Return the set of ticker symbols currently held as open positions."""
        if not self._available:
            return set()
        try:
            return {p.symbol for p in self.client.get_all_positions()}
        except Exception as exc:
            logger.warning("Could not fetch open positions: %s", exc)
            return set()

    def get_account(self) -> dict:
        """
        Circuit breaker repair: `equity`/`last_equity` added so
        risk/circuit_breakers.py can compute daily P/L (realized + unrealized)
        as (equity - last_equity) / last_equity, instead of only ever seeing
        closed-trade pnl. Both fields confirmed present on alpaca-py's
        TradeAccount model (verified against the installed version via
        TradeAccount.model_fields — not assumed). `last_equity` is Alpaca's
        prior-trading-day closing equity, refreshed once daily at market
        close/settlement, so this ratio is stable intraday.
        """
        if not self._available:
            return {"portfolio_value": 100000.0, "cash": 100000.0, "equity": 100000.0, "last_equity": 100000.0}
        acct = self.client.get_account()
        return {
            "portfolio_value": float(acct.portfolio_value),
            "cash": float(acct.cash),
            "equity": float(acct.equity) if acct.equity is not None else None,
            "last_equity": float(acct.last_equity) if acct.last_equity is not None else None,
        }

    def is_etb(self, ticker: str) -> bool:
        """Return True only if the asset is shortable and easy-to-borrow on Alpaca.

        HTB assets require a locate; paper trading does not support locates.
        ETB/HTB status can change overnight, so this is always checked at order time.
        Fails closed (returns False) on any API error so a locate-required stock is never shorted.
        """
        if not self._available:
            return True  # stub mode — allow shorts for back-testing
        try:
            asset = self.client.get_asset(ticker)
            return bool(asset.shortable and asset.easy_to_borrow)
        except Exception as exc:
            logger.warning("[%s] Could not verify ETB status: %s — blocking short entry", ticker, exc)
            return False

    def _stub_fill(self, plan: TradePlan) -> dict:
        """Only reachable with ALLOW_STUB_BROKER=true. status='stub_filled'
        (not 'filled') so this is never confused with a real broker fill in
        logs or downstream status handling, even though agents/execution.py
        treats it the same way for sizing/backtest purposes."""
        logger.info("STUB FILL: %s %s x%s", plan.side.value, plan.ticker, plan.quantity)
        return {
            "order_id": None,
            "status": "stub_filled",
            "filled_price": plan.entry_price or 0.0,
            "filled_qty": plan.quantity,
            "filled_at": datetime.now(timezone.utc),
        }
