"""
Live trading entry point.
Runs both intraday and long-term pipelines concurrently from a single process.

Session 42: replaces run_paper.py's live-market behavior. The premarket news
fetch and premarket-only agent pass now live in scripts/run_premarket.py,
scheduled separately (e.g. 8:30 AM ET) — this script no longer runs that
batch itself. It only runs the full 11-agent pipeline (including
ExecutionAgent) on a continuous loop during market hours, reading whatever
premarket signals/trade plans run_premarket.py already wrote to SQLite.

Session 34 cycle frequency split (Decision 2): the full 11-agent pipeline —
signals, LLM, execution — runs every PIPELINE_CYCLE_SECS (default 300s)
instead of every 60s, since 1-minute candles were removed (5-minute is the
new floor) and re-running every agent every 60s wasted Ollama/yfinance/FinBERT
calls on unchanged candle data. Long-term full pipeline runs every
LONGTERM_LOOP_INTERVAL_SECONDS (default 1800s).

Session 39 (item 68): SL/TP breach checks, closed-trade reconciliation, and
the long-term SL/TP price monitor no longer live in the same loop as the two
run_all() calls above — a long pipeline cycle (Ollama queueing under load)
was capable of fully starving stop-loss protection for its entire duration.
They now run on their own daemon thread (`_maintenance_loop`, every
INTRADAY_CYCLE_SECS for intraday maintenance, every 300s for the long-term
price monitor), completely decoupled from the main loop below.

Both pipelines share the same account equity and aggregated risk budget.
Each pipeline owns independent position lanes: (ticker, strategy_mode) is the
unique key, so a ticker may hold one intraday position and one long-term
position simultaneously without either lane interfering with the other.
"""
import sys
import os
import time
import logging
import threading
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Pandas warns that float(single_element_series) will require .iloc[0] in a
# future release. Harmless today (still evaluates correctly) and noisy across
# ~20 call sites — silenced until we do the .iloc[0] cleanup pass.
warnings.filterwarnings("ignore", category=FutureWarning, message="Calling float on a single element Series")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from core.config import WATCHLIST, LONGTERM_LOOP_INTERVAL_SECONDS, INTRADAY_CYCLE_SECS, PIPELINE_CYCLE_SECS
from core.config import TradingMode
from core.constants import INTRADAY_TIMEFRAMES, LONGTERM_TIMEFRAMES
from data.storage.db import init_schema
from data.cache.market_data_cache import warm_up
from agents.orchestrator import Orchestrator
from execution.broker.alpaca import AlpacaBroker
from news.finbert import get_finbert_pipeline
from llm.client import warm_up_llm


def _get_portfolio_value(fallback: float) -> float:
    try:
        return AlpacaBroker().get_account()["portfolio_value"]
    except Exception as exc:
        logging.warning("Could not fetch portfolio value (%s) — using $%.2f", exc, fallback)
        return fallback


def _maintenance_loop(intraday_orch: Orchestrator, longterm_orch: Orchestrator, stop_event: threading.Event) -> None:
    """
    Session 39, item 68: SL/TP breach checks and closed-trade reconciliation
    must never go dark just because run_all() is stuck in a long pipeline
    cycle (worst case, under an Ollama queueing burst, estimated ~240s — see
    progress.md Session 39 finding 3). Runs on its own daemon thread,
    completely decoupled from the main loop below (which only drives the two
    run_all() pipelines), so stop-loss protection keeps firing on schedule
    regardless of pipeline duration. Orchestrator._maintenance_lock serializes
    this against run_all() on the same instance to prevent a double-close/
    duplicate-exit race.
    """
    last_longterm_sl_check = 0.0
    while not stop_event.is_set():
        loop_start = time.monotonic()

        try:
            intraday_orch.run_maintenance()
        except Exception:
            logging.exception("Intraday maintenance error — continuing")

        now = time.monotonic()
        if now - last_longterm_sl_check >= 300:
            try:
                longterm_orch.check_sl_tp_exits()
            except Exception:
                logging.exception("Long-term SL/TP price check error — continuing")
            last_longterm_sl_check = time.monotonic()

        elapsed = time.monotonic() - loop_start
        stop_event.wait(max(0.0, INTRADAY_CYCLE_SECS - elapsed))


def main():
    logging.info("Starting live trading | dual-mode | watchlist=%s", WATCHLIST)

    init_schema()

    # Pre-warm the FinBERT singleton on the main thread before any parallel
    # agent work starts — avoids every ticker's NewsSentimentAgent thread
    # racing to load the model on first use. get_finbert_pipeline() catches
    # its own load failures and returns None rather than raising (Session 39
    # fix), so check the result here instead of assuming success — the app
    # runs fine without FinBERT (score_sentiment() falls back to neutral 0.0),
    # it just shouldn't silently claim "ready" when it isn't.
    logging.info("Pre-warming FinBERT...")
    if get_finbert_pipeline() is not None:
        logging.info("FinBERT ready.")
    else:
        logging.warning("FinBERT unavailable — continuing without it; sentiment scoring will use a neutral (0.0) fallback for this run.")

    logging.info("Warming up LLM backend...")
    if warm_up_llm():
        logging.info("LLM backend warm.")
    else:
        logging.warning("LLM warm-up call failed — first real call this run may pay the full cold-load penalty.")

    # Warm up cache for all timeframes used by both modes
    all_timeframes = list(dict.fromkeys(INTRADAY_TIMEFRAMES + LONGTERM_TIMEFRAMES))
    warm_up(WATCHLIST, all_timeframes)

    portfolio_value = _get_portfolio_value(100_000.0)
    logging.info("Initial portfolio value: $%.2f", portfolio_value)

    intraday_orch  = Orchestrator(TradingMode.INTRADAY,   portfolio_value=portfolio_value)
    longterm_orch  = Orchestrator(TradingMode.LONG_TERM,  portfolio_value=portfolio_value)

    # Session 39, item 68: SL/TP monitoring runs on its own thread, independent
    # of the main loop below, so it can never be starved by a long run_all().
    maintenance_stop = threading.Event()
    maintenance_thread = threading.Thread(
        target=_maintenance_loop,
        args=(intraday_orch, longterm_orch, maintenance_stop),
        name="maintenance-loop",
        daemon=True,
    )
    maintenance_thread.start()

    last_pipeline_run: float = 0.0          # epoch seconds; 0 forces an immediate first full-pipeline run
    last_longterm_run: float = 0.0          # epoch seconds; 0 forces an immediate first run

    try:
        while True:
            cycle_start = time.monotonic()

            # ── Refresh shared portfolio value once per cycle ──────────────────
            portfolio_value = _get_portfolio_value(portfolio_value)
            intraday_orch.portfolio_value = portfolio_value
            longterm_orch.portfolio_value = portfolio_value

            # ── Intraday full pipeline (every PIPELINE_CYCLE_SECS) ─────────────
            now = time.monotonic()
            if now - last_pipeline_run >= PIPELINE_CYCLE_SECS:
                try:
                    intraday_orch.run_all()
                except Exception:
                    logging.exception("Intraday pipeline error — continuing")
                last_pipeline_run = time.monotonic()

            # ── Long-term pipeline (every LONGTERM_LOOP_INTERVAL_SECONDS) ──────
            now = time.monotonic()
            if now - last_longterm_run >= LONGTERM_LOOP_INTERVAL_SECONDS:
                logging.info("Running long-term pipeline cycle.")
                try:
                    longterm_orch.run_all()
                except Exception:
                    logging.exception("Long-term pipeline error — continuing")
                last_longterm_run = time.monotonic()

            elapsed = time.monotonic() - cycle_start
            sleep_for = max(0.0, INTRADAY_CYCLE_SECS - elapsed)
            logging.info("Cycle complete (%.1fs). Sleeping %.1fs...", elapsed, sleep_for)
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        logging.info("Shutting down.")
    finally:
        maintenance_stop.set()
        maintenance_thread.join(timeout=5)


if __name__ == "__main__":
    main()
