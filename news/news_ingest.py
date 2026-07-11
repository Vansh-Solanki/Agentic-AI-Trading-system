import logging
from core.models import NewsItem
from data.feeds.news_feed import fetch_premarket_news
from data.storage.news_store import save_news_event
from news.finbert import score_sentiment
from llm.client import query_llm

logger = logging.getLogger(__name__)

# Same bullish/bearish thresholds the Decision Agent uses for BUY/SELL classification,
# reused here so sentiment_label is consistent with how the rest of the system reads sentiment.
_BULLISH_THRESHOLD = 0.15
_BEARISH_THRESHOLD = -0.15

_HORIZON_PROMPT = (
    "Classify this financial news headline as one of exactly three words: "
    "intraday, long_term, or both.\n"
    "intraday = affects price action today only (earnings surprise, guidance, analyst action, "
    "short squeeze, spike-worthy event)\n"
    "long_term = affects the multi-month thesis only (strategic shift, regulatory change, "
    "management change, secular trend)\n"
    "both = affects both\n\n"
    "Headline: {headline}\n\n"
    "Respond with exactly one word: intraday, long_term, or both."
)


def sentiment_label(score: float) -> str:
    if score > _BULLISH_THRESHOLD:
        return "bullish"
    if score < _BEARISH_THRESHOLD:
        return "bearish"
    return "neutral"


def _classify_horizon(headline: str) -> str:
    resp = query_llm(_HORIZON_PROMPT.format(headline=headline), max_tokens=10).strip().lower()
    if "intraday" in resp:
        return "intraday"
    if "long" in resp:
        return "long_term"
    if "both" in resp:
        return "both"
    return "both"  # unrecognised response — default to the safer, more-inclusive label


def ingest_news_item(item: NewsItem, classify_horizon: bool = False) -> float:
    """
    Scores an item with FinBERT and persists it to news_events. Horizon
    classification (an Ollama round-trip) is optional — skipped on the hot
    intraday/premarket-analysis path to keep per-cycle latency low, and only
    run during the dedicated pre-market batch job where the time budget
    accounts for it.
    Returns the FinBERT sentiment score.
    """
    score = score_sentiment(item.headline + " " + item.summary)
    horizon = _classify_horizon(item.headline) if classify_horizon else None
    save_news_event(item, sentiment_score=score, sentiment_label=sentiment_label(score), horizon=horizon)
    return score


def run_premarket_news_fetch(tickers: list[str]) -> None:
    """
    Layer 1 pre-market batch (Session 34, Decision 3/4): sequential REST fetch
    across the watchlist, FinBERT sentiment + Ollama horizon classification per
    article, all persisted to news_events. Meant to run once at startup before
    market open so the day's first cycles have context instead of a cold start,
    and so news_events carries over from the prior session.
    """
    logger.info("Pre-market news fetch starting for %d tickers...", len(tickers))
    total_articles = 0
    for ticker in tickers:
        try:
            items = fetch_premarket_news([ticker])
        except Exception:
            logger.exception("Pre-market news fetch failed for %s", ticker)
            continue
        for item in items:
            ingest_news_item(item, classify_horizon=True)
        total_articles += len(items)
        logger.info("Pre-market news: %s — %d articles ingested", ticker, len(items))
    logger.info("Pre-market news fetch complete: %d articles across %d tickers", total_articles, len(tickers))
