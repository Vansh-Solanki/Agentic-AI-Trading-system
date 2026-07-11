import json
import logging
from pathlib import Path
from core.config import TradingMode

logger = logging.getLogger(__name__)

_SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"
_SNAPSHOTS_DIR.mkdir(exist_ok=True)


def _path(ticker: str, mode: TradingMode) -> Path:
    return _SNAPSHOTS_DIR / f"{ticker}_{mode.value}.json"


def load_memory(ticker: str, mode: TradingMode) -> dict:
    p = _path(ticker, mode)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to load memory for %s [%s]", ticker, mode.value)
        return {}


def save_memory(ticker: str, mode: TradingMode, data: dict) -> None:
    p = _path(ticker, mode)
    try:
        p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    except Exception:
        logger.exception("Failed to save memory for %s [%s]", ticker, mode.value)


def update_memory(ticker: str, mode: TradingMode, key: str, value: object) -> None:
    mem = load_memory(ticker, mode)
    mem[key] = value
    save_memory(ticker, mode, mem)


def clear_memory(ticker: str, mode: TradingMode) -> None:
    p = _path(ticker, mode)
    if p.exists():
        p.unlink()
