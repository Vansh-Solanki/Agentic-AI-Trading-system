import pandas as pd
from core.config import (
    RSI_LLM_ZONE_LOW, RSI_LLM_ZONE_HIGH,
    STOCH_RSI_LLM_ZONE_LOW, STOCH_RSI_LLM_ZONE_HIGH,
    BB_LLM_ZONE_LOW_PCT_B, BB_LLM_ZONE_HIGH_PCT_B,
    RSI_LLM_DELTA_THRESHOLD, STOCH_RSI_LLM_DELTA_THRESHOLD,
    ADX_LLM_DELTA_THRESHOLD, BB_LLM_DELTA_THRESHOLD_PCT_B,
    LLM_INDICATOR_VOTE_CONFIDENCE,
)
from core.constants import Timeframe, SignalType, SignalSource
from core.models import Signal
from data.storage.llm_indicator_cache_store import get_cached_verdict, save_verdict
from llm.client import query_llm
from llm.prompts.indicator_escalation import build_combined_indicator_verdict_prompt
from llm.response_parser import parse_indicator_verdicts
from signals.indicators.momentum import calculate_rsi, calculate_macd, get_stoch_rsi
from signals.indicators.scoring import (
    rsi_buy_confidence, rsi_sell_confidence,
    stoch_rsi_buy_confidence, stoch_rsi_sell_confidence,
    bb_buy_confidence, bb_sell_confidence,
    adx_trend_confidence,
)
from signals.indicators.volatility import calculate_bollinger_bands
from signals.indicators.volume import volume_spike
from signals.indicators.trend import trend_direction, calculate_ema_stack, calculate_adx
from signals.patterns.candlestick import (
    detect_hammer, detect_shooting_star,
    detect_bullish_engulfing, detect_bearish_engulfing, detect_doji,
)


_MIN_FLOOR = 0.50   # signals below this confidence are treated as noise and not emitted


def aggregate_signals(
    ticker: str,
    df: pd.DataFrame,
    timeframe: Timeframe,
    *,
    enable_llm_escalation: bool = False,
    is_canonical: bool = True,
    inherited_llm_verdicts: dict[str, str] | None = None,
    llm_verdicts_out: dict[str, str] | None = None,
) -> list[Signal]:
    """
    Run all indicator and pattern checks and return a list of Signals.

    enable_llm_escalation (Session 45, default off): when True, RSI/Stoch
    RSI/Bollinger/ADX readings that fall in the ambiguous middle zone get a
    genuine LLM buy/sell/hold verdict instead of being silently skipped
    (RSI/Stoch RSI/Bollinger) or defaulting to a flat NEUTRAL (ADX). Default
    is False so existing callers (backtesting/replay.py) are unaffected —
    this is a live-agent behavior, not a backtest one.

    is_canonical / inherited_llm_verdicts / llm_verdicts_out implement the
    canonical-timeframe call-volume gate (locked Option B): only the
    canonical timeframe for the mode is allowed to actually call the LLM;
    other timeframes inherit its verdict per indicator via
    inherited_llm_verdicts instead of calling it themselves. The canonical
    caller should pass an empty dict as llm_verdicts_out to collect verdicts
    for the other timeframes to inherit afterward.
    """
    signals: list[Signal] = []
    # Session 47 item 11: ambiguous-zone indicators are collected here instead
    # of each calling the LLM individually — one combined call fires after all
    # four indicator checks below, instead of up to four separate ones.
    pending_llm: list[dict] = []

    # ── RSI ────────────────────────────────────────────────────────────────
    rsi = calculate_rsi(df)
    rsi_val = float(rsi.iloc[-1])
    if rsi_val < RSI_LLM_ZONE_LOW:
        buy_conf = round(rsi_buy_confidence(rsi_val), 3)
        if buy_conf > _MIN_FLOOR:
            signals.append(_sig(ticker, timeframe, SignalType.BUY, buy_conf, rsi_val,
                                f"RSI {rsi_val:.1f} — oversold confidence {buy_conf:.2f}"))
    elif rsi_val > RSI_LLM_ZONE_HIGH:
        sell_conf = round(rsi_sell_confidence(rsi_val), 3)
        if sell_conf > _MIN_FLOOR:
            signals.append(_sig(ticker, timeframe, SignalType.SELL, sell_conf, rsi_val,
                                f"RSI {rsi_val:.1f} — overbought confidence {sell_conf:.2f}"))
    elif enable_llm_escalation:
        pending_llm.append({
            "key": "rsi", "value": rsi_val, "signal_value": rsi_val,
            "label": f"RSI {rsi_val:.1f}",
            "description": (
                f"RSI (14): {rsi_val:.1f} — inside the {RSI_LLM_ZONE_LOW:.0f}-{RSI_LLM_ZONE_HIGH:.0f} ambiguous band, "
                f"no clear oversold/overbought reading by the deterministic rule."
            ),
            "delta_threshold": RSI_LLM_DELTA_THRESHOLD,
        })

    # ── Stochastic RSI ─────────────────────────────────────────────────────
    if len(df) >= 50:
        srsi = get_stoch_rsi(df)
        if srsi.k < STOCH_RSI_LLM_ZONE_LOW:
            s_buy_conf = stoch_rsi_buy_confidence(srsi.k)
            if srsi.k > srsi.d:
                s_buy_conf = min(1.0, s_buy_conf + 0.05)
            s_buy_conf = round(s_buy_conf, 3)
            if s_buy_conf > _MIN_FLOOR:
                signals.append(_sig(ticker, timeframe, SignalType.BUY, s_buy_conf, srsi.k,
                                    f"Stoch RSI K={srsi.k:.1f} D={srsi.d:.1f} — oversold confidence {s_buy_conf:.2f}"))
        elif srsi.k > STOCH_RSI_LLM_ZONE_HIGH:
            s_sell_conf = stoch_rsi_sell_confidence(srsi.k)
            if srsi.k < srsi.d:
                s_sell_conf = min(1.0, s_sell_conf + 0.05)
            s_sell_conf = round(s_sell_conf, 3)
            if s_sell_conf > _MIN_FLOOR:
                signals.append(_sig(ticker, timeframe, SignalType.SELL, s_sell_conf, srsi.k,
                                    f"Stoch RSI K={srsi.k:.1f} D={srsi.d:.1f} — overbought confidence {s_sell_conf:.2f}"))
        elif enable_llm_escalation:
            pending_llm.append({
                "key": "stoch_rsi", "value": srsi.k, "signal_value": srsi.k,
                "label": f"Stoch RSI K={srsi.k:.1f} D={srsi.d:.1f}",
                "description": (
                    f"Stoch RSI: K={srsi.k:.1f}, D={srsi.d:.1f} — inside the "
                    f"{STOCH_RSI_LLM_ZONE_LOW:.0f}-{STOCH_RSI_LLM_ZONE_HIGH:.0f} ambiguous band, "
                    f"no clear oversold/overbought reading by the deterministic rule."
                ),
                "delta_threshold": STOCH_RSI_LLM_DELTA_THRESHOLD,
            })

    # ── MACD ──────────────────────────────────────────────────────────────────
    macd_line, sig_line, hist = calculate_macd(df)
    if len(hist) >= 2:
        if float(hist.iloc[-2]) < 0 < float(hist.iloc[-1]):
            signals.append(_sig(ticker, timeframe, SignalType.BUY, 0.65, float(hist.iloc[-1]),
                                "MACD bullish crossover"))
        elif float(hist.iloc[-2]) > 0 > float(hist.iloc[-1]):
            signals.append(_sig(ticker, timeframe, SignalType.SELL, 0.65, float(hist.iloc[-1]),
                                "MACD bearish crossover"))

    # ── Bollinger Bands ────────────────────────────────────────────────────
    upper, mid, lower = calculate_bollinger_bands(df)
    close      = float(df["Close"].iloc[-1])
    bb_upper   = float(upper.iloc[-1])
    bb_lower   = float(lower.iloc[-1])
    bb_width   = bb_upper - bb_lower
    pct_b      = (close - bb_lower) / bb_width if bb_width > 0 else 0.5
    if pct_b < BB_LLM_ZONE_LOW_PCT_B:
        bb_b_conf = round(bb_buy_confidence(close, bb_lower, bb_upper), 3)
        if bb_b_conf > _MIN_FLOOR:
            signals.append(_sig(ticker, timeframe, SignalType.BUY, bb_b_conf, close,
                                f"Price below lower Bollinger Band (%B={pct_b:.2f}) — oversold confidence {bb_b_conf:.2f}"))
    elif pct_b > BB_LLM_ZONE_HIGH_PCT_B:
        bb_s_conf = round(bb_sell_confidence(close, bb_lower, bb_upper), 3)
        if bb_s_conf > _MIN_FLOOR:
            signals.append(_sig(ticker, timeframe, SignalType.SELL, bb_s_conf, close,
                                f"Price above upper Bollinger Band (%B={pct_b:.2f}) — overbought confidence {bb_s_conf:.2f}"))
    elif enable_llm_escalation:
        pending_llm.append({
            "key": "bollinger", "value": pct_b, "signal_value": close,
            "label": f"Bollinger %B={pct_b:.2f}",
            "description": (
                f"Bollinger Bands: price {close:.2f}, lower {bb_lower:.2f}, upper {bb_upper:.2f}, "
                f"%B={pct_b:.2f} — inside the middle/hold zone, not near either band."
            ),
            "delta_threshold": BB_LLM_DELTA_THRESHOLD_PCT_B,
        })

    # ── EMA Stack (9 / 20 / 50 / 200) ────────────────────────────────────────
    if len(df) >= 30:
        ema_now, ema_prev = calculate_ema_stack(df)

        if ema_now.fully_bullish:
            signals.append(_sig(ticker, timeframe, SignalType.STRONG_BUY, 0.8, ema_now.ema9,
                                f"Full EMA stack bullish: price > EMA9 > EMA20 > EMA50 > EMA200"))
        elif ema_now.fully_bearish:
            signals.append(_sig(ticker, timeframe, SignalType.STRONG_SELL, 0.8, ema_now.ema9,
                                f"Full EMA stack bearish: price < EMA9 < EMA20 < EMA50 < EMA200"))
        elif ema_now.bullish_count >= 3:
            signals.append(_sig(ticker, timeframe, SignalType.BUY, 0.6, float(ema_now.bullish_count),
                                f"Bullish EMA alignment ({ema_now.bullish_count}/4 EMAs below price)"))
        elif ema_now.bearish_count >= 3:
            signals.append(_sig(ticker, timeframe, SignalType.SELL, 0.6, float(ema_now.bearish_count),
                                f"Bearish EMA alignment ({ema_now.bearish_count}/4 EMAs above price)"))

        # Golden / Death cross — high-conviction, low-frequency
        if len(df) >= 205 and ema_now.golden_cross(ema_prev):
            signals.append(_sig(ticker, timeframe, SignalType.STRONG_BUY, 0.85, ema_now.ema50,
                                "Golden cross: EMA50 crossed above EMA200"))
        elif len(df) >= 205 and ema_now.death_cross(ema_prev):
            signals.append(_sig(ticker, timeframe, SignalType.STRONG_SELL, 0.85, ema_now.ema50,
                                "Death cross: EMA50 crossed below EMA200"))

    # ── ADX / DMI — trend strength filter ─────────────────────────────────
    if len(df) >= 30:
        adx = calculate_adx(df)
        if adx.is_trending:
            conf = round(adx_trend_confidence(adx.adx), 3)
            if adx.bullish:
                signals.append(_sig(ticker, timeframe, SignalType.BUY, conf, adx.adx,
                                    f"ADX={adx.adx:.1f} trending bullish (+DI={adx.plus_di:.1f} > -DI={adx.minus_di:.1f}), conf={conf:.2f}"))
            else:
                signals.append(_sig(ticker, timeframe, SignalType.SELL, conf, adx.adx,
                                    f"ADX={adx.adx:.1f} trending bearish (-DI={adx.minus_di:.1f} > +DI={adx.plus_di:.1f}), conf={conf:.2f}"))
        elif enable_llm_escalation:
            pending_llm.append({
                "key": "adx", "value": adx.adx, "signal_value": adx.adx,
                "label": f"ADX={adx.adx:.1f}",
                "description": (
                    f"ADX={adx.adx:.1f} (below the trending threshold), +DI={adx.plus_di:.1f}, -DI={adx.minus_di:.1f} "
                    f"— no clear directional trend by the deterministic rule."
                ),
                "delta_threshold": ADX_LLM_DELTA_THRESHOLD,
            })
        else:
            # No escalation path available (e.g. backtesting) — same flat
            # dampener signal this branch has always emitted.
            signals.append(_sig(ticker, timeframe, SignalType.NEUTRAL, 0.4, adx.adx,
                                f"ADX={adx.adx:.1f} — no clear trend, range-bound conditions"))

    # ── Combined LLM escalation call (Session 47 item 11) ───────────────────
    # One call for every indicator that fell ambiguous this cycle, instead of
    # each of the four branches above calling the LLM on its own.
    if pending_llm:
        verdicts = _llm_indicator_votes_batch(
            ticker, timeframe, is_canonical, inherited_llm_verdicts, llm_verdicts_out, pending_llm,
        )
        for item in pending_llm:
            verdict = verdicts.get(item["key"], "hold")
            if verdict == "buy":
                signals.append(_sig(ticker, timeframe, SignalType.BUY, LLM_INDICATOR_VOTE_CONFIDENCE, item["signal_value"],
                                    f"{item['label']} — ambiguous zone, LLM verdict BUY"))
            elif verdict == "sell":
                signals.append(_sig(ticker, timeframe, SignalType.SELL, LLM_INDICATOR_VOTE_CONFIDENCE, item["signal_value"],
                                    f"{item['label']} — ambiguous zone, LLM verdict SELL"))

    # ── Legacy trend direction (EMA20 vs EMA50) ───────────────────────────────
    trend = trend_direction(df)
    if trend == "up":
        signals.append(_sig(ticker, timeframe, SignalType.BUY, 0.55, None, "Uptrend: EMA20 > EMA50"))
    elif trend == "down":
        signals.append(_sig(ticker, timeframe, SignalType.SELL, 0.55, None, "Downtrend: EMA20 < EMA50"))

    # ── Volume spike ──────────────────────────────────────────────────────────
    if volume_spike(df):
        signals.append(_sig(ticker, timeframe, SignalType.NEUTRAL, 0.5, None,
                            "Volume spike detected — increased interest"))

    # ── Candlestick patterns ──────────────────────────────────────────────────
    if detect_hammer(df):
        signals.append(_sig(ticker, timeframe, SignalType.BUY, 0.6, None, "Hammer candle"))
    if detect_shooting_star(df):
        signals.append(_sig(ticker, timeframe, SignalType.SELL, 0.6, None, "Shooting star candle"))
    if detect_bullish_engulfing(df):
        signals.append(_sig(ticker, timeframe, SignalType.BUY, 0.65, None, "Bullish engulfing"))
    if detect_bearish_engulfing(df):
        signals.append(_sig(ticker, timeframe, SignalType.SELL, 0.65, None, "Bearish engulfing"))
    if detect_doji(df):
        signals.append(_sig(ticker, timeframe, SignalType.NEUTRAL, 0.5, None,
                            "Doji candle — indecision, open ≈ close"))

    # Chart patterns (double top/bottom, H&S, flags, pennants, triangles,
    # range breakout/breakdown, regression channel breakout) removed Session
    # 45 — flagged as producing noise, out of scope for the LLM-escalation
    # work, deleted from this function entirely.

    return signals


def _sig(ticker: str, tf: Timeframe, stype: SignalType, conf: float,
         value: float | None, reasoning: str) -> Signal:
    return Signal(
        ticker=ticker,
        source=SignalSource.TECHNICAL,
        signal_type=stype,
        timeframe=tf,
        confidence=conf,
        value=value,
        reasoning=reasoning,
    )


def _llm_indicator_votes_batch(
    ticker: str,
    timeframe: Timeframe,
    is_canonical: bool,
    inherited_llm_verdicts: dict[str, str] | None,
    llm_verdicts_out: dict[str, str] | None,
    pending: list[dict],
) -> dict[str, str]:
    """
    Returns 'buy' / 'sell' / 'hold' per indicator key in `pending`.

    Non-canonical timeframes never call the LLM themselves (Session 45,
    locked Option B) — they inherit the canonical timeframe's verdict for
    each indicator, or 'hold' if the canonical timeframe didn't escalate it
    this cycle (e.g. its own reading wasn't ambiguous).

    Session 47 item 11: canonical timeframes make at most one combined
    query_llm() call here for every indicator not already cache-hit, instead
    of one call per indicator.
    """
    if not is_canonical:
        return {item["key"]: (inherited_llm_verdicts or {}).get(item["key"], "hold") for item in pending}

    results: dict[str, str] = {}
    to_call: list[dict] = []
    for item in pending:
        cached = get_cached_verdict(ticker, timeframe.value, item["key"], item["value"], item["delta_threshold"])
        if cached is not None:
            results[item["key"]] = cached
        else:
            to_call.append(item)

    if to_call:
        prompt = build_combined_indicator_verdict_prompt(
            ticker, timeframe.value, [(item["key"], item["description"]) for item in to_call],
        )
        resp = query_llm(prompt, max_tokens=32 * len(to_call))
        verdicts = (
            parse_indicator_verdicts(resp, tuple(item["key"] for item in to_call))
            if resp is not None
            else {item["key"]: "hold" for item in to_call}
        )
        for item in to_call:
            verdict = verdicts.get(item["key"], "hold")
            results[item["key"]] = verdict
            save_verdict(ticker, timeframe.value, item["key"], item["value"], verdict)

    if llm_verdicts_out is not None:
        llm_verdicts_out.update(results)
    return results
