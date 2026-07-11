def build_quarterly_analysis_prompt(ticker: str, earnings_headlines: str, finbert_avg: float) -> str:
    return f"""You are a buy-side analyst writing a quarterly fundamental review for {ticker}.

Earnings, guidance, and analyst rating headlines from the past 90 days:
{earnings_headlines}

FinBERT average sentiment score: {finbert_avg:.3f} (range -1 bearish to +1 bullish)

Write a 3-4 sentence quarterly thesis covering:
1. Earnings trend and guidance trajectory (beat/miss/in-line, raised/lowered/maintained)
2. Analyst sentiment shift (upgrades, downgrades, PT changes)
3. Institutional tone: accumulation, distribution, or neutral
4. Net quarterly stance: bullish / bearish / neutral with one-line rationale

Be specific. Do not hedge every statement."""
