import logging
from datetime import datetime, timezone
from core.config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL, PAPER_TRADING
from core.models import TradePlan

logger = logging.getLogger(__name__)


class AlpacaBroker:
    def __init__(self):
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, StopOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus
            self.client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_TRADING)
            self._MarketOrderRequest = MarketOrderRequest
            self._LimitOrderRequest  = LimitOrderRequest
            self._StopOrderRequest   = StopOrderRequest
            self._OrderSide          = OrderSide
            self._TimeInForce        = TimeInForce
            self._OrderStatus        = OrderStatus
            self._available          = True
            logger.info("Alpaca broker initialized (paper=%s)", PAPER_TRADING)
        except ImportError:
            logger.warning("alpaca-py not installed — broker in stub mode")
            self._available = False
        except Exception as exc:
            logger.warning("Alpaca broker init failed (%s) — broker in stub mode", exc)
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
        return {"filled_price": float(order.filled_avg_price or plan.entry_price), "filled_at": datetime.now(timezone.utc)}

    def submit_limit_order(self, plan: TradePlan, client_order_id: str | None = None) -> dict:
        if not self._available:
            return self._stub_fill(plan)
        kw = {"client_order_id": client_order_id} if client_order_id else {}
        req = self._LimitOrderRequest(
            symbol=plan.ticker,
            qty=plan.quantity,
            side=self._OrderSide.BUY if plan.side.value == "buy" else self._OrderSide.SELL,
            time_in_force=self._TimeInForce.GTC,
            # Alpaca rejects sub-penny limit prices (e.g. 322.17999267578125,
            # a float precision artifact from upstream candle data) with
            # "invalid limit_price ... sub-penny increment does not fulfill
            # minimum pricing criteria." Round at this final point, right
            # before the value leaves the app — nothing downstream adjusts it
            # further.
            limit_price=round(plan.entry_price, 2),
            **kw,
        )
        order = self.client.submit_order(req)
        return {"filled_price": float(order.filled_avg_price or plan.entry_price), "filled_at": datetime.now(timezone.utc)}

    def submit_stop_order(self, plan: TradePlan, client_order_id: str | None = None) -> dict:
        if not self._available:
            return self._stub_fill(plan)
        kw = {"client_order_id": client_order_id} if client_order_id else {}
        req = self._StopOrderRequest(
            symbol=plan.ticker,
            qty=plan.quantity,
            side=self._OrderSide.BUY if plan.side.value == "buy" else self._OrderSide.SELL,
            time_in_force=self._TimeInForce.GTC,
            # Same sub-penny rejection risk as submit_limit_order() above.
            stop_price=round(plan.entry_price, 2),
            **kw,
        )
        order = self.client.submit_order(req)
        return {"filled_price": float(order.filled_avg_price or plan.entry_price), "filled_at": datetime.now(timezone.utc)}

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
        if not self._available:
            return {"portfolio_value": 100000.0, "cash": 100000.0}
        acct = self.client.get_account()
        return {"portfolio_value": float(acct.portfolio_value), "cash": float(acct.cash)}

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
        logger.info("STUB FILL: %s %s x%s", plan.side.value, plan.ticker, plan.quantity)
        return {"filled_price": plan.entry_price or 0.0, "filled_at": datetime.now(timezone.utc)}
