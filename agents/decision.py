import threading
import time
from core.config import TradingMode
from core.constants import SignalType, SignalSource, Timeframe, RegimeType
from core.models import AgentOutput, Signal
from agents.base_agent import BaseAgent


# Weight per signal source — intraday vs long-term profiles
_INTRADAY_WEIGHTS = {
    SignalSource.TECHNICAL:   0.35,
    SignalSource.NEWS:        0.25,
    SignalSource.REGIME:      0.10,
    SignalSource.MEMORY:      0.10,
    SignalSource.FUNDAMENTAL: 0.20,
}

_LONGTERM_WEIGHTS = {
    SignalSource.TECHNICAL:   0.20,
    SignalSource.NEWS:        0.15,
    SignalSource.REGIME:      0.15,
    SignalSource.MEMORY:      0.10,
    SignalSource.FUNDAMENTAL: 0.40,
}

_SIGNAL_SCORES = {
    SignalType.STRONG_BUY:  1.0,
    SignalType.BUY:         0.5,
    SignalType.NEUTRAL:     0.0,
    SignalType.SELL:       -0.5,
    SignalType.STRONG_SELL:-1.0,
}

_weight_cache: dict = {}  # {mode_str: (monotonic_ts, weights_dict)}
_weight_cache_lock = threading.Lock()
_WEIGHT_CACHE_TTL = 300   # 5 minutes


def _load_weights(mode_str: str) -> dict:
    """Load weights from DB agent_weights table; fall back to hardcoded defaults."""
    cached = _weight_cache.get(mode_str)
    if cached is not None and time.monotonic() - cached[0] < _WEIGHT_CACHE_TTL:
        return cached[1]
    try:
        from data.storage.decision_store import get_agent_weights
        db_weights_raw = get_agent_weights(mode_str)
        # Convert string keys from DB to SignalSource enum keys used by _weighted_score
        db_weights = {SignalSource(k): v for k, v in db_weights_raw.items()} if db_weights_raw else {}
    except Exception:
        db_weights = {}
    weights = db_weights if db_weights else (
        _INTRADAY_WEIGHTS if mode_str == "intraday" else _LONGTERM_WEIGHTS
    )
    with _weight_cache_lock:
        _weight_cache[mode_str] = (time.monotonic(), weights)
    return weights


class DecisionAgent(BaseAgent):
    """Aggregates all agent signals into a single directional decision."""

    @property
    def name(self) -> str:
        return "DecisionAgent"

    def run(self, ticker: str, **kwargs) -> AgentOutput:
        all_signals: list[Signal] = kwargs.get("signals", [])
        regime: str               = kwargs.get("regime", RegimeType.UNKNOWN.value)
        weights = _load_weights(self.mode.value)

        score = self._weighted_score(all_signals, weights)
        score = self._apply_regime_modifier(score, regime)
        decision_signal = self._score_to_signal(ticker, score, len(all_signals))

        summary = f"Composite score {score:.3f} → {decision_signal.signal_type.value} [regime={regime}]"
        output = AgentOutput(
            agent_name=self.name,
            ticker=ticker,
            signals=[decision_signal],
            summary=summary,
            raw_data={"composite_score": score, "input_signals": len(all_signals), "regime": regime},
        )
        self._log_output(output)
        return output

    def _weighted_score(self, signals: list[Signal], weights: dict) -> float:
        total_weight, weighted_sum = 0.0, 0.0
        for s in signals:
            w = weights.get(s.source, 0.1) * s.confidence
            weighted_sum += _SIGNAL_SCORES.get(s.signal_type, 0.0) * w
            total_weight  += w
        return weighted_sum / total_weight if total_weight else 0.0

    def _apply_regime_modifier(self, score: float, regime: str) -> float:
        """Scale the composite score based on the current market regime."""
        if regime == RegimeType.BEAR_TREND.value and score > 0:
            return score * 0.5    # halve bullish conviction in a bear market
        if regime == RegimeType.BULL_TREND.value and score < 0:
            return score * 0.5    # halve bearish conviction in a bull market
        if regime == RegimeType.HIGH_VOLATILITY.value:
            return score * 0.7    # reduce overall conviction when vol is elevated
        if regime == RegimeType.BREAKOUT.value and score > 0:
            return min(score * 1.15, 1.0)  # amplify bullish signals in breakout
        return score

    def _score_to_signal(self, ticker: str, score: float, n_inputs: int) -> Signal:
        if score > 0.5:
            stype, conf = SignalType.STRONG_BUY, min(score, 1.0)
        elif score > 0.15:
            stype, conf = SignalType.BUY, score
        elif score < -0.5:
            stype, conf = SignalType.STRONG_SELL, min(abs(score), 1.0)
        elif score < -0.15:
            stype, conf = SignalType.SELL, abs(score)
        else:
            stype, conf = SignalType.NEUTRAL, 0.1

        return Signal(
            ticker=ticker,
            source=SignalSource.TECHNICAL,
            signal_type=stype,
            timeframe=Timeframe.D1,
            confidence=conf,
            value=score,
            reasoning=f"Weighted composite of {n_inputs} signals. Score={score:.3f}",
        )
