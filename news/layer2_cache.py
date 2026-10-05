"""
Bug 4: Layer 2 (news/layer2_premarket.py::run_premarket_analysis) ran three
times per ticker in a premarket run — once from the news_ingest.py batch,
then again from NewsSentimentAgent.run() for both the intraday and
long-term Orchestrator (each calling run_all(premarket_only=True)) — each
an independent fetch + FinBERT pass + LLM call for what is the same daily
bias question asked three times in under 15 minutes (Oct 4 log: NVDA's
identical prompt at 00:19:16, 00:26:09, 00:33:35).

Cache location: the existing `stock_memory` SQLite table
(data/storage/stock_memory_store.py), under mode="shared" (a value no
existing reader ever queries — Layers 1/4/5 and agents/news_sentiment.py's
_cached_thesis_signal() only ever use mode="long_term") and
key="layer2_signal". Confirmed no collision risk: agents/memory_agent.py
(MemoryAgent) doesn't read this table at all — it reads/writes JSON files
on disk via memory/stock_memory.py, a completely separate "memory" system
despite the similar name. No schema change needed — stock_memory.mode has
no CHECK constraint, just UNIQUE(ticker, mode, key).
"""
import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from core.constants import SignalSource, SignalType, Timeframe
from core.models import Signal
from data.storage.stock_memory_store import get_memory, upsert_memory

logger = logging.getLogger(__name__)

_CACHE_MODE = "shared"
_CACHE_KEY = "layer2_signal"
_NY_TZ = ZoneInfo("America/New_York")


def _signal_to_dict(signal: Signal) -> dict:
    return {
        "ticker": signal.ticker,
        "source": signal.source.value,
        "signal_type": signal.signal_type.value,
        "timeframe": signal.timeframe.value,
        "confidence": signal.confidence,
        "value": signal.value,
        "reasoning": signal.reasoning,
        "metadata": signal.metadata,
    }


def _signal_from_dict(d: dict) -> Signal:
    return Signal(
        ticker=d["ticker"],
        source=SignalSource(d["source"]),
        signal_type=SignalType(d["signal_type"]),
        timeframe=Timeframe(d["timeframe"]),
        confidence=float(d["confidence"]),
        value=d.get("value"),
        reasoning=d.get("reasoning", ""),
        metadata=dict(d.get("metadata") or {}),
    )


def save_layer2_signal(ticker: str, signal: Signal, article_count: int) -> None:
    """Cache today's Layer 2 daily-bias result. Caller (run_premarket_analysis)
    decides whether this result is cacheable at all — this function always
    writes whatever it's given."""
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "batch_date": datetime.now(_NY_TZ).date().isoformat(),
        "article_count": article_count,
        "signal": _signal_to_dict(signal),
    }
    upsert_memory(ticker, _CACHE_MODE, _CACHE_KEY, json.dumps(payload))


def get_cached_layer2_signal(ticker: str, max_age_minutes: float) -> Signal | None:
    """Return the cached Layer 2 Signal if one exists and is younger than
    max_age_minutes, else None. A malformed, missing, or expired entry is a
    cache miss — never an exception. Rebuilds a real Signal the same way
    agents/news_sentiment.py::_cached_thesis_signal() does, with
    metadata["cached"] = True added so callers can tell a cache hit apart
    from a freshly computed result."""
    raw = get_memory(ticker, _CACHE_MODE, _CACHE_KEY)
    if not raw:
        return None

    try:
        payload = json.loads(raw)
        saved_at = datetime.fromisoformat(payload["saved_at"])
        if saved_at.tzinfo is None:
            saved_at = saved_at.replace(tzinfo=timezone.utc)
        age_minutes = (datetime.now(timezone.utc) - saved_at).total_seconds() / 60.0
        if age_minutes > max_age_minutes:
            return None
        signal = _signal_from_dict(payload["signal"])
    except Exception:
        logger.warning("Layer 2 cache entry for %s is malformed — treating as a miss.", ticker)
        return None

    signal.metadata = dict(signal.metadata)
    signal.metadata["cached"] = True
    logger.info("Layer 2 cache hit for %s (age %.1f min)", ticker, age_minutes)
    return signal
