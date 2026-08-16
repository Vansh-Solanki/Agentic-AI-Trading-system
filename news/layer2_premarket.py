import logging
from core.config import LLM_TIMEOUT_SECONDS
from core.models import Signal
from core.constants import SignalType, SignalSource, Timeframe
from data.feeds.news_feed import fetch_premarket_news
from data.storage.news_store import save_news, save_news_event
from news.finbert import score_batch
from news.news_ingest import sentiment_label
from llm.client import query_llm
from llm.prompts.daily_bias import build_daily_bias_prompt

logger = logging.getLogger(__name__)


def run_premarket_analysis(ticker: str, llm_timeout: int = LLM_TIMEOUT_SECONDS) -> list[Signal]:
    """
    Layer 2 — Pre-market deep analysis.
    Fetches overnight news, scores with FinBERT for confidence, then escalates
    top stories to LLM. The LLM response drives the BUY/SELL/NEUTRAL direction;
    FinBERT avg_score provides the confidence magnitude.

    `llm_timeout`: defaults to the shared LLM_TIMEOUT_SECONDS constant
    (core/config.py) rather than its own hardcoded value.
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
    llm_response = query_llm(prompt, timeout=llm_timeout)

    # LLM response drives direction; FinBERT avg provides confidence magnitude
    avg_score = sum(scores) / len(scores)

    llm_failed = llm_response is None
    if llm_failed:
        logger.warning("Layer 2: %s — LLM call failed, falling back to FinBERT-only NEUTRAL bias.", ticker)
        stype = SignalType.NEUTRAL
        # Halve confidence rather than using the normal FinBERT-derived
        # magnitude — this signal is missing the LLM's directional read
        # entirely, not genuinely neutral, and shouldn't carry the same
        # weight as a completed analysis.
        conf = min(abs(avg_score) + 0.1, 0.9) * 0.5
        reasoning = f"Pre-market LLM bias unavailable (call failed) | FinBERT={avg_score:.3f}"
    else:
        lower = llm_response.lower()
        if any(w in lower for w in ("bullish", "buy", "upside", "positive", "strength")):
            stype = SignalType.BUY
        elif any(w in lower for w in ("bearish", "sell", "downside", "negative", "weakness")):
            stype = SignalType.SELL
        else:
            stype = SignalType.NEUTRAL
        conf = min(abs(avg_score) + 0.1, 0.9)
        reasoning = f"Pre-market LLM bias ({stype.value}): {llm_response[:120]} | FinBERT={avg_score:.3f}"

    return [Signal(
        ticker=ticker,
        source=SignalSource.NEWS,
        signal_type=stype,
        timeframe=Timeframe.D1,
        confidence=conf,
        value=avg_score,
        reasoning=reasoning,
        metadata={"layer": 2, "llm_response": llm_response, "finbert_avg": avg_score, "llm_failed": llm_failed},
    )]