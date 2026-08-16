import threading
import time
import zlib
from core.config import TradingMode
from core.models import AgentOutput, Signal
from agents.base_agent import BaseAgent
from data.feeds.news_feed import fetch_intraday_news
from news.layer1_quarterly import run_quarterly_analysis
from news.layer2_premarket import run_premarket_analysis
from news.layer3_spike import run_spike_detector
from news.layer4_weekly import run_weekly_analysis
from news.layer5_monthly import run_monthly_analysis

# Run-frequency throttling (see progress.md Session 34/35): Layers 1, 2, 4, 5
# produce identical output when re-run every 5-minute cycle and were wasting
# Ollama calls. Only Layer 3 (spike detector) needs to run every cycle — it's
# already self-gated by ATR/volume conditions. Keyed by ticker; a
# threading.Lock guards them since multiple tickers run concurrently.
_last_run_layer1: dict[str, float] = {}
_last_run_layer4: dict[str, float] = {}
_last_run_layer5: dict[str, float] = {}
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


class NewsSentimentAgent(BaseAgent):
    @property
    def name(self) -> str:
        return "NewsSentimentAgent"

    def run(self, ticker: str, **kwargs) -> AgentOutput:
        signals: list[Signal] = []

        if self.mode == TradingMode.INTRADAY:
            items = fetch_intraday_news([ticker])
            spike_signals = run_spike_detector(ticker, items)
            signals.extend(spike_signals)

        # Layer 2 — pre-market only. The pre-market batch job in news_ingest.py
        # already covers this before market open; skip once the market is open.
        # Local import: agents.orchestrator imports NewsSentimentAgent at module
        # level, so a top-level import here would be circular.
        from agents.orchestrator import _is_market_open
        if not _is_market_open():
            premarket_signals = run_premarket_analysis(ticker)
            signals.extend(premarket_signals)

        # Layer 4 — once per hour per ticker, staggered on first sight so all
        # 20 tickers don't re-synchronize onto the same hourly burst (item 66).
        with _layer_timer_lock:
            if ticker not in _last_run_layer4:
                _last_run_layer4[ticker] = (
                    time.monotonic() - _LAYER4_INTERVAL_SECONDS + _layer4_stagger_offset(ticker)
                )
            run_layer4 = time.monotonic() - _last_run_layer4[ticker] >= _LAYER4_INTERVAL_SECONDS
        if run_layer4:
            weekly_signals = run_weekly_analysis(ticker)
            signals.extend(weekly_signals)
            with _layer_timer_lock:
                _last_run_layer4[ticker] = time.monotonic()

        # Layer 1 — once per session per ticker (never re-run once it has succeeded).
        with _layer_timer_lock:
            run_layer1 = ticker not in _last_run_layer1
        if run_layer1:
            quarterly_signals = run_quarterly_analysis(ticker)
            signals.extend(quarterly_signals)
            with _layer_timer_lock:
                _last_run_layer1[ticker] = time.monotonic()

        # Layer 5 — once per session per ticker (never re-run once it has succeeded).
        if self.mode == TradingMode.LONG_TERM:
            with _layer_timer_lock:
                run_layer5 = ticker not in _last_run_layer5
            if run_layer5:
                monthly_signals = run_monthly_analysis(ticker)
                signals.extend(monthly_signals)
                with _layer_timer_lock:
                    _last_run_layer5[ticker] = time.monotonic()

        layers = "spike+daily+weekly+quarterly" if self.mode == TradingMode.INTRADAY else "daily+weekly+quarterly+monthly"
        summary = f"{len(signals)} news-driven signals for {ticker} ({layers})"
        output = AgentOutput(agent_name=self.name, ticker=ticker, signals=signals, summary=summary)
        self._log_output(output)
        return output
