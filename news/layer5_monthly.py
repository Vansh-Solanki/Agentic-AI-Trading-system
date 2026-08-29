import json
import logging
from core.config import LLM_TIMEOUT_SECONDS
from core.models import Signal
from core.constants import SignalType, SignalSource, Timeframe
from data.storage.news_store import get_recent_news_events
from data.storage.stock_memory_store import get_memory, upsert_memory
from data.feeds.fundamentals_feed import fetch_fundamentals
from llm.client import query_llm
from llm.prompts.monthly_bias import build_monthly_bias_prompt
from llm.response_parser import parse_stance_json

logger = logging.getLogger(__name__)


def run_monthly_analysis(ticker: str, llm_timeout: int = LLM_TIMEOUT_SECONDS) -> list[Signal]:
    """
    Layer 5 — Monthly investment thesis via LLM, combining fundamentals + stored news + quarterly thesis.
    Session 49: no longer runs live — only called from scripts/run_weekly_news.py
    (Monday-only); the live pipeline reads the cached result back the rest of
    the week instead (agents/news_sentiment.py). Persists its thesis to
    stock_memory (previously it did not persist anything at all, unlike
    Layer 1's quarterly_thesis) so there's something to read Tuesday-Sunday.

    `llm_timeout`: defaults to the shared LLM_TIMEOUT_SECONDS constant
    (core/config.py) rather than its own hardcoded value.
    """
    rows = get_recent_news_events(ticker, hours_back=720)  # 30 days, matches monthly horizon
    if not rows:
        return []

    fundamental_summary = _build_fundamental_summary(ticker)
    news_summary        = _build_news_summary(rows)
    quarterly_thesis    = get_memory(ticker, "long_term", "quarterly_thesis")

    prompt       = build_monthly_bias_prompt(ticker, fundamental_summary, news_summary, quarterly_thesis)
    llm_response = query_llm(prompt, max_tokens=400, timeout=llm_timeout)

    if llm_response is None:
        # LLM call failed — Layer 5's entire thesis is LLM-derived (there's no
        # independent FinBERT/fundamentals score to fall back on the way
        # Layer 2 has). Emitting a signal here would be pure guesswork, so
        # skip this cycle rather than manufacture a NEUTRAL with fake confidence.
        logger.warning("Layer 5: %s — LLM call failed, skipping monthly thesis this cycle.", ticker)
        return []

    parsed = parse_stance_json(
        llm_response, extra_fields=("valuation", "position_size", "risk")
    )
    stance = parsed["stance"]
    if stance == "bullish":
        stype, conf = SignalType.BUY, 0.65
    elif stance == "bearish":
        stype, conf = SignalType.SELL, 0.65
    else:
        stype, conf = SignalType.NEUTRAL, 0.4

    # Persist a human-readable thesis, mirroring Layer 1's existing
    # quarterly_thesis pattern, so the live pipeline's cached-signal read
    # path has something to read back Tuesday-Sunday (see
    # agents/news_sentiment.py::_cached_thesis_signal()).
    thesis_text = (
        f"{stance.capitalize()} — valuation: {parsed.get('valuation') or 'n/a'}; "
        f"position sizing: {parsed.get('position_size') or 'n/a'}. {parsed.get('risk') or ''}"
    ).strip()
    upsert_memory(ticker, "long_term", "monthly_thesis", thesis_text)
    upsert_memory(ticker, "long_term", "monthly_signal", json.dumps({
        "signal_type": stype.value, "confidence": conf,
    }))

    return [Signal(
        ticker=ticker,
        source=SignalSource.NEWS,
        signal_type=stype,
        timeframe=Timeframe.MN1,
        confidence=conf,
        value=None,
        reasoning=(
            f"Monthly LLM thesis ({stance}): valuation={parsed.get('valuation') or 'n/a'}, "
            f"position={parsed.get('position_size') or 'n/a'} | Risk: {parsed.get('risk') or ''}"
        )[:250],
        metadata={
            "layer": 5, "llm_response": llm_response, "stance": stance,
            "valuation": parsed.get("valuation"), "position_size": parsed.get("position_size"),
        },
    )]


def _build_fundamental_summary(ticker: str) -> str:
    try:
        data = fetch_fundamentals(ticker)
        parts = []
        if data.pe_ratio is not None:
            parts.append(f"P/E={data.pe_ratio:.1f}")
        if data.pb_ratio is not None:
            parts.append(f"P/B={data.pb_ratio:.1f}")
        if data.revenue_growth is not None:
            parts.append(f"RevGrowth={data.revenue_growth:.1%}")
        if data.debt_to_equity is not None:
            parts.append(f"D/E={data.debt_to_equity:.2f}")
        return ", ".join(parts) if parts else "Fundamental data unavailable."
    except Exception as exc:
        logger.warning("Could not fetch fundamentals for %s: %s", ticker, exc)
        return "Fundamental data unavailable."


def _build_news_summary(rows: list[dict]) -> str:
    headlines = [r["headline"] for r in rows[:15] if r.get("headline")]
    return "\n".join(f"- {h}" for h in headlines) or "No recent news."