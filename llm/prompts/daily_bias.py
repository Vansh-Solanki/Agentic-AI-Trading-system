def build_daily_bias_prompt(ticker: str, news_text: str) -> str:
    return f"""You are a professional equity analyst. Based on the following pre-market news headlines for {ticker},
provide a brief directional bias for today's trading session.

News headlines:
{news_text}

Respond with ONLY valid JSON — no prose, no markdown, no code fences. Schema:
{{
  "stance": "bullish" | "bearish" | "neutral",
  "catalyst": "one sentence — the key driver of the bias",
  "risk": "one sentence — the key risk to the thesis"
}}

Be concise and direct. Do not repeat the headlines."""
