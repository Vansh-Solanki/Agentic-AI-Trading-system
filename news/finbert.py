import logging
import threading

logger = logging.getLogger(__name__)

_pipeline = None
_pipeline_lock = threading.Lock()


def get_finbert_pipeline():
    """
    Returns the singleton FinBERT pipeline, loading it on first call.

    Double-checked locking: the orchestrator runs many agents concurrently
    via ThreadPoolExecutor, so multiple threads can reach this function
    before the singleton is set. The lock ensures only one thread triggers
    the (slow) model load; the outer None-check keeps the common case
    (already loaded) lock-free. Callers should still pre-warm this once on
    the main thread at startup so the lock is never actually contended.
    """
    global _pipeline
    if _pipeline is None:
        with _pipeline_lock:
            if _pipeline is None:
                try:
                    from transformers import pipeline
                    _pipeline = pipeline(
                        "text-classification",
                        model="ProsusAI/finbert",
                        tokenizer="ProsusAI/finbert",
                        top_k=None,
                    )
                    logger.info("FinBERT pipeline loaded")
                except ImportError:
                    logger.warning(
                        "transformers not installed — FinBERT unavailable, "
                        "sentiment scoring will fall back to neutral (0.0)."
                    )
                except Exception:
                    # ProsusAI/finbert's main branch has no safetensors weights;
                    # transformers/huggingface_hub silently falls back to an
                    # auto-discovered, non-merged PR branch (refs/pr/29 as of
                    # Session 34) to find compatible weights — a mechanism this
                    # app has no control over. If that PR is closed, squashed, or
                    # force-pushed, loading can fail for any reason (revision
                    # resolution, network, format mismatch), not just a missing
                    # package. Catching broadly here — not just ImportError — is
                    # what actually prevents that from crashing the whole process
                    # during the startup pre-warm in run_paper.py, before any
                    # trading has started; score_sentiment() already degrades to
                    # a neutral 0.0 score whenever this returns None.
                    logger.exception(
                        "FinBERT pipeline failed to load (model=ProsusAI/finbert) — "
                        "sentiment scoring will fall back to neutral (0.0) for this run."
                    )
    return _pipeline


def score_sentiment(text: str) -> float:
    """
    Returns a sentiment score in [-1.0, 1.0].
    Positive = bullish, negative = bearish.
    Falls back to 0.0 if FinBERT is unavailable.
    """
    pipe = get_finbert_pipeline()
    if pipe is None:
        return 0.0

    truncated = text[:512]
    try:
        results = pipe(truncated)[0]
        scores = {r["label"]: r["score"] for r in results}
        return scores.get("positive", 0.0) - scores.get("negative", 0.0)
    except Exception:
        logger.exception("FinBERT inference failed")
        return 0.0


def score_batch(texts: list[str]) -> list[float]:
    return [score_sentiment(t) for t in texts]
