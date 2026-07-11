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
Provide a monthly investment thesis in 4-5 sentences:
1. Fundamental valuation stance (overvalued / fair / undervalued)
2. Key growth or risk drivers for the next 30 days
3. Catalyst calendar (earnings, product releases, macro events)
4. Suggested position size relative to a standard allocation (overweight / neutral / underweight)
5. Primary risk that would invalidate the thesis

Be precise. Do not hedge every statement."""
