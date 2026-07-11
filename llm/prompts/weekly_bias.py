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
Provide a weekly directional bias in 3-4 sentences:
1. Macro/sector tailwinds or headwinds
2. Technical structure (trend, key levels)
3. Recommended stance (accumulate / hold / reduce / avoid) with reasoning
4. Top downside risk this week

Be direct and avoid restating the input data."""
