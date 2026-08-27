def build_indicator_verdict_prompt(ticker: str, timeframe: str, indicator: str, description: str) -> str:
    """
    One indicator's ambiguous reading, one verdict — not a paragraph, not a
    final trading decision. The response becomes exactly one vote in the
    same tally every other deterministic indicator already contributes to.
    """
    return f"""You are a technical analyst. For {ticker} on the {timeframe} timeframe, the {indicator} indicator has fallen into an ambiguous zone where the deterministic rule has no clear buy or sell reading.

{description}

Based on this reading alone, give a single directional bias for this one indicator — not a full trade decision.

Respond with exactly one word: BUY, SELL, or HOLD."""


def build_combined_indicator_verdict_prompt(
    ticker: str, timeframe: str, items: list[tuple[str, str]]
) -> str:
    """
    Session 47 item 11: one combined call for every indicator that fell into
    its ambiguous zone this cycle, replacing up to four separate single-
    indicator calls (build_indicator_verdict_prompt() above). Benchmarked at
    ~8s combined vs ~10.4s for four sequential calls once the model is warm.
    Uses a JSON schema with one named key per indicator rather than the
    free-text "one line per indicator" format that was also benchmarked,
    since that version did not reliably preserve indicator labels/vocabulary
    (model dropped labels entirely in one benchmark run) - same collision
    class Session 46 fixed for the news layers with parse_stance_json().

    `items`: (indicator_key, description) pairs, e.g. ("rsi", "RSI (14): ...").
    """
    lines = [f"- {key}: {description}" for key, description in items]
    body = "\n".join(lines)
    keys_schema = ",\n".join(f'  "{key}": "BUY" | "SELL" | "HOLD"' for key, _ in items)
    return f"""You are a technical analyst. For {ticker} on the {timeframe} timeframe, the following indicators have each fallen into an ambiguous zone where their own deterministic rule has no clear buy or sell reading:

{body}

For each indicator, give a single directional bias based on that indicator's own reading alone - not a full trade decision, and not influenced by the other indicators listed.

Respond with ONLY valid JSON - no prose, no markdown, no code fences. Schema:
{{
{keys_schema}
}}"""
