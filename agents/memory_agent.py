from datetime import datetime, timezone
from core.config import TradingMode
from core.constants import SignalType, SignalSource, Timeframe
from core.models import AgentOutput, Signal
from agents.base_agent import BaseAgent
from memory.stock_memory import load_memory, save_memory


class MemoryAgent(BaseAgent):
    """Reads per-stock memory and emits signals based on historical patterns and notes."""

    @property
    def name(self) -> str:
        return "MemoryAgent"

    def run(self, ticker: str, **kwargs) -> AgentOutput:
        mem = load_memory(ticker, self.mode)
        signals = self._signals_from_memory(ticker, mem)

        summary = f"Memory loaded — {len(mem)} keys | {len(signals)} memory-driven signals"
        output = AgentOutput(
            agent_name=self.name,
            ticker=ticker,
            signals=signals,
            summary=summary,
            raw_data=mem,
        )
        self._log_output(output)
        return output

    def update(self, ticker: str, outcome: dict) -> None:
        """
        Two-tier write triggered at trade close (once pnl is known).

        outcome keys (all optional except pnl / closed_at for history appends):
          directional_bias  — new bias string ("bullish" | "bearish" | "neutral")
          bias_confidence   — float 0–1
          key_levels        — {"support": float, "resistance": float}
          avoid_until       — ISO timestamp str or None
          reasoning         — string carried into history entry
          pnl               — float, realized PnL of the closing trade
          closed_at         — ISO timestamp str of trade close
        """
        mem = load_memory(ticker, self.mode)
        prev_bias = mem.get("directional_bias")
        prev_confidence = mem.get("bias_confidence", 0.5)  # capture before Tier 1 overwrites it

        # Tier 1 — overwrite current-state fields in place
        new_bias = outcome.get("directional_bias", prev_bias)
        new_confidence = outcome.get("bias_confidence", prev_confidence)

        mem["directional_bias"] = new_bias
        mem["bias_confidence"] = new_confidence
        if "key_levels" in outcome:
            mem["key_levels"] = outcome["key_levels"]
        mem["avoid_until"] = outcome.get("avoid_until", mem.get("avoid_until"))
        mem["last_updated"] = datetime.now(timezone.utc).isoformat()

        # Tier 2 — append to bias_history only when bias changes and pnl is known
        pnl = outcome.get("pnl")
        if prev_bias != new_bias and pnl is not None:
            entry = {
                "bias": prev_bias,
                "confidence": prev_confidence,  # previous bias's confidence, not the new one
                "reasoning": outcome.get("reasoning", ""),
                "pnl": pnl,
                "closed_at": outcome.get("closed_at", datetime.now(timezone.utc).isoformat()),
            }
            mem.setdefault("bias_history", []).append(entry)

        save_memory(ticker, self.mode, mem)

    def _signals_from_memory(self, ticker: str, mem: dict) -> list[Signal]:
        signals: list[Signal] = []

        bias = mem.get("directional_bias")
        if bias in ("bullish", "bearish"):
            stype = SignalType.BUY if bias == "bullish" else SignalType.SELL
            signals.append(Signal(
                ticker=ticker,
                source=SignalSource.MEMORY,
                signal_type=stype,
                timeframe=Timeframe.D1,
                confidence=float(mem.get("bias_confidence", 0.5)),
                reasoning=f"Historical memory bias: {bias}",
            ))

        if mem.get("avoid_until"):
            signals.append(Signal(
                ticker=ticker,
                source=SignalSource.MEMORY,
                signal_type=SignalType.STRONG_SELL,
                timeframe=Timeframe.D1,
                confidence=0.9,
                reasoning=f"Memory flag: avoid until {mem['avoid_until']}",
            ))

        return signals
