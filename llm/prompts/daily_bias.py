def build_daily_bias_prompt(ticker: str, news_text: str) -> str:
    return f"""You are a professional equity analyst. Based on the following pre-market news headlines for {ticker},
provide a brief directional bias for today's trading session.

News headlines:
{news_text}

Respond in 2-3 sentences covering:
1. The overall sentiment (bullish / bearish / neutral)
2. The key catalyst driving the bias
3. Key risk to the thesis

Be concise and direct. Do not repeat the headlines."""
