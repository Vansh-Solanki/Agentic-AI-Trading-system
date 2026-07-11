#!/usr/bin/env python3
"""
Weekly decision weight update job.
Usage: python scripts/update_weights.py

Confidence-weighted accuracy per signal source per trading mode.
Blends computed candidate weights with current weights: 70% old + 30% new.
Re-scores ALL closed trades on every run (idempotent).
"""
import sys
import os
import json
import logging
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

from data.storage.db import init_schema
from data.storage.decision_store import get_closed_trades_for_weights, get_agent_weights, save_agent_weights

_INTRADAY_DEFAULTS = {"technical": 0.35, "news": 0.25, "regime": 0.10, "memory": 0.10, "fundamental": 0.20}
_LONGTERM_DEFAULTS = {"technical": 0.20, "news": 0.15, "regime": 0.15, "memory": 0.10, "fundamental": 0.40}
_DEFAULTS = {"intraday": _INTRADAY_DEFAULTS, "long_term": _LONGTERM_DEFAULTS}
_MODES = ["intraday", "long_term"]
_SOURCES = ["technical", "news", "fundamental", "regime", "memory"]
_BUY_TYPES = {"buy", "strong_buy"}
_SELL_TYPES = {"sell", "strong_sell"}
_BLEND = 0.30


def compute_candidate_weights(trades: list[dict]) -> dict[str, float] | None:
    raw_scores: dict[str, float] = {s: 0.0 for s in _SOURCES}
    used = 0

    for trade in trades:
        pnl = trade.get("pnl")
        snapshot_json = trade.get("signal_snapshot")
        if pnl is None or snapshot_json is None:
            continue
        try:
            snapshot = json.loads(snapshot_json)
        except (json.JSONDecodeError, TypeError):
            continue

        trade_outcome = 1 if pnl > 0 else -1
        used += 1

        for sig in snapshot:
            source = sig.get("source")
            stype = sig.get("signal_type", "")
            conf = float(sig.get("confidence", 0.0))
            if source not in _SOURCES:
                continue
            if stype in _BUY_TYPES:
                vote = 1
            elif stype in _SELL_TYPES:
                vote = -1
            else:
                continue
            raw_scores[source] += conf * vote * trade_outcome

    if used == 0:
        logger.info("No usable trades found.")
        return None

    min_score = min(raw_scores.values())
    shifted = {s: v - min_score for s, v in raw_scores.items()}
    total = sum(shifted.values())

    if total == 0:
        return {s: 1.0 / len(_SOURCES) for s in _SOURCES}

    return {s: v / total for s, v in shifted.items()}


def blend_weights(old: dict[str, float], candidate: dict[str, float]) -> dict[str, float]:
    all_sources = set(old) | set(candidate)
    blended = {s: (1 - _BLEND) * old.get(s, 0.0) + _BLEND * candidate.get(s, 0.0)
               for s in all_sources}
    total = sum(blended.values())
    if total > 0:
        blended = {s: v / total for s, v in blended.items()}
    return blended


def run_update():
    init_schema()
    for mode in _MODES:
        trades = get_closed_trades_for_weights(mode)
        logger.info("[%s] %d closed trades with snapshots available", mode, len(trades))

        candidate = compute_candidate_weights(trades)
        if candidate is None:
            logger.info("[%s] Skipping — no usable data", mode)
            continue

        current = get_agent_weights(mode) or _DEFAULTS[mode].copy()
        new_weights = blend_weights(current, candidate)
        save_agent_weights(mode, new_weights)

        for source in sorted(new_weights):
            logger.info("[%s] %-12s  %.4f → %.4f", mode, source,
                        current.get(source, 0.0), new_weights[source])

    logger.info("Weight update complete.")


if __name__ == "__main__":
    run_update()
