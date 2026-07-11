from __future__ import annotations
from core.models import TradePlan, Signal


def build_structured_summary(regime: str, signals: list[Signal]) -> str:
    lines = [f"Regime: {regime}"]
    by_source: dict[str, list[Signal]] = {}
    for s in signals:
        by_source.setdefault(s.source.value, []).append(s)
    lines.append("\nSignals by source and timeframe:")
    for source, source_signals in sorted(by_source.items()):
        for s in source_signals:
            lines.append(
                f"- {source.capitalize()} [{s.timeframe.value}]: "
                f"{s.signal_type.value.upper()} {s.confidence:.2f}"
                + (f" | {s.reasoning[:80]}" if s.reasoning else "")
            )
    return "\n".join(lines)


def build_spike_prompt(
    ticker: str,
    headline: str,
    sentiment_score: float,
    market_event_desc: str,
) -> str:
    direction = "bullish" if sentiment_score > 0 else "bearish"
    return f"""You are a high-frequency trading analyst. An abnormal market event was detected for {ticker}.

Market trigger: {market_event_desc}
Headline: "{headline}"
FinBERT sentiment: {sentiment_score:.3f} ({direction})

Assess in 2 sentences:
1. Is this a sustained directional move or a brief spike likely to fade?
2. Recommended immediate action: BUY / SELL / HOLD — and why.

Start your response with exactly one of: BUY, SELL, or HOLD."""


def build_critic_prompt(ticker: str, plan: TradePlan, structured_summary: str) -> str:
    atr = plan.metadata.get("atr")
    atr_line = f"- ATR (14): {atr}" if atr is not None else ""
    stop_width = abs((plan.entry_price or 0) - plan.stop_loss)
    atr_rule = (
        f"- Set approved=false if stop width ({stop_width:.2f}) is wider than 3x ATR ({3 * atr:.2f})."
        if atr is not None
        else "- Set approved=false if the stop appears wider than typical volatility for this ticker."
    )
    return f"""You are a contrarian trading critic. Your job is to stress-test the following trade plan for {ticker} and return a structured JSON verdict.

Proposed trade:
- Side: {plan.side.value.upper()}
- Entry: {plan.entry_price}
- Stop loss: {plan.stop_loss}
- Take profit: {plan.take_profit}
- Risk/reward: {round((plan.take_profit - (plan.entry_price or 0)) / max((plan.entry_price or 0) - plan.stop_loss, 0.01), 2)}
{atr_line}
- Confidence: {plan.confidence:.2f}
- Reasoning: {plan.reasoning}

Supporting signals:
{structured_summary}

Respond with ONLY valid JSON — no prose, no markdown, no code fences. Schema:
{{
  "approved": true or false,
  "confidence": 0.0 to 1.0,
  "biggest_risk": "one sentence",
  "failure_scenario": "one sentence",
  "reasoning": "one to two sentence justification for your verdict"
}}

Rules:
- Set approved=false if risk/reward < 1.5.
{atr_rule}
- Set approved=false if signals conflict across timeframes.
- Set approved=true only if the setup is clean, risk is defined, and the bias is consistent.
- confidence reflects how certain you are in your verdict, not how good the trade is."""
