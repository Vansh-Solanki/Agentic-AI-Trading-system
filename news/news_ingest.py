import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from core.config import LLM_TIMEOUT_SECONDS
from core.models import NewsItem
from data.storage.news_store import (
    save_news_event, is_premarket_checkpoint_complete, save_premarket_checkpoint,
)
from data.feeds.fundamentals_feed import fetch_fundamentals
from data.storage.fundamentals_store import save_fundamentals_cache
from news.finbert import score_sentiment
from llm.client import query_llm

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

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


def _classify_horizon(headline: str, timeout: int = LLM_TIMEOUT_SECONDS) -> str:
    raw_resp = query_llm(_HORIZON_PROMPT.format(headline=headline), max_tokens=10, timeout=timeout)
    if raw_resp is None:
        # LLM call failed (timeout, connection error, etc.) — default to the
        # safer, more-inclusive label, same as an unrecognised response.
        return "both"
    resp = raw_resp.strip().lower()
    if "intraday" in resp:
        return "intraday"
    if "long" in resp:
        return "long_term"
    if "both" in resp:
        return "both"
    return "both"  # unrecognised response — default to the safer, more-inclusive label


def ingest_news_item(item: NewsItem, classify_horizon: bool = False, llm_timeout: int = LLM_TIMEOUT_SECONDS) -> float:
    """
    Scores an item with FinBERT and persists it to news_events. Horizon
    classification (an Ollama round-trip) is optional — skipped on the hot
    intraday/premarket-analysis path to keep per-cycle latency low, and only
    run during the dedicated pre-market batch job where the time budget
    accounts for it.
    Returns the FinBERT sentiment score.
    """
    score = item.sentiment_score if item.sentiment_score is not None else score_sentiment(item.headline + " " + item.summary)
    horizon = _classify_horizon(item.headline, timeout=llm_timeout) if classify_horizon else None
    save_news_event(item, sentiment_score=score, sentiment_label=sentiment_label(score), horizon=horizon)
    return score


def run_premarket_news_fetch(tickers: list[str]) -> None:
    """
    Pre-market batch (Session 36 redesign): runs Layer 2 for each ticker
    before market open, so the day's first live cycles can skip it (session
    flag below) instead of re-running it cold. Meant to start ~8:15am ET,
    though the original ~80s/ticker (~27 min for 20 tickers) worst-case
    estimate was based on the old 25s per-call cap — LLM_TIMEOUT_SECONDS
    was later raised to 90s (Session 39 addendum) after finding even a trivial
    prompt took ~19s, so the real worst-case per-ticker time is now
    correspondingly higher and hasn't been re-measured; verify there's still a
    comfortable buffer before 9:30am open before relying on the old estimate.

    Session 49: Layers 1 (quarterly) and 5 (monthly) removed from this batch
    entirely — both now run once, Monday only, via scripts/run_weekly_news.py,
    since both ask slow-moving questions that don't meaningfully change day to
    day. This is what actually delivers the time savings; both used to run
    here every single day.

    Crash mitigations (design locked Session 36, now covering Layer 2 only):
      1. Ollama timeout cap — every LLM call in this batch passes
         timeout=LLM_TIMEOUT_SECONDS (see core/config.py; shared with every
         other query_llm() call in the app, not a batch-specific value) so one
         stuck request can't stall the whole batch.
      2. Per-layer exception isolation — the layer call is its own try/except;
         a failure for one ticker doesn't abort remaining tickers.
      3. Per-ticker SQLite checkpoint — premarket_checkpoints records tickers
         already completed for today's date; a restart mid-batch skips them.
      4. Market-open abort guard — checked before starting each ticker; if the
         market has opened, log and abort the remaining tickers rather than
         running layers over into market hours.
    """
    # Local imports: layer2_premarket imports sentiment_label from this module
    # at its own module level, so importing the layers back at news_ingest's
    # module level would be a circular import (news_ingest <-> layer2_premarket).
    # Deferring to call time — after every module has finished loading — avoids
    # it, same reasoning as news_sentiment.py's local import of _is_market_open.
    from agents.orchestrator import _is_market_open
    from news.layer2_premarket import run_premarket_analysis

    batch_date = datetime.now(_ET).date().isoformat()
    logger.info("Pre-market news batch starting for %d tickers (batch_date=%s)...", len(tickers), batch_date)

    total_articles = 0
    for ticker in tickers:
        # Commented out (2026-09-11): user is intentionally running premarket
        # during market hours, so this abort guard would stop news fetching
        # mid-batch. Re-enable by uncommenting if premarket should stay
        # strictly pre-open again.
        # if _is_market_open():
        #     logger.warning("Market open — aborting remaining pre-market batch tickers.")
        #     break

        if is_premarket_checkpoint_complete(ticker, batch_date):
            logger.info("Pre-market news: %s — already completed today, skipping.", ticker)
            continue

        # Fundamentals (Session 38): fetched here, sequentially, so market-hours
        # cycles can read from fundamentals_cache instead of hitting yfinance
        # via _yf_lock inside the 10-ticker parallel phase.
        try:
            fundamentals = fetch_fundamentals(ticker)
            save_fundamentals_cache(fundamentals)
        except Exception:
            logger.exception("Pre-market news: %s — fundamentals fetch failed", ticker)

        # Layer 2 — pre-market deep analysis (FinBERT + LLM daily bias).
        # Session 45 addendum: this used to do its own fetch_premarket_news()
        # call here, then call run_premarket_analysis() right below, which
        # did a second, independent fetch_premarket_news() + FinBERT pass
        # over what is very likely the same articles — on separate NewsItem
        # objects that never shared state, so the "skip re-scoring" reuse
        # check added elsewhere never had a chance to fire for these. Fixed
        # by having run_premarket_analysis() do the one fetch + one FinBERT
        # pass and hand the already-scored objects back via items_out;
        # ingest_news_item() is no longer called here for these items since
        # run_premarket_analysis() already persists each one to news_events
        # with its FinBERT score (classify_horizon is always False on this
        # path now, so there is no horizon value ingest_news_item() would
        # have added on top).
        items: list[NewsItem] = []
        try:
            # Bug 4 fix: the batch is the one place that should always do
            # the real fetch/FinBERT/LLM work and refresh the cache that
            # agents/news_sentiment.py's live calls read from later the same
            # day — force_refresh=True skips the cache read unconditionally.
            run_premarket_analysis(ticker, llm_timeout=LLM_TIMEOUT_SECONDS, items_out=items, force_refresh=True)
        except Exception:
            logger.exception("Pre-market news: %s — Layer 2 fetch/analysis failed", ticker)
        total_articles += len(items)

        save_premarket_checkpoint(ticker, batch_date)
        logger.info("Pre-market news: %s — fundamentals cached, %d articles ingested, layer 2 done", ticker, len(items))

    logger.info("Pre-market news batch complete: %d articles across %d tickers", total_articles, len(tickers))