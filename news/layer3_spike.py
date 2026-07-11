import logging
from core.models import Signal, NewsItem
from core.constants import SignalType, SignalSource, Timeframe
from data.feeds.price_feed import fetch_candles
from data.storage.news_store import save_news, save_news_event
from news.finbert import score_sentiment
from news.news_ingest import sentiment_label
from llm.client import query_llm
from llm.prompts.spike_analysis import build_spike_prompt
from signals.indicators.volatility import calculate_atr

logger = logging.getLogger(__name__)

FINBERT_ESCALATION_THRESHOLD = 0.5   # FinBERT magnitude to escalate an article to LLM
ATR_MOVE_MULTIPLIER           = 1.5  # current candle move must exceed this × ATR
VOLUME_SPIKE_MULTIPLIER       = 2.0  # current volume must exceed this × 20-bar avg


def _detect_market_event(ticker: str) -> tuple[bool, str]:
    """
    Returns (event_detected, description).
    Checks 5-min candles for: price move > 1.5x ATR  OR  volume > 2x 20-bar avg.
    """
    try:
        df = fetch_candles(ticker, Timeframe.M5)
    except Exception:
        logger.warning("Could not fetch 5m candles for spike gate on %s", ticker)
        return False, ""

    if df is None or len(df) < 21:
        return False, ""

    atr = calculate_atr(df)
    if atr is None:
        logger.warning("ATR unavailable for %s — skipping spike gate to avoid false triggers", ticker)
        return False, ""

    latest        = df.iloc[-1]
    candle_move   = abs(float(latest["Close"]) - float(latest["Open"]))
    avg_volume    = float(df["Volume"].iloc[-21:-1].mean())
    current_vol   = float(latest["Volume"])

    price_event  = candle_move  > ATR_MOVE_MULTIPLIER  * atr
    volume_event = current_vol  > VOLUME_SPIKE_MULTIPLIER * avg_volume

    if not (price_event or volume_event):
        return False, ""

    parts = []
    if price_event:
        parts.append(f"price move {candle_move:.2f} > {ATR_MOVE_MULTIPLIER}x ATR {atr:.2f}")
    if volume_event:
        parts.append(f"volume {current_vol:.0f} > {VOLUME_SPIKE_MULTIPLIER}x avg {avg_volume:.0f}")
    return True, " + ".join(parts)


def run_spike_detector(ticker: str, items: list[NewsItem]) -> list[Signal]:
    """
    Layer 3 — Intraday spike detector.
    Only activates when an abnormal price move (>1.5x ATR) or volume spike (>2x avg)
    is detected on the 5-min chart. If no market event, returns immediately without
    scoring any headlines (keeps calls to ~5-20/day instead of 390+).
    When a market event IS present, scores each headline with FinBERT and escalates
    high-magnitude articles to LLM. LLM response drives the signal direction.
    """
    event_detected, event_desc = _detect_market_event(ticker)
    if not event_detected:
        for item in items:
            item.layer = 3
        save_news(items)   # persist items for downstream layers even without escalation
        return []

    logger.info("Spike gate triggered for %s: %s", ticker, event_desc)

    signals: list[Signal] = []

    for item in items:
        item.layer = 3
        score = score_sentiment(item.headline + " " + item.summary)
        item.sentiment_score = score
        save_news_event(item, sentiment_score=score, sentiment_label=sentiment_label(score))

        if abs(score) >= FINBERT_ESCALATION_THRESHOLD:
            prompt   = build_spike_prompt(ticker, item.headline, score, event_desc)
            llm_resp = query_llm(prompt)

            # LLM response drives direction; FinBERT score provides confidence
            first_word = llm_resp.strip().split()[0].upper() if llm_resp.strip() else ""
            if first_word == "BUY":
                stype = SignalType.STRONG_BUY
            elif first_word == "SELL":
                stype = SignalType.STRONG_SELL
            else:
                # HOLD or unrecognised — only emit if FinBERT is decisive
                if score > 0.5:
                    stype = SignalType.BUY
                elif score < -0.5:
                    stype = SignalType.SELL
                else:
                    continue   # LLM says hold and FinBERT is ambiguous — skip

            signals.append(Signal(
                ticker=ticker,
                source=SignalSource.NEWS,
                signal_type=stype,
                timeframe=Timeframe.M5,
                confidence=min(abs(score), 1.0),
                value=score,
                reasoning=(
                    f"Spike [{event_desc}] | FinBERT={score:.2f} | "
                    f"{item.headline[:60]} | LLM: {llm_resp[:80]}"
                ),
                metadata={"layer": 3, "headline": item.headline, "llm": llm_resp, "event": event_desc},
            ))

    save_news(items)
    return signals
