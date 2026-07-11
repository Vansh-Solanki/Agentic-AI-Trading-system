import logging
from core.models import Signal
from core.constants import SignalType, SignalSource, Timeframe
from data.feeds.news_feed import fetch_premarket_news
from data.storage.news_store import save_news, save_news_event
from news.finbert import score_batch
from news.news_ingest import sentiment_label
from llm.client import query_llm
from llm.prompts.daily_bias import build_daily_bias_prompt

logger = logging.getLogger(__name__)


def run_premarket_analysis(ticker: str) -> list[Signal]:
    """
    Layer 2 — Pre-market deep analysis.
    Fetches overnight news, scores with FinBERT for confidence, then escalates
    top stories to LLM. The LLM response drives the BUY/SELL/NEUTRAL direction;
    FinBERT avg_score provides the confidence magnitude.
    """
    items = fetch_premarket_news([ticker])
    if not items:
        return []

    for item in items:
        item.layer = 2

    headlines = [i.headline + " " + i.summary for i in items]
    scores    = score_batch(headlines)
    for item, score in zip(items, scores):
        item.sentiment_score = score
        save_news_event(item, sentiment_score=score, sentiment_label=sentiment_label(score))
    save_news(items)

    # Escalate top 5 most extreme articles to LLM for a daily bias
    sorted_items = sorted(items, key=lambda i: abs(i.sentiment_score or 0), reverse=True)[:5]
    news_text    = "\n".join(f"- {i.headline}" for i in sorted_items)
    prompt       = build_daily_bias_prompt(ticker, news_text)
    llm_response = query_llm(prompt)

    # LLM response drives direction; FinBERT avg provides confidence magnitude
    avg_score = sum(scores) / len(scores)
    lower     = llm_response.lower()

    if any(w in lower for w in ("bullish", "buy", "upside", "positive", "strength")):
        stype = SignalType.BUY
    elif any(w in lower for w in ("bearish", "sell", "downside", "negative", "weakness")):
        stype = SignalType.SELL
    else:
        stype = SignalType.NEUTRAL

    conf = min(abs(avg_score) + 0.1, 0.9)

    return [Signal(
        ticker=ticker,
        source=SignalSource.NEWS,
        signal_type=stype,
        timeframe=Timeframe.D1,
        confidence=conf,
        value=avg_score,
        reasoning=f"Pre-market LLM bias ({stype.value}): {llm_response[:120]} | FinBERT={avg_score:.3f}",
        metadata={"layer": 2, "llm_response": llm_response, "finbert_avg": avg_score},
    )]
