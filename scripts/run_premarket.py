"""
Premarket analysis entry point.

Session 42: one-shot script that runs the full agent pipeline (everything
except ExecutionAgent) for both INTRADAY and LONG_TERM modes, then exits.
Meant to be scheduled at 8:30 AM ET daily (Oracle/cron) so trade plans and
signals are sitting in SQLite before the market opens, but it can also be run
manually after hours for testing — it bypasses the `_is_market_open()` guard
via `run_all(premarket_only=True)` (see agents/orchestrator.py item 56/42A).

Does not start the maintenance thread and does not loop: no positions are
opened here (ExecutionAgent is skipped), so there is nothing to monitor.
scripts/run_live.py reads the signals/trade plans this script writes.
"""
import sys
import os
import logging
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

warnings.filterwarnings("ignore", category=FutureWarning, message="Calling float on a single element Series")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from core.config import WATCHLIST
from core.config import TradingMode
from core.constants import INTRADAY_TIMEFRAMES, LONGTERM_TIMEFRAMES
from data.storage.db import init_schema
from data.cache.market_data_cache import warm_up
from agents.orchestrator import Orchestrator
from execution.broker.alpaca import AlpacaBroker
from news.finbert import get_finbert_pipeline
from news.news_ingest import run_premarket_news_fetch
from llm.client import warm_up_llm


def _get_portfolio_value(fallback: float) -> float:
    try:
        return AlpacaBroker().get_account()["portfolio_value"]
    except Exception as exc:
        logging.warning("Could not fetch portfolio value (%s) — using $%.2f", exc, fallback)
        return fallback


def main():
    logging.info("Starting premarket analysis | dual-mode | watchlist=%s", WATCHLIST)

    init_schema()

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

    all_timeframes = list(dict.fromkeys(INTRADAY_TIMEFRAMES + LONGTERM_TIMEFRAMES))
    warm_up(WATCHLIST, all_timeframes)

    try:
        run_premarket_news_fetch(WATCHLIST)
    except Exception:
        logging.exception("Pre-market news fetch failed — continuing without it")

    portfolio_value = _get_portfolio_value(100_000.0)
    logging.info("Initial portfolio value: $%.2f", portfolio_value)

    intraday_orch = Orchestrator(TradingMode.INTRADAY,  portfolio_value=portfolio_value)
    longterm_orch = Orchestrator(TradingMode.LONG_TERM, portfolio_value=portfolio_value)

    try:
        intraday_results = intraday_orch.run_all(premarket_only=True)
        intraday_plans = sum(1 for r in intraday_results.values() if "trade_plan" in r)
        logging.info("Analysis done — INTRADAY: %d/%d tickers produced a trade plan.",
                     intraday_plans, len(intraday_results))
    except Exception:
        logging.exception("Intraday premarket pipeline error")

    try:
        longterm_results = longterm_orch.run_all(premarket_only=True)
        longterm_plans = sum(1 for r in longterm_results.values() if "trade_plan" in r)
        logging.info("Analysis done — LONG_TERM: %d/%d tickers produced a trade plan.",
                     longterm_plans, len(longterm_results))
    except Exception:
        logging.exception("Long-term premarket pipeline error")

    logging.info("Premarket analysis complete.")


if __name__ == "__main__":
    main()
