def build_monthly_bias_prompt(
    ticker: str,
    fundamental_summary: str,
    news_summary: str,
    quarterly_thesis: str | None = None,
) -> str:
    quarterly_section = (
        f"\nQuarterly earnings/analyst thesis:\n{quarterly_thesis}\n"
        if quarterly_thesis
        else ""
    )
    return f"""You are a fundamental equity analyst evaluating {ticker} for the coming month.

Fundamental snapshot:
{fundamental_summary}

Recent news themes:
{news_summary}{quarterly_section}
Respond with ONLY valid JSON — no prose, no markdown, no code fences. Schema:
{{
  "stance": "bullish" | "bearish" | "neutral",
  "valuation": "overvalued" | "fair" | "undervalued",
  "position_size": "overweight" | "neutral" | "underweight",
  "risk": "one sentence — primary risk that would invalidate the thesis"
}}

Be precise. Do not hedge every field."""
