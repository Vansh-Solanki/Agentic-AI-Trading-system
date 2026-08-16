import logging
import threading
from typing import Optional
import requests
from core.config import OLLAMA_BASE_URL, OLLAMA_MODEL, ANTHROPIC_API_KEY, LLM_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

# Session 34 addendum (found during live verification, same pattern as the
# Finnhub fix): Ollama serializes generation on a single model instance. With
# up to 10 tickers x multiple news/critic layers all calling query_llm()
# concurrently within one Phase-1 cycle, requests queue behind each other and
# tail requests blow through both Ollama's 60s read timeout and the
# orchestrator's 30s Phase-1 agent timeout. Capping concurrent Ollama calls
# doesn't reduce total work, but keeps queuing civil instead of every thread
# opening its own 60s-timeout connection at once.
_ollama_semaphore = threading.Semaphore(1)


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


def _query_ollama(prompt: str, max_tokens: int, timeout: int = LLM_TIMEOUT_SECONDS) -> Optional[str]:
    url = f"{OLLAMA_BASE_URL}/api/generate"
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": max_tokens},
    }
    try:
        with _ollama_semaphore:
            resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception:
        logger.exception("Ollama query failed")
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