"""
Weekly news batch entry point.

Session 49: Layer 1 (quarterly earnings/analyst thesis) and Layer 5 (monthly
investment thesis) both ask slow-moving questions that don't meaningfully
change day to day, so they no longer run in the daily pre-market batch
(scripts/run_premarket.py -> news/news_ingest.py) or the live pipeline
(agents/news_sentiment.py). This script is now the only caller of
run_quarterly_analysis() and run_monthly_analysis() — meant to be scheduled
Monday-only via cron. Both layers persist their result to stock_memory
(quarterly_thesis/quarterly_signal, monthly_thesis/monthly_signal) so the
live pipeline can read the cached result back Tuesday through Sunday instead
of losing it for six days a week (see
agents/news_sentiment.py::_cached_thesis_signal()).

One-shot script, no loop, no execution/trading side effects — same shape as
scripts/run_premarket.py's news portion. Cadence (Monday-only) is enforced by
whatever schedules this script, not by the script itself, so it can still be
run manually on any day for testing.
"""
import sys
import os
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from core.config import WATCHLIST, LLM_TIMEOUT_SECONDS
from data.storage.db import init_schema
from news.finbert import get_finbert_pipeline
from news.layer1_quarterly import run_quarterly_analysis
from news.layer5_monthly import run_monthly_analysis
from llm.client import warm_up_llm


def main():
    logging.info("Starting weekly news batch (Layer 1 quarterly + Layer 5 monthly) | watchlist=%s", WATCHLIST)

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

    quarterly_done = 0
    monthly_done = 0
    for i, ticker in enumerate(WATCHLIST, 1):
        logging.info("[%d/%d] %s: starting Layer 1 (quarterly)...", i, len(WATCHLIST), ticker)
        try:
            if run_quarterly_analysis(ticker, llm_timeout=LLM_TIMEOUT_SECONDS):
                quarterly_done += 1
                logging.info("[%d/%d] %s: Layer 1 done.", i, len(WATCHLIST), ticker)
            else:
                logging.info("[%d/%d] %s: Layer 1 returned no signal.", i, len(WATCHLIST), ticker)
        except Exception:
            logging.exception("Weekly news: %s — Layer 1 (quarterly) failed", ticker)

        logging.info("[%d/%d] %s: starting Layer 5 (monthly)...", i, len(WATCHLIST), ticker)
        try:
            if run_monthly_analysis(ticker, llm_timeout=LLM_TIMEOUT_SECONDS):
                monthly_done += 1
                logging.info("[%d/%d] %s: Layer 5 done.", i, len(WATCHLIST), ticker)
            else:
                logging.info("[%d/%d] %s: Layer 5 returned no signal.", i, len(WATCHLIST), ticker)
        except Exception:
            logging.exception("Weekly news: %s — Layer 5 (monthly) failed", ticker)

    logging.info(
        "Weekly news batch complete: %d/%d tickers produced a quarterly signal, %d/%d produced a monthly signal.",
        quarterly_done, len(WATCHLIST), monthly_done, len(WATCHLIST),
    )


if __name__ == "__main__":
    main()
