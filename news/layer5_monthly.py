import logging
from core.models import Signal
from core.constants import SignalType, SignalSource, Timeframe
from data.storage.news_store import get_recent_news
from data.storage.stock_memory_store import get_memory
from data.feeds.fundamentals_feed import fetch_fundamentals
from llm.client import query_llm
from llm.prompts.monthly_bias import build_monthly_bias_prompt

logger = logging.getLogger(__name__)


def run_monthly_analysis(ticker: str) -> list[Signal]:
    """Layer 5 — Monthly investment thesis via LLM, combining fundamentals + stored news + quarterly thesis."""
    rows = get_recent_news(ticker, limit=40)
    if not rows:
        return []

    fundamental_summary = _build_fundamental_summary(ticker)
    news_summary        = _build_news_summary(rows)
    quarterly_thesis    = get_memory(ticker, "long_term", "quarterly_thesis")

    prompt       = build_monthly_bias_prompt(ticker, fundamental_summary, news_summary, quarterly_thesis)
    llm_response = query_llm(prompt, max_tokens=400)

    lower = llm_response.lower()
    if any(w in lower for w in ("overweight", "undervalued", "accumulate", "bullish")):
        stype, conf = SignalType.BUY, 0.65
    elif any(w in lower for w in ("underweight", "overvalued", "avoid", "bearish")):
        stype, conf = SignalType.SELL, 0.65
    else:
        stype, conf = SignalType.NEUTRAL, 0.4

    return [Signal(
        ticker=ticker,
        source=SignalSource.NEWS,
        signal_type=stype,
        timeframe=Timeframe.MN1,
        confidence=conf,
        value=None,
        reasoning=f"Monthly LLM thesis: {llm_response[:150]}",
        metadata={"layer": 5, "llm_response": llm_response},
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
