import json
import logging
from core.config import LLM_TIMEOUT_SECONDS
from core.models import Signal
from core.constants import SignalType, SignalSource, Timeframe
from data.storage.news_store import get_recent_news_events
from data.storage.stock_memory_store import upsert_memory
from llm.client import query_llm
from llm.prompts.quarterly_analysis import build_quarterly_analysis_prompt
from llm.response_parser import parse_stance_json

logger = logging.getLogger(__name__)

_EARNINGS_KEYWORDS = {
    "earnings", "eps", "revenue", "guidance", "outlook", "analyst",
    "upgrade", "downgrade", "price target", "rating", "beat", "miss",
    "quarter", "q1", "q2", "q3", "q4", "institutional", "accumulation",
    "distribution", "raises", "lowers", "maintains",
}


def _is_earnings_relevant(row: dict) -> bool:
    text = ((row.get("headline") or "") + " " + (row.get("raw_summary") or "")).lower()
    return any(kw in text for kw in _EARNINGS_KEYWORDS)


def run_quarterly_analysis(ticker: str, llm_timeout: int = LLM_TIMEOUT_SECONDS) -> list[Signal]:
    """
    Layer 1 — Quarterly earnings/analyst narrative.
    Reads 90-day already-scored news_events rows, filters for earnings/analyst
    content, escalates to LLM for a quarterly thesis, and stores the thesis in
    stock_memory so Layer 4 (weekly) and Layer 5 (monthly) can incorporate it.
    Session 49: no longer runs live — only called from scripts/run_weekly_news.py
    (Monday-only); the live pipeline reads the cached result back the rest of
    the week instead (agents/news_sentiment.py).

    Session 49 fix: previously called fetch_news()/save_news() (the old,
    independent "news" table) instead of get_recent_news_events() (the
    "news_events" table Layer 2/3/4/5 actually write to and read from) — the
    same class of bug already fixed for Layer 4/5 in Session 45. Reading
    through news_events also means these rows are already FinBERT-scored at
    ingestion time, so this no longer needs its own score_batch() pass.

    `llm_timeout`: defaults to the shared LLM_TIMEOUT_SECONDS constant
    (core/config.py) rather than its own hardcoded value.
    """
    rows = get_recent_news_events(ticker, hours_back=90 * 24)
    if not rows:
        return []

    # Filter to earnings/analyst relevant articles
    items = [r for r in rows if _is_earnings_relevant(r)]
    if not items:
        # Fall back to all fetched rows rather than returning nothing
        items = rows

    scores = [r["sentiment_score"] for r in items if r.get("sentiment_score") is not None]
    if not scores:
        return []

    avg_score = sum(scores) / len(scores)

    # Build earnings headlines text for LLM
    sorted_items  = sorted(items, key=lambda r: abs(r.get("sentiment_score") or 0), reverse=True)[:10]
    earnings_text = "\n".join(f"- {r['headline']}" for r in sorted_items) or "No earnings/analyst headlines found."

    prompt       = build_quarterly_analysis_prompt(ticker, earnings_text, avg_score)
    llm_response = query_llm(prompt, max_tokens=300, timeout=llm_timeout)

    if llm_response is None:
        # LLM call failed — do NOT overwrite quarterly_thesis in stock_memory
        # with a placeholder. Layer 4/5 read this field back into their own
        # prompts, so a bad write here corrupts context for future cycles,
        # not just this one. Leaving the prior thesis in place is strictly
        # safer than replacing it with nothing.
        logger.warning("Layer 1: %s — LLM call failed, skipping quarterly_thesis update this cycle.", ticker)
        return []

    parsed = parse_stance_json(
        llm_response,
        extra_fields=("earnings_trend", "analyst_sentiment", "institutional_tone", "reasoning"),
    )
    stance = parsed["stance"]

    # Persist a human-readable thesis (not the raw JSON blob) so Layer 4/5 can
    # pull it into their own prompts as free-text context.
    thesis_text = (
        f"{stance.capitalize()} — earnings: {parsed.get('earnings_trend') or 'n/a'}; "
        f"analysts: {parsed.get('analyst_sentiment') or 'n/a'}; "
        f"institutional tone: {parsed.get('institutional_tone') or 'n/a'}. "
        f"{parsed.get('reasoning') or ''}"
    ).strip()
    upsert_memory(ticker, "long_term", "quarterly_thesis", thesis_text)

    if stance == "bullish":
        stype = SignalType.STRONG_BUY if avg_score > 0.3 else SignalType.BUY
    elif stance == "bearish":
        stype = SignalType.STRONG_SELL if avg_score < -0.3 else SignalType.SELL
    else:
        stype = SignalType.NEUTRAL

    conf = min(abs(avg_score) + 0.1, 0.9)

    # Session 49: small structured companion to quarterly_thesis, written
    # purely so the live pipeline's cached-signal read path (see
    # agents/news_sentiment.py::_cached_thesis_signal()) can reconstruct an
    # equivalent Signal on non-Monday days without re-deriving stance from
    # the free-text thesis prose — that would reintroduce the exact
    # keyword-scan collision risk Session 46 removed from the free-text layers.
    upsert_memory(ticker, "long_term", "quarterly_signal", json.dumps({
        "signal_type": stype.value, "confidence": conf, "value": avg_score,
    }))

    return [Signal(
        ticker=ticker,
        source=SignalSource.NEWS,
        signal_type=stype,
        timeframe=Timeframe.MN1,
        confidence=conf,
        value=avg_score,
        reasoning=f"Quarterly earnings/analyst thesis: {thesis_text[:150]}",
        metadata={
            "layer": 1, "article_count": len(items), "finbert_avg": avg_score,
            "llm_response": llm_response, "stance": stance,
            "earnings_trend": parsed.get("earnings_trend"),
            "analyst_sentiment": parsed.get("analyst_sentiment"),
            "institutional_tone": parsed.get("institutional_tone"),
        },
    )]