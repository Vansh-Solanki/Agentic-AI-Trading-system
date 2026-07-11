import logging
from core.models import NewsItem, Signal
from core.constants import SignalType, SignalSource, Timeframe
from data.feeds.news_feed import fetch_news
from data.storage.news_store import save_news, get_recent_news
from data.storage.stock_memory_store import upsert_memory
from news.finbert import score_batch
from llm.client import query_llm
from llm.prompts.quarterly_analysis import build_quarterly_analysis_prompt

logger = logging.getLogger(__name__)

_EARNINGS_KEYWORDS = {
    "earnings", "eps", "revenue", "guidance", "outlook", "analyst",
    "upgrade", "downgrade", "price target", "rating", "beat", "miss",
    "quarter", "q1", "q2", "q3", "q4", "institutional", "accumulation",
    "distribution", "raises", "lowers", "maintains",
}


def _is_earnings_relevant(item: NewsItem) -> bool:
    text = (item.headline + " " + item.summary).lower()
    return any(kw in text for kw in _EARNINGS_KEYWORDS)


def run_quarterly_analysis(ticker: str) -> list[Signal]:
    """
    Layer 1 — Quarterly earnings/analyst narrative.
    Fetches 90-day news, filters for earnings/analyst content, scores with FinBERT,
    escalates to LLM for a quarterly thesis, and stores the thesis in stock_memory
    so Layer 4 (weekly) and Layer 5 (monthly) can incorporate it.
    Only called in LONG_TERM mode.
    """
    raw_items = fetch_news([ticker], hours_back=90 * 24, max_articles=50)
    for item in raw_items:
        item.layer = 1

    # Filter to earnings/analyst relevant articles
    items = [i for i in raw_items if _is_earnings_relevant(i)]
    if not items:
        # Fall back to all fetched items rather than returning nothing
        items = raw_items

    headlines = [i.headline + " " + i.summary for i in items]
    scores    = score_batch(headlines)
    for item, score in zip(items, scores):
        item.sentiment_score = score

    save_news(items)

    if not scores:
        return []

    avg_score = sum(scores) / len(scores)

    # Build earnings headlines text for LLM
    sorted_items  = sorted(items, key=lambda i: abs(i.sentiment_score or 0), reverse=True)[:10]
    earnings_text = "\n".join(f"- {i.headline}" for i in sorted_items) or "No earnings/analyst headlines found."

    prompt       = build_quarterly_analysis_prompt(ticker, earnings_text, avg_score)
    llm_response = query_llm(prompt, max_tokens=300)

    # Persist quarterly thesis so Layer 4/5 can pull it into their prompts
    upsert_memory(ticker, "long_term", "quarterly_thesis", llm_response)

    lower = llm_response.lower()
    if any(w in lower for w in ("bullish", "beat", "accumulate", "raised guidance", "upgrade")):
        stype = SignalType.STRONG_BUY if avg_score > 0.3 else SignalType.BUY
    elif any(w in lower for w in ("bearish", "miss", "distribution", "lowered guidance", "downgrade")):
        stype = SignalType.STRONG_SELL if avg_score < -0.3 else SignalType.SELL
    else:
        stype = SignalType.NEUTRAL

    conf = min(abs(avg_score) + 0.1, 0.9)

    return [Signal(
        ticker=ticker,
        source=SignalSource.NEWS,
        signal_type=stype,
        timeframe=Timeframe.MN1,
        confidence=conf,
        value=avg_score,
        reasoning=f"Quarterly earnings/analyst thesis: {llm_response[:150]}",
        metadata={"layer": 1, "article_count": len(items), "finbert_avg": avg_score, "llm_response": llm_response},
    )]
