def build_weekly_bias_prompt(
    ticker: str,
    news_summary: str,
    technical_summary: str,
    quarterly_thesis: str | None = None,
) -> str:
    quarterly_section = (
        f"\nQuarterly earnings/analyst context:\n{quarterly_thesis}\n"
        if quarterly_thesis
        else ""
    )
    return f"""You are a senior portfolio manager preparing a weekly outlook for {ticker}.

Recent news context:
{news_summary}

Technical posture:
{technical_summary}{quarterly_section}
Respond with ONLY valid JSON — no prose, no markdown, no code fences. Schema:
{{
  "stance": "bullish" | "bearish" | "neutral",
  "technical_structure": "one sentence — trend, key levels",
  "recommended_action": "accumulate" | "hold" | "reduce" | "avoid",
  "risk": "one sentence — top downside risk this week"
}}

Be direct and avoid restating the input data."""
