from core.config import TradingMode
from core.constants import Timeframe, SignalType, SignalSource, INTRADAY_TIMEFRAMES, LONGTERM_TIMEFRAMES
from core.models import AgentOutput, Signal
from agents.base_agent import BaseAgent
from data.cache.market_data_cache import get_candles
from signals.timeframe_aggregator import aggregate_signals


class TechnicalAgent(BaseAgent):
    @property
    def name(self) -> str:
        return "TechnicalAgent"

    def run(self, ticker: str, **kwargs) -> AgentOutput:
        timeframes = INTRADAY_TIMEFRAMES if self.mode == TradingMode.INTRADAY else LONGTERM_TIMEFRAMES
        all_signals: list[Signal] = []

        for tf in timeframes:
            df = get_candles(ticker, tf)
            if df.empty or len(df) < 50:
                self.logger.warning("Insufficient candles for %s @ %s", ticker, tf.value)
                continue
            signals = aggregate_signals(ticker, df, tf)
            all_signals.extend(signals)

        summary = self._summarize(all_signals)
        output = AgentOutput(
            agent_name=self.name,
            ticker=ticker,
            signals=all_signals,
            summary=summary,
        )
        self._log_output(output)
        return output

    def _summarize(self, signals: list[Signal]) -> str:
        if not signals:
            return "No technical signals generated."
        buys  = sum(1 for s in signals if s.signal_type in (SignalType.BUY, SignalType.STRONG_BUY))
        sells = sum(1 for s in signals if s.signal_type in (SignalType.SELL, SignalType.STRONG_SELL))
        avg_conf = sum(s.confidence for s in signals) / len(signals)
        return f"{len(signals)} signals — {buys} bullish / {sells} bearish | avg confidence {avg_conf:.2f}"
