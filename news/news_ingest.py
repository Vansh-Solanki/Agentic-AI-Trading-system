import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from core.config import LLM_TIMEOUT_SECONDS
from core.models import NewsItem
from data.feeds.news_feed import fetch_premarket_news
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
    score = score_sentiment(item.headline + " " + item.summary)
    horizon = _classify_horizon(item.headline, timeout=llm_timeout) if classify_horizon else None
    save_news_event(item, sentiment_score=score, sentiment_label=sentiment_label(score), horizon=horizon)
    return score


def run_premarket_news_fetch(tickers: list[str]) -> None:
    """
    Pre-market batch (Session 36 redesign): runs Layers 2, 1, and 5 sequentially
    for each ticker before market open, so the day's first live cycles can skip
    all three (session flags below) instead of re-running them cold. Meant to
    start ~8:15am ET, though the original ~80s/ticker (~27 min for 20 tickers)
    worst-case estimate was based on the old 25s per-call cap — LLM_TIMEOUT_SECONDS
    was later raised to 90s (Session 39 addendum) after finding even a trivial
    prompt took ~19s, so the real worst-case per-ticker time is now
    correspondingly higher and hasn't been re-measured; verify there's still a
    comfortable buffer before 9:30am open before relying on the old estimate.

    Five crash mitigations (design locked Session 36):
      1. Ollama timeout cap — every LLM call in this batch passes
         timeout=LLM_TIMEOUT_SECONDS (see core/config.py; shared with every
         other query_llm() call in the app, not a batch-specific value) so one
         stuck request can't stall the whole batch.
      2. Per-layer exception isolation — each layer call is its own try/except;
         one layer failing for one ticker doesn't skip the other layers or
         abort remaining tickers.
      3. Session flag population — on success, write directly into
         agents.news_sentiment's _last_run_layer1 / _last_run_layer5 dicts so
         the first live pipeline cycle doesn't immediately re-run them.
      4. Per-ticker SQLite checkpoint — premarket_checkpoints records tickers
         already completed for today's date; a restart mid-batch skips them.
      5. Market-open abort guard — checked before starting each ticker; if the
         market has opened, log and abort the remaining tickers rather than
         running layers over into market hours.
    """
    # Local imports: layer2_premarket imports sentiment_label from this module
    # at its own module level, so importing the layers back at news_ingest's
    # module level would be a circular import (news_ingest <-> layer2_premarket).
    # Deferring to call time — after every module has finished loading — avoids
    # it, same reasoning as news_sentiment.py's local import of _is_market_open.
    from agents.orchestrator import _is_market_open
    from agents.news_sentiment import _last_run_layer1, _last_run_layer5, _layer_timer_lock
    from news.layer1_quarterly import run_quarterly_analysis
    from news.layer2_premarket import run_premarket_analysis
    from news.layer5_monthly import run_monthly_analysis

    batch_date = datetime.now(_ET).date().isoformat()
    logger.info("Pre-market news batch starting for %d tickers (batch_date=%s)...", len(tickers), batch_date)

    total_articles = 0
    for ticker in tickers:
        if _is_market_open():
            logger.warning("Market open — aborting remaining pre-market batch tickers.")
            break

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

        # Layer 2 — pre-market deep analysis (FinBERT + LLM daily bias)
        try:
            items = fetch_premarket_news([ticker])
        except Exception:
            logger.exception("Pre-market news: %s — Layer 2 fetch failed", ticker)
            items = []
        try:
            run_premarket_analysis(ticker, llm_timeout=LLM_TIMEOUT_SECONDS)
        except Exception:
            logger.exception("Pre-market news: %s — Layer 2 analysis failed", ticker)
        for item in items:
            try:
                ingest_news_item(item, classify_horizon=True, llm_timeout=LLM_TIMEOUT_SECONDS)
            except Exception:
                logger.exception("Pre-market news: %s — failed ingesting an article", ticker)
        total_articles += len(items)

        # Layer 1 — quarterly earnings/analyst thesis
        try:
            run_quarterly_analysis(ticker, llm_timeout=LLM_TIMEOUT_SECONDS)
            with _layer_timer_lock:
                _last_run_layer1[ticker] = time.monotonic()
        except Exception:
            logger.exception("Pre-market news: %s — Layer 1 failed", ticker)

        # Layer 5 — monthly investment thesis
        try:
            run_monthly_analysis(ticker, llm_timeout=LLM_TIMEOUT_SECONDS)
            with _layer_timer_lock:
                _last_run_layer5[ticker] = time.monotonic()
        except Exception:
            logger.exception("Pre-market news: %s — Layer 5 failed", ticker)

        save_premarket_checkpoint(ticker, batch_date)
        logger.info("Pre-market news: %s — fundamentals cached, %d articles ingested, layers 2/1/5 done", ticker, len(items))

    logger.info("Pre-market news batch complete: %d articles across %d tickers", total_articles, len(tickers))