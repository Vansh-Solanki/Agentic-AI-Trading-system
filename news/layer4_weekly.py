import logging
from core.config import LLM_TIMEOUT_SECONDS
from core.models import Signal
from core.constants import SignalType, SignalSource, Timeframe
from data.storage.news_store import get_recent_news
from data.storage.stock_memory_store import get_memory
from data.cache.market_data_cache import get_candles
from llm.client import query_llm
from llm.prompts.weekly_bias import build_weekly_bias_prompt

logger = logging.getLogger(__name__)


def run_weekly_analysis(ticker: str) -> list[Signal]:
    """Layer 4 — Weekly directional bias via LLM, combining stored news + technical context + quarterly thesis."""
    rows = get_recent_news(ticker, limit=30)
    if not rows:
        return []

    news_summary       = _build_news_summary(rows)
    technical_summary  = _build_technical_summary(ticker)
    quarterly_thesis   = get_memory(ticker, "long_term", "quarterly_thesis")

    prompt       = build_weekly_bias_prompt(ticker, news_summary, technical_summary, quarterly_thesis)
    # Session 39, item 67 mitigation: bounds how long this thread can hold
    # llm/client.py's _ollama_semaphore slot past its caller's own timeout —
    # future.result() timing out doesn't kill this thread.
    llm_response = query_llm(prompt, max_tokens=300, timeout=LLM_TIMEOUT_SECONDS)
    if llm_response is None:
        # Layer 4 has no independent score to fall back on (unlike Layer 2's
        # FinBERT avg or Layer 3's FinBERT score) — skip this cycle rather
        # than manufacture a signal from nothing, same pattern as Layer 5.
        logger.warning("Layer 4: %s — LLM call failed, skipping weekly thesis this cycle.", ticker)
        return []

    lower = llm_response.lower()
    if any(w in lower for w in ("accumulate", "bullish", "uptrend", "buy")):
        stype, conf = SignalType.BUY, 0.6
    elif any(w in lower for w in ("avoid", "bearish", "downtrend", "sell", "reduce")):
        stype, conf = SignalType.SELL, 0.6
    else:
        stype, conf = SignalType.NEUTRAL, 0.4

    return [Signal(
        ticker=ticker,
        source=SignalSource.NEWS,
        signal_type=stype,
        timeframe=Timeframe.W1,
        confidence=conf,
        value=None,
        reasoning=f"Weekly LLM bias: {llm_response[:150]}",
        metadata={"layer": 4, "llm_response": llm_response},
    )]


def _build_news_summary(rows: list[dict]) -> str:
    headlines = [r["headline"] for r in rows[:10] if r.get("headline")]
    return "\n".join(f"- {h}" for h in headlines) or "No recent news."


def _build_technical_summary(ticker: str) -> str:
    # Session 39 fix: read through the pre-warmed cache instead of calling
    # price_feed.fetch_candles() directly (same bug class as Layer 3's spike
    # gate) — lower-impact here since Layer 4 is already staggered/hourly,
    # but the same one-line fix removes a redundant live fetch either way.
    try:
        df = get_candles(ticker, Timeframe.D1)
        if df.empty or len(df) < 20:
            return "Insufficient data."
        close = df["Close"]
        price = float(close.iloc[-1])
        sma20 = float(close.rolling(20).mean().iloc[-1])
        parts = [f"Price {price:.2f} is {'above' if price > sma20 else 'below'} 20-day SMA {sma20:.2f}."]
        if len(df) >= 50:
            sma50 = float(close.rolling(50).mean().iloc[-1])
            parts.append(f"20-day SMA {'above' if sma20 > sma50 else 'below'} 50-day SMA {sma50:.2f}.")
        return " ".join(parts)
    except Exception as exc:
        logger.warning("Could not build technical summary for %s: %s", ticker, exc)
        return "Technical data unavailable."
