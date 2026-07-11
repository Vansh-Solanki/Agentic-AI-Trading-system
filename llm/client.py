import logging
import threading
import requests
from core.config import OLLAMA_BASE_URL, OLLAMA_MODEL, ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

# Session 34 addendum (found during live verification, same pattern as the
# Finnhub fix): Ollama serializes generation on a single model instance. With
# up to 10 tickers x multiple news/critic layers all calling query_llm()
# concurrently within one Phase-1 cycle, requests queue behind each other and
# tail requests blow through both Ollama's 60s read timeout and the
# orchestrator's 30s Phase-1 agent timeout. Capping concurrent Ollama calls
# doesn't reduce total work, but keeps queuing civil instead of every thread
# opening its own 60s-timeout connection at once.
_ollama_semaphore = threading.Semaphore(2)


def query_llm(prompt: str, max_tokens: int = 512) -> str:
    """
    Send a prompt to the configured LLM backend.
    Uses Ollama by default; falls back to Claude API if ANTHROPIC_API_KEY is set.
    """
    if ANTHROPIC_API_KEY:
        return _query_claude(prompt, max_tokens)
    return _query_ollama(prompt, max_tokens)


def _query_ollama(prompt: str, max_tokens: int) -> str:
    url = f"{OLLAMA_BASE_URL}/api/generate"
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": max_tokens},
    }
    try:
        with _ollama_semaphore:
            resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception:
        logger.exception("Ollama query failed")
        return "[LLM unavailable]"


def _query_claude(prompt: str, max_tokens: int) -> str:
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    except Exception:
        logger.exception("Claude API query failed")
        return "[LLM unavailable]"
