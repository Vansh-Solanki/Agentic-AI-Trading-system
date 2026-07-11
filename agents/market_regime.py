import numpy as np
import pandas as pd
from core.config import TradingMode
from core.constants import SignalType, SignalSource, Timeframe, RegimeType
from core.models import AgentOutput, Signal
from agents.base_agent import BaseAgent
from data.cache.market_data_cache import get_candles


class MarketRegimeAgent(BaseAgent):
    """Classifies the current market regime using VIX + SPY trend + volatility."""

    @property
    def name(self) -> str:
        return "MarketRegimeAgent"

    def run(self, ticker: str, **kwargs) -> AgentOutput:
        spy_df = get_candles("SPY", Timeframe.D1)
        vix_df = get_candles("^VIX", Timeframe.D1)

        regime = self._classify(spy_df, vix_df)
        signal = self._regime_to_signal(ticker, regime)

        output = AgentOutput(
            agent_name=self.name,
            ticker=ticker,
            signals=[signal],
            summary=f"Market regime: {regime.value}",
            raw_data={"regime": regime.value},
        )
        self._log_output(output)
        return output

    def _classify(self, spy: pd.DataFrame, vix: pd.DataFrame) -> RegimeType:
        if spy.empty or len(spy) < 50:
            return RegimeType.UNKNOWN

        close = spy["Close"].squeeze()
        ma50  = close.rolling(50).mean().iloc[-1]
        ma200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else ma50
        current = float(close.iloc[-1])

        vix_level = float(vix["Close"].iloc[-1]) if not vix.empty else 20.0
        returns   = close.pct_change().dropna().tail(20)
        realized_vol = float(returns.std() * np.sqrt(252))

        # High volatility takes priority — a breakout in a volatile tape is still risky
        if vix_level > 30 or realized_vol > 0.30:
            return RegimeType.HIGH_VOLATILITY

        # Breakout: today's close exceeds the highest close of the prior 20 sessions
        # Checked before BULL_TREND so fresh breaks are classified as BREAKOUT, not folded
        # silently into an ongoing trend — this is what fires the 1.15× Decision Agent amplifier
        if len(close) >= 21:
            prior_20_high = float(close.iloc[-21:-1].max())
            if current > prior_20_high:
                return RegimeType.BREAKOUT

        if current > ma50 > ma200:
            return RegimeType.BULL_TREND
        if current < ma50 < ma200:
            return RegimeType.BEAR_TREND

        # Low volatility: calm, non-trending market (VIX suppressed + realized vol quiet)
        if vix_level < 15 and realized_vol < 0.12:
            return RegimeType.LOW_VOLATILITY

        if abs(current - ma50) / ma50 < 0.02:
            return RegimeType.RANGING

        return RegimeType.UNKNOWN

    def _regime_to_signal(self, ticker: str, regime: RegimeType) -> Signal:
        mapping = {
            RegimeType.BULL_TREND:      (SignalType.BUY,         0.7),
            RegimeType.BEAR_TREND:      (SignalType.SELL,        0.7),
            RegimeType.HIGH_VOLATILITY: (SignalType.NEUTRAL,     0.5),
            RegimeType.LOW_VOLATILITY:  (SignalType.BUY,         0.4),
            RegimeType.RANGING:         (SignalType.NEUTRAL,     0.4),
            RegimeType.BREAKOUT:        (SignalType.STRONG_BUY,  0.6),
            RegimeType.UNKNOWN:         (SignalType.NEUTRAL,     0.1),
        }
        stype, conf = mapping[regime]
        return Signal(
            ticker=ticker,
            source=SignalSource.REGIME,
            signal_type=stype,
            timeframe=Timeframe.D1,
            confidence=conf,
            reasoning=f"Market regime classified as {regime.value}",
            metadata={"regime": regime.value},
        )
