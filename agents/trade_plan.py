import math
from core.config import TradingMode
from core.constants import SignalType, OrderSide, OrderType, RegimeType, Timeframe
from core.models import AgentOutput, Signal, TradePlan
from agents.base_agent import BaseAgent
from data.cache.market_data_cache import get_candles
from signals.indicators.volatility import calculate_atr
from risk.position_sizing import kelly_position_size, get_historical_performance
import core.config as cfg


class TradePlanAgent(BaseAgent):
    """Converts a decision signal into a concrete, risk-sized trade plan."""

    @property
    def name(self) -> str:
        return "TradePlanAgent"

    def run(self, ticker: str, **kwargs) -> AgentOutput:
        decision: Signal | None = kwargs.get("decision_signal")
        portfolio_value: float  = kwargs.get("portfolio_value", 100_000.0)
        regime: str             = kwargs.get("regime", RegimeType.UNKNOWN.value)

        if decision is None or decision.signal_type == SignalType.NEUTRAL:
            return AgentOutput(agent_name=self.name, ticker=ticker, signals=[],
                               summary="No actionable decision — no trade plan generated")

        tf = Timeframe.M15 if self.mode == TradingMode.INTRADAY else Timeframe.D1
        df = get_candles(ticker, tf)
        if df.empty:
            return AgentOutput(agent_name=self.name, ticker=ticker, signals=[],
                               summary="No price data — cannot generate trade plan")
        atr = calculate_atr(df)
        if atr is None:
            return AgentOutput(agent_name=self.name, ticker=ticker, signals=[],
                               summary="ATR unavailable (insufficient data) — cannot size trade plan")

        current_price = float(df["Close"].iloc[-1])
        if current_price != current_price:  # NaN check (today's bar not finalized yet)
            if len(df) < 2:
                return AgentOutput(agent_name=self.name, ticker=ticker, signals=[],
                                   summary="No valid close price — cannot generate trade plan")
            current_price = float(df["Close"].iloc[-2])
            if current_price != current_price:  # t-1 also NaN — genuine data problem, stop here
                return AgentOutput(agent_name=self.name, ticker=ticker, signals=[],
                                   summary="No valid close price (t and t-1 both NaN) — cannot generate trade plan")

        side = OrderSide.BUY if decision.signal_type in (SignalType.BUY, SignalType.STRONG_BUY) else OrderSide.SELL
        if self.mode == TradingMode.INTRADAY:
            sl_mult = cfg.STOP_LOSS_ATR_MULT_INTRADAY
            tp_mult = cfg.TAKE_PROFIT_ATR_MULT_INTRADAY
        else:
            sl_mult = cfg.STOP_LOSS_ATR_MULT_LONGTERM
            tp_mult = cfg.TAKE_PROFIT_ATR_MULT_LONGTERM
        stop_dist   = atr * sl_mult
        target_dist = atr * tp_mult

        if side == OrderSide.BUY:
            stop_loss   = current_price - stop_dist
            take_profit = current_price + target_dist
        else:
            stop_loss   = current_price + stop_dist
            take_profit = current_price - target_dist

        risk_per_share = abs(current_price - stop_loss)
        risk_amount    = portfolio_value * cfg.MAX_PORTFOLIO_RISK_PCT

        # Reduce position sizing in high-volatility regime
        size_mult = 0.5 if regime == RegimeType.HIGH_VOLATILITY.value else 1.0
        atr_quantity = round(risk_amount / risk_per_share * size_mult) if risk_per_share > 0 else 0

        # Kelly sizing: use when we have enough closed-trade history, otherwise fall back to ATR sizing
        win_rate, avg_win, avg_loss = get_historical_performance(ticker)
        sizing_note = "ATR"
        if win_rate > 0 and avg_win > 0 and avg_loss > 0:
            kelly_qty = round(kelly_position_size(portfolio_value, current_price, win_rate, avg_win, avg_loss) * size_mult)
            quantity = min(atr_quantity, kelly_qty) if kelly_qty > 0 else atr_quantity
            sizing_note = f"Kelly(wr={win_rate:.0%})"
        else:
            quantity = atr_quantity

        # Bug 2 fix: neither the ATR sizing above nor the Kelly min ever
        # applied cfg.MAX_POSITION_SIZE_PCT — RiskManagerAgent.validate_plan()
        # resizes later, but plans/premarket output showed grossly oversized
        # quantities in the meantime (e.g. AAPL intraday 1603 shares, ~$535k,
        # against a 5% cap of ~15 shares on a $100,628.91 portfolio). Applied
        # here, after the final quantity (ATR sizing + Kelly min) is known,
        # so the cap is the last word regardless of which sizing path won.
        max_shares = math.floor(portfolio_value * cfg.MAX_POSITION_SIZE_PCT / current_price) if current_price > 0 else 0
        if max_shares <= 0:
            return AgentOutput(
                agent_name=self.name, ticker=ticker, signals=[],
                summary=(
                    f"Position cap is 0 shares at {current_price:.2f} — too expensive for the "
                    f"{cfg.MAX_POSITION_SIZE_PCT:.0%} cap on a ${portfolio_value:,.0f} portfolio — "
                    f"cannot generate trade plan"
                ),
            )
        cap_applied = quantity > max_shares
        quantity = min(quantity, max_shares)

        regime_note = f" | size×{size_mult:.1f} (regime={regime})" if size_mult != 1.0 else f" | regime={regime}"
        cap_note = f" | capped to {cfg.MAX_POSITION_SIZE_PCT:.0%} position cap ({max_shares} sh)" if cap_applied else ""
        plan = TradePlan(
            ticker=ticker,
            side=side,
            order_type=OrderType.LIMIT,
            quantity=quantity,
            entry_price=current_price,
            stop_loss=round(stop_loss, 2),
            take_profit=round(take_profit, 2),
            confidence=decision.confidence,
            reasoning=f"{sizing_note} plan. ATR={atr:.2f}, SL×{sl_mult} TP×{tp_mult}, R:R={target_dist/stop_dist:.1f}:1{regime_note}{cap_note}",
            strategy_mode=self.mode.value,
            metadata={"atr": round(atr, 4)},
        )

        output = AgentOutput(
            agent_name=self.name,
            ticker=ticker,
            signals=[],
            summary=f"{side.value.upper()} {quantity} {ticker} @ {current_price:.2f} | SL {stop_loss:.2f} | TP {take_profit:.2f}",
            raw_data={"plan": plan},
        )
        self._log_output(output)
        return output
