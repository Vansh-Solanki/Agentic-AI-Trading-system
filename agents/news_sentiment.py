import json
import logging
import threading
import time
import zlib
from core.config import TradingMode
from core.constants import SignalSource, SignalType, Timeframe
from core.models import AgentOutput, Signal
from agents.base_agent import BaseAgent
from data.feeds.news_feed import fetch_intraday_news
from data.storage.stock_memory_store import get_memory
from news.layer2_premarket import run_premarket_analysis
from news.layer3_spike import run_spike_detector
from news.layer4_weekly import run_weekly_analysis

logger = logging.getLogger(__name__)

# Run-frequency throttling (see progress.md Session 34/35): Layer 4 produces
# identical output when re-run every 5-minute cycle and was wasting Ollama
# calls. Only Layer 3 (spike detector) needs to run every cycle — it's
# already self-gated by ATR/volume conditions. Keyed by ticker; a
# threading.Lock guards it since multiple tickers run concurrently.
# Layers 1 and 5 used to have their own throttle dicts here (once-per-session)
# but Session 49 moved them off the live path entirely — see
# _cached_thesis_signal() below.
_last_run_layer4: dict[str, float] = {}
_layer_timer_lock = threading.Lock()

_LAYER4_INTERVAL_SECONDS = 3600


def _layer4_stagger_offset(ticker: str) -> float:
    """
    Session 39, item 66: deterministic per-ticker offset within one hourly
    window. `_last_run_layer4.get(ticker, 0.0)` previously defaulted every
    unseen ticker to "already due," so all 20 tickers fired Layer 4 together
    on the first post-startup cycle and re-synchronized onto the same hourly
    burst thereafter — 20 Ollama calls at once through the 2-slot semaphore
    instead of a smooth per-ticker trickle. zlib.crc32 (not the builtin hash(),
    which is randomized per-process for strings) gives a stable offset across
    runs so the same ticker always staggers to the same point in the hour.
    """
    return zlib.crc32(ticker.encode()) % _LAYER4_INTERVAL_SECONDS


def _cached_thesis_signal(
    ticker: str, thesis_key: str, signal_key: str, layer: int, degraded: bool = False
) -> list[Signal]:
    """
    Session 49: Layers 1 (quarterly) and 5 (monthly) now only run live on
    Mondays, via scripts/run_weekly_news.py — both ask slow-moving questions
    that don't meaningfully change day to day. Tuesday through Sunday, this
    reads back whatever Monday's run cached in stock_memory instead of
    re-running the LLM analysis.

    thesis_key ("quarterly_thesis"/"monthly_thesis") holds the free-text
    thesis — unchanged format, still readable by Layer 4/5's own prompts.
    signal_key ("quarterly_signal"/"monthly_signal") holds a small JSON
    companion ({"signal_type", "confidence", ["value"]}) written alongside it
    purely so this function can reconstruct an equivalent Signal without
    re-deriving direction from the thesis prose — doing that would reintroduce
    the exact keyword-scan collision risk Session 46 removed from the
    free-text layers. Returns [] if either key is missing or malformed (e.g.
    the weekly batch has never run yet for this ticker).

    `degraded`: set when today's live fetch (premarket/intraday) came back
    with zero articles for this ticker this cycle — either a fetch error or
    a genuine no-news day. Either way, this cached thesis has no fresh
    confirmation behind it today, so its confidence is halved rather than
    reported at full strength, letting DecisionAgent's confidence-weighted
    scoring naturally down-weight it instead of silently treating stale
    cache as a live signal.
    """
    thesis_text = get_memory(ticker, "long_term", thesis_key)
    signal_raw = get_memory(ticker, "long_term", signal_key)
    if not thesis_text or not signal_raw:
        return []
    try:
        parsed = json.loads(signal_raw)
        stype = SignalType(parsed["signal_type"])
        conf = float(parsed["confidence"])
    except (json.JSONDecodeError, KeyError, ValueError):
        logger.warning("Cached signal for %s (%s) is malformed — skipping.", ticker, signal_key)
        return []
    reasoning = f"Cached (last computed Monday): {thesis_text[:150]}"
    if degraded:
        conf *= 0.5
        reasoning += " [degraded: no fresh news this cycle]"
    return [Signal(
        ticker=ticker,
        source=SignalSource.NEWS,
        signal_type=stype,
        timeframe=Timeframe.MN1,
        confidence=conf,
        value=parsed.get("value"),
        reasoning=reasoning,
        metadata={"layer": layer, "cached": True, "degraded": degraded},
    )]


class NewsSentimentAgent(BaseAgent):
    @property
    def name(self) -> str:
        return "NewsSentimentAgent"

    def run(self, ticker: str, **kwargs) -> AgentOutput:
        signals: list[Signal] = []
        # True once this cycle has actually tried a live fetch (Finnhub/Benzinga)
        # for `ticker` and gotten back zero articles — fetch error or genuine
        # no-news day, we don't distinguish. Used below to flag the cached
        # thesis signals as degraded instead of letting stale cache pass as
        # today's confirmation.
        news_degraded = False

        if self.mode == TradingMode.INTRADAY:
            items = fetch_intraday_news([ticker])
            if not items:
                news_degraded = True
            spike_signals = run_spike_detector(ticker, items)
            signals.extend(spike_signals)

        # Layer 2 — pre-market only. The pre-market batch job in news_ingest.py
        # already covers this before market open; skip once the market is open.
        # Local import: agents.orchestrator imports NewsSentimentAgent at module
        # level, so a top-level import here would be circular.
        from agents.orchestrator import _is_market_open
        layer2_ran = not _is_market_open()
        if layer2_ran:
            # Bug 4 fix: previously judged "degraded" by whether items_out
            # got populated — but a cache hit (news/layer2_cache.py) leaves
            # items_out empty by design (nothing was freshly fetched) while
            # still returning a perfectly good signal, which wrongly halved
            # the cached quarterly/monthly confidence on every cache hit.
            # run_premarket_analysis() only ever returns [] when it found no
            # articles at all (a real fetch failure/no-news day) — reading
            # degraded from the returned signals themselves is correct in
            # both the cache-hit and fresh-fetch cases.
            premarket_signals = run_premarket_analysis(ticker)
            signals.extend(premarket_signals)
            if not premarket_signals:
                news_degraded = True

        # Layer 4 — once per hour per ticker, staggered on first sight so all
        # 20 tickers don't re-synchronize onto the same hourly burst (item 66).
        # Skipped whenever Layer 2 already made an LLM call this cycle: both
        # allow up to LLM_TIMEOUT_SECONDS (90s) each, and two sequential calls
        # can exceed PHASE1_AGENT_TIMEOUT_SECONDS (100s), which showed up in
        # production as 'news' timing out with an empty output. Layer 4's own
        # hourly stagger timer is left untouched, so it just fires next cycle
        # instead of being skipped permanently.
        with _layer_timer_lock:
            if ticker not in _last_run_layer4:
                _last_run_layer4[ticker] = (
                    time.monotonic() - _LAYER4_INTERVAL_SECONDS + _layer4_stagger_offset(ticker)
                )
            run_layer4 = time.monotonic() - _last_run_layer4[ticker] >= _LAYER4_INTERVAL_SECONDS
        if run_layer4 and not layer2_ran:
            weekly_signals = run_weekly_analysis(ticker)
            signals.extend(weekly_signals)
            with _layer_timer_lock:
                _last_run_layer4[ticker] = time.monotonic()

        # Layer 1 (quarterly) and Layer 5 (monthly) — Session 49: no longer
        # run live. Both now run once, Monday only, via
        # scripts/run_weekly_news.py; every other day just reads back
        # whatever Monday cached in stock_memory (see _cached_thesis_signal()
        # above). Mode-scoping unchanged from before: quarterly is read in
        # both modes, monthly only in LONG_TERM.
        signals.extend(_cached_thesis_signal(
            ticker, "quarterly_thesis", "quarterly_signal", layer=1, degraded=news_degraded
        ))
        if self.mode == TradingMode.LONG_TERM:
            signals.extend(_cached_thesis_signal(
                ticker, "monthly_thesis", "monthly_signal", layer=5, degraded=news_degraded
            ))

        layer_parts = []
        if self.mode == TradingMode.INTRADAY:
            layer_parts.append("spike")
        if layer2_ran:
            layer_parts.append("daily")
        if run_layer4 and not layer2_ran:
            layer_parts.append("weekly")
        if news_degraded:
            layer_parts.append("degraded")
        layer_parts.append("cached_quarterly")
        if self.mode == TradingMode.LONG_TERM:
            layer_parts.append("cached_monthly")
        summary = f"{len(signals)} news-driven signals for {ticker} ({'+'.join(layer_parts)})"
        output = AgentOutput(agent_name=self.name, ticker=ticker, signals=signals, summary=summary)
        self._log_output(output)
        return output
