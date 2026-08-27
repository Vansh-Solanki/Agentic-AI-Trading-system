from core.config import TradingMode
from core.constants import (
    Timeframe, SignalType, SignalSource, INTRADAY_TIMEFRAMES, LONGTERM_TIMEFRAMES,
    INTRADAY_LLM_CANONICAL_TIMEFRAME, LONGTERM_LLM_CANONICAL_TIMEFRAME,
)
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
        canonical_tf = (
            INTRADAY_LLM_CANONICAL_TIMEFRAME if self.mode == TradingMode.INTRADAY
            else LONGTERM_LLM_CANONICAL_TIMEFRAME
        )
        # Canonical timeframe first (Session 45, locked Option B): it's the
        # only timeframe allowed to actually call the LLM for an ambiguous
        # RSI/Stoch RSI/Bollinger/ADX reading. The other timeframes in this
        # mode inherit its verdicts per indicator via llm_verdicts instead of
        # triggering their own calls, bounding total LLM call volume.
        ordered_timeframes = [canonical_tf] + [tf for tf in timeframes if tf != canonical_tf]
        llm_verdicts: dict[str, str] = {}
        all_signals: list[Signal] = []

        for tf in ordered_timeframes:
            df = get_candles(ticker, tf)
            if df.empty or len(df) < 50:
                self.logger.warning("Insufficient candles for %s @ %s", ticker, tf.value)
                continue
            is_canonical = tf == canonical_tf
            signals = aggregate_signals(
                ticker, df, tf,
                enable_llm_escalation=True,
                is_canonical=is_canonical,
                inherited_llm_verdicts=llm_verdicts if not is_canonical else None,
                llm_verdicts_out=llm_verdicts if is_canonical else None,
            )
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
