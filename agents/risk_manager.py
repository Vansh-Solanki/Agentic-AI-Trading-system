from core.config import TradingMode
from core.constants import SignalType, SignalSource, Timeframe
from core.models import AgentOutput, Signal, TradePlan
from agents.base_agent import BaseAgent
from risk.position_sizing import calculate_position_size
from risk.portfolio_exposure import check_exposure_limits, check_var_limit
from risk.circuit_breakers import check_circuit_breakers


class RiskManagerAgent(BaseAgent):
    """Validates trade plans against risk rules and portfolio exposure limits."""

    @property
    def name(self) -> str:
        return "RiskManagerAgent"

    def run(self, ticker: str, **kwargs) -> AgentOutput:
        # Called without a plan — surface portfolio-level risk status for dashboard/orchestrator
        portfolio_value: float = kwargs.get("portfolio_value", 100_000.0)
        cb_triggered, cb_reason = check_circuit_breakers()
        signals: list[Signal] = []
        status_parts: list[str] = []

        if cb_triggered:
            signals.append(Signal(
                ticker=ticker,
                source=SignalSource.RISK,
                signal_type=SignalType.STRONG_SELL,
                timeframe=Timeframe.D1,
                confidence=1.0,
                reasoning=f"Circuit breaker: {cb_reason}",
            ))
            status_parts.append(f"Circuit breaker ACTIVE: {cb_reason}")
        else:
            status_parts.append("Circuit breakers nominal")

        var_ok, var_reason = check_var_limit(portfolio_value)
        if not var_ok:
            signals.append(Signal(
                ticker=ticker,
                source=SignalSource.RISK,
                signal_type=SignalType.STRONG_SELL,
                timeframe=Timeframe.D1,
                confidence=0.9,
                reasoning=f"VaR limit breached: {var_reason}",
            ))
        status_parts.append(f"VaR: {var_reason}")

        from data.storage.decision_store import get_open_trades
        import core.config as _cfg
        open_count = len(get_open_trades())
        status_parts.append(f"Exposure: {open_count}/{_cfg.MAX_CONCURRENT_TRADES} trades open")

        summary = " | ".join(status_parts)
        output = AgentOutput(
            agent_name=self.name,
            ticker=ticker,
            signals=signals,
            summary=summary,
            raw_data={"var_ok": var_ok, "var_reason": var_reason, "open_trades": open_count},
        )
        self._log_output(output)
        return output

    def validate_plan(self, plan: TradePlan, portfolio_value: float) -> tuple[bool, str]:
        """Returns (approved, reason). May resize plan.quantity in-place to fit within limits."""
        cb_triggered, cb_reason = check_circuit_breakers()
        if cb_triggered:
            return False, f"Circuit breaker active: {cb_reason}"

        # Resize before the VaR gate: the quantity trade_plan.py hands us is
        # sized purely off the 2% risk budget / stop distance, with no
        # awareness of the 5% position cap. For any realistic stop (well
        # under 40% of price) that quantity is already oversized relative to
        # the cap, so checking VaR against it first means VaR is evaluating
        # a size the plan was never going to be allowed to hold. Capping
        # first means VaR sees the same quantity the plan will actually open.
        max_qty = calculate_position_size(
            portfolio_value=portfolio_value,
            entry_price=plan.entry_price or 0,
            stop_loss=plan.stop_loss,
        )
        if plan.quantity > max_qty * 1.05:
            plan.quantity = round(max_qty)

        if plan.quantity <= 0:
            return False, "Position size is zero after applying the position cap"

        var_ok, var_reason = check_var_limit(
            portfolio_value,
            candidate_ticker=plan.ticker,
            candidate_qty=plan.quantity,
            candidate_price=plan.entry_price or 0,
        )
        if not var_ok:
            return False, var_reason

        allowed, reason = check_exposure_limits(
            plan.ticker, plan.quantity, plan.entry_price or 0, plan.strategy_mode
        )
        if not allowed:
            return False, reason

        return True, "Risk checks passed"
