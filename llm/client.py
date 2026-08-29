import logging
import threading
from typing import Optional
import requests
from core.config import OLLAMA_BASE_URL, OLLAMA_MODEL, ANTHROPIC_API_KEY, LLM_TIMEOUT_SECONDS
import time 

logger = logging.getLogger(__name__)

# Session 34 addendum (found during live verification, same pattern as the
# Finnhub fix): Ollama serializes generation on a single model instance. With
# up to 10 tickers x multiple news/critic layers all calling query_llm()
# concurrently within one Phase-1 cycle, requests queue behind each other and
# tail requests blow through both Ollama's 60s read timeout and the
# orchestrator's 100s Phase-1 agent timeout. Capping concurrent Ollama calls
# doesn't reduce total work, but keeps queuing civil instead of every thread
# opening its own 60s-timeout connection at once.
_ollama_semaphore = threading.Semaphore(1)

# Session 51 item 67 (51A): a non-blocking queue limiter (BoundedSemaphore(3)
# gating entry to _ollama_semaphore, added Session 48) used to reject any 4th+
# concurrent caller outright instead of letting it wait. A premarket-run log
# showed this was discarding most calls, not just delaying them: out of ~40
# ticker/mode run_premarket_analysis() calls, only ~7 got a real LLM response
# through the queue; the rest fell back to FinBERT-only NEUTRAL, effectively
# disabling Layer 2's directional signal for most of the batch. Removed —
# every caller now blocks on _ollama_semaphore and gets a genuine attempt (or
# a genuine timeout/connection failure) instead of an artificial queue-depth
# rejection. Accepted tradeoff: calls become strictly sequential, so total
# batch runtime grows; premarket has no hard time budget so this is fine
# there. NOT yet verified against the live/intraday path, which shares this
# same module — if enough tickers need an LLM call in the same live cycle,
# sequential blocking here could push waits past PHASE1_AGENT_TIMEOUT_SECONDS
# or stack PIPELINE_CYCLE_SECS cycles under load. Revisit with a live log if
# that surfaces; see progress.md item 67 for parked mitigation options.


def query_llm(prompt: str, max_tokens: int = 512, timeout: int = LLM_TIMEOUT_SECONDS) -> Optional[str]:
    """
    Send a prompt to the configured LLM backend.
    Uses Ollama by default; falls back to Claude API if ANTHROPIC_API_KEY is set.

    `timeout` (seconds) defaults to the single shared LLM_TIMEOUT_SECONDS
    constant (core/config.py) rather than its own hardcoded value, so every
    caller agrees on the same number unless it has a genuine reason to
    override — callers with a tight time budget can still pass an explicit
    shorter timeout.

    Returns None on any failure (timeout, connection error, API error) instead
    of a sentinel string. This is deliberate (Session 39+ fix): a sentinel
    string like "[LLM unavailable]" can silently pass through downstream
    keyword-matching and JSON-adjacent logic as if it were real model output,
    and can get persisted into long-lived stores (e.g. stock_memory) as if it
    were a genuine thesis. Every caller must explicitly branch on None rather
    than being able to accidentally treat a failure as content.
    """
    if ANTHROPIC_API_KEY:
        return _query_claude(prompt, max_tokens, timeout)
    return _query_ollama(prompt, max_tokens, timeout)


def warm_up_llm(timeout: int = LLM_TIMEOUT_SECONDS) -> bool:
    """
    Session 47 item 13: fires one trivial, throwaway prompt so the model's
    cold-load penalty (benchmarked at ~9.2s for the first call vs ~2.6s once
    warm) lands once here at process startup, instead of unpredictably on
    whichever ticker/agent happens to be first through the pipeline that run.
    Returns whether the warm-up call succeeded — a failure here is not fatal,
    it just means the next real call pays the cold-load cost instead.
    """
    return query_llm("Reply with the single word: ready", max_tokens=5, timeout=timeout) is not None


def _query_ollama(prompt: str, max_tokens: int, timeout: int = LLM_TIMEOUT_SECONDS) -> Optional[str]:
    url = f"{OLLAMA_BASE_URL}/api/generate"
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": max_tokens},
    }
    queue_start = time.monotonic()
    try:
        with _ollama_semaphore:
            wait_time = time.monotonic() - queue_start
            call_start = time.monotonic()
            resp = requests.post(url, json=payload, timeout=timeout)
            call_time = time.monotonic() - call_start
        resp.raise_for_status()
        logger.info(
            "Ollama call succeeded — waited %.1fs for slot, call itself took %.1fs (prompt len=%d, max_tokens=%d)",
            wait_time, call_time, len(prompt), max_tokens,
        )
        return resp.json().get("response", "").strip()
    except Exception:
        total_time = time.monotonic() - queue_start
        logger.exception("Ollama query failed after %.1fs total (wait + call)", total_time)
        return None


def _query_claude(prompt: str, max_tokens: int, timeout: int = LLM_TIMEOUT_SECONDS) -> Optional[str]:
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=timeout)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    except Exception:
        logger.exception("Claude API query failed")
        return None