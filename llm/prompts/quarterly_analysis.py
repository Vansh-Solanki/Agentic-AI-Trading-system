def build_quarterly_analysis_prompt(ticker: str, earnings_headlines: str, finbert_avg: float) -> str:
    return f"""You are a buy-side analyst writing a quarterly fundamental review for {ticker}.

Earnings, guidance, and analyst rating headlines from the past 90 days:
{earnings_headlines}

FinBERT average sentiment score: {finbert_avg:.3f} (range -1 bearish to +1 bullish)

Respond with ONLY valid JSON — no prose, no markdown, no code fences. Schema:
{{
  "stance": "bullish" | "bearish" | "neutral",
  "earnings_trend": "one sentence — beat/miss/in-line, raised/lowered/maintained guidance",
  "analyst_sentiment": "one sentence — upgrades, downgrades, PT changes",
  "institutional_tone": "accumulation" | "distribution" | "neutral",
  "reasoning": "one sentence — net quarterly rationale"
}}

Be specific. Do not hedge every field."""
