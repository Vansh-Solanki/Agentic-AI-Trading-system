from core.config import TradingMode, LLM_TIMEOUT_SECONDS
from core.constants import SignalType, SignalSource, Timeframe
from core.models import AgentOutput, Signal, TradePlan
from agents.base_agent import BaseAgent
from llm.client import query_llm
from llm.prompts.spike_analysis import build_critic_prompt


class CriticAgent(BaseAgent):
    """Contrarian agent that stress-tests the proposed trade plan via LLM reasoning."""

    @property
    def name(self) -> str:
        return "CriticAgent"

    def run(self, ticker: str, **kwargs) -> AgentOutput:
        plan: TradePlan | None = kwargs.get("plan")
        supporting_summary: str = kwargs.get("supporting_summary", "")

        if plan is None:
            return AgentOutput(agent_name=self.name, ticker=ticker, signals=[],
                               summary="No plan to critique.")

        prompt = build_critic_prompt(ticker, plan, supporting_summary)
        _fail_open_response = '{"approved": true, "confidence": 0.5, "reasoning": "LLM unavailable — critic skipped."}'
        try:
            # Session 39, item 67 mitigation: bounds how long this thread can
            # hold llm/client.py's _ollama_semaphore slot after the caller
            # (Phase 4's future.result timeout) has already given up on it —
            # future.result() timing out does not kill this thread.
            response = query_llm(prompt, timeout=LLM_TIMEOUT_SECONDS)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("[%s] Critic LLM call failed (%s) — defaulting to approved.", ticker, exc)
            response = _fail_open_response

        if response is None:
            # query_llm() returns None on failure (Session 39+) instead of
            # raising or returning a sentinel string — the try/except above
            # only catches raised exceptions, so this path is not dead code.
            import logging
            logging.getLogger(__name__).warning("[%s] Critic LLM call returned None — defaulting to approved.", ticker)
            response = _fail_open_response

        approved, confidence, reasoning, biggest_risk, failure_scenario = self._parse_response(response)
        stype = SignalType.BUY if approved else SignalType.STRONG_SELL

        signal = Signal(
            ticker=ticker,
            source=SignalSource.CRITIC,
            signal_type=stype,
            timeframe=Timeframe.H1 if self.mode == TradingMode.INTRADAY else Timeframe.D1,
            confidence=confidence,
            reasoning=f"Critic: {reasoning[:200]}",
        )

        summary = f"Critic {'APPROVED' if approved else 'REJECTED'} the trade. {reasoning[:120]}"
        output = AgentOutput(
            agent_name=self.name,
            ticker=ticker,
            signals=[signal],
            summary=summary,
            raw_data={
                "llm_response": response,
                "approved": approved,
                "biggest_risk": biggest_risk,
                "failure_scenario": failure_scenario,
                "confidence": confidence,
                "reasoning": reasoning,
            },
        )
        self._log_output(output)
        return output

    def _parse_response(self, response: str) -> tuple[bool, float, str, str | None, str | None]:
        import json, re
        text = response.strip()
        # Strip markdown code fences if LLM wraps output in ```json ... ```
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE).strip()
        # Isolate first JSON object even if surrounded by prose
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
                approved = bool(data.get("approved", False))
                confidence = float(data.get("confidence", 0.75 if approved else 0.7))
                reasoning = str(data.get("reasoning", text))
                biggest_risk = data.get("biggest_risk") or None
                failure_scenario = data.get("failure_scenario") or None
                return approved, confidence, reasoning, biggest_risk, failure_scenario
            except (json.JSONDecodeError, ValueError):
                pass

        # Targeted regex recovery: malformed JSON (e.g. a missing quote) still
        # usually has an intact "approved": true/false key — recover that
        # directly rather than falling through to the blind keyword scan,
        # which would misfire on the literal key name itself (see below).
        approved_match = re.search(r'"approved"\s*:\s*(true|false)', text, re.IGNORECASE)
        if approved_match:
            approved = approved_match.group(1).lower() == "true"
            confidence = 0.75 if approved else 0.7
            return approved, confidence, text, None, None

        # Last-resort fallback keyword scan — keeps agent functional if the
        # LLM ignores the schema entirely. The literal JSON key name
        # "approved" contains "approve" as a substring, so it must be
        # stripped first or this always returns True whenever the raw text
        # still contains that key (regardless of its actual true/false value).
        stripped = re.sub(r'"approved"\s*:\s*\S+', "", text)
        lower = stripped.lower()
        approved = "approve" in lower or "proceed" in lower
        confidence = 0.75 if approved else 0.7
        return approved, confidence, text, None, None
