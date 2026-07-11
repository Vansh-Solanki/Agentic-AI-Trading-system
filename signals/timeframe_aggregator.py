import pandas as pd
from core.constants import Timeframe, SignalType, SignalSource
from core.models import Signal
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
from signals.patterns.chart_patterns import (
    detect_double_bottom, detect_double_top,
    detect_head_and_shoulders, detect_inverse_head_and_shoulders,
    detect_bull_flag, detect_bear_flag,
    detect_bull_pennant, detect_bear_pennant,
    detect_ascending_triangle, detect_descending_triangle,
    detect_range_breakout, detect_range_breakdown,
    detect_regression_channel_breakout,
)


_MIN_FLOOR = 0.50   # signals below this confidence are treated as noise and not emitted


def aggregate_signals(ticker: str, df: pd.DataFrame, timeframe: Timeframe) -> list[Signal]:
    """Run all indicator and pattern checks and return a list of Signals."""
    signals: list[Signal] = []

    # ── RSI (continuous / fuzzy scoring) ─────────────────────────────────────
    rsi = calculate_rsi(df)
    rsi_val   = float(rsi.iloc[-1])
    buy_conf  = rsi_buy_confidence(rsi_val)
    sell_conf = rsi_sell_confidence(rsi_val)
    if buy_conf > sell_conf and buy_conf > _MIN_FLOOR:
        signals.append(_sig(ticker, timeframe, SignalType.BUY, round(buy_conf, 3), rsi_val,
                            f"RSI {rsi_val:.1f} — oversold confidence {buy_conf:.2f}"))
    elif sell_conf > buy_conf and sell_conf > _MIN_FLOOR:
        signals.append(_sig(ticker, timeframe, SignalType.SELL, round(sell_conf, 3), rsi_val,
                            f"RSI {rsi_val:.1f} — overbought confidence {sell_conf:.2f}"))

    # ── Stochastic RSI (continuous / fuzzy scoring) ───────────────────────────
    if len(df) >= 50:
        srsi        = get_stoch_rsi(df)
        s_buy_conf  = stoch_rsi_buy_confidence(srsi.k)
        s_sell_conf = stoch_rsi_sell_confidence(srsi.k)
        # %K/%D cross acts as a confirmation bump, not a separate binary gate
        if srsi.k > srsi.d:
            s_buy_conf  = min(1.0, s_buy_conf + 0.05)
        elif srsi.k < srsi.d:
            s_sell_conf = min(1.0, s_sell_conf + 0.05)
        if s_buy_conf > s_sell_conf and s_buy_conf > _MIN_FLOOR:
            signals.append(_sig(ticker, timeframe, SignalType.BUY, round(s_buy_conf, 3), srsi.k,
                                f"Stoch RSI K={srsi.k:.1f} D={srsi.d:.1f} — oversold confidence {s_buy_conf:.2f}"))
        elif s_sell_conf > s_buy_conf and s_sell_conf > _MIN_FLOOR:
            signals.append(_sig(ticker, timeframe, SignalType.SELL, round(s_sell_conf, 3), srsi.k,
                                f"Stoch RSI K={srsi.k:.1f} D={srsi.d:.1f} — overbought confidence {s_sell_conf:.2f}"))

    # ── MACD ──────────────────────────────────────────────────────────────────
    macd_line, sig_line, hist = calculate_macd(df)
    if len(hist) >= 2:
        if float(hist.iloc[-2]) < 0 < float(hist.iloc[-1]):
            signals.append(_sig(ticker, timeframe, SignalType.BUY, 0.65, float(hist.iloc[-1]),
                                "MACD bullish crossover"))
        elif float(hist.iloc[-2]) > 0 > float(hist.iloc[-1]):
            signals.append(_sig(ticker, timeframe, SignalType.SELL, 0.65, float(hist.iloc[-1]),
                                "MACD bearish crossover"))

    # ── Bollinger Bands (continuous / fuzzy scoring) ──────────────────────────
    upper, mid, lower = calculate_bollinger_bands(df)
    close     = float(df["Close"].iloc[-1])
    bb_upper  = float(upper.iloc[-1])
    bb_lower  = float(lower.iloc[-1])
    bb_b_conf = bb_buy_confidence(close, bb_lower, bb_upper)
    bb_s_conf = bb_sell_confidence(close, bb_lower, bb_upper)
    if bb_b_conf > bb_s_conf and bb_b_conf > _MIN_FLOOR:
        signals.append(_sig(ticker, timeframe, SignalType.BUY, round(bb_b_conf, 3), close,
                            f"Price below lower Bollinger Band — oversold confidence {bb_b_conf:.2f}"))
    elif bb_s_conf > bb_b_conf and bb_s_conf > _MIN_FLOOR:
        signals.append(_sig(ticker, timeframe, SignalType.SELL, round(bb_s_conf, 3), close,
                            f"Price above upper Bollinger Band — overbought confidence {bb_s_conf:.2f}"))

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

    # ── ADX / DMI — trend strength filter (continuous / fuzzy scoring) ───────
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
        else:
            # Weak ADX — suppress trend-following signals by adding a neutral dampener
            signals.append(_sig(ticker, timeframe, SignalType.NEUTRAL, 0.4, adx.adx,
                                f"ADX={adx.adx:.1f} — no clear trend, range-bound conditions"))

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

    # ── Chart patterns ────────────────────────────────────────────────────────
    if detect_double_bottom(df):
        signals.append(_sig(ticker, timeframe, SignalType.BUY, 0.7, None, "Double bottom pattern"))
    if detect_double_top(df):
        signals.append(_sig(ticker, timeframe, SignalType.SELL, 0.7, None, "Double top pattern"))

    if detect_head_and_shoulders(df):
        signals.append(_sig(ticker, timeframe, SignalType.STRONG_SELL, 0.75, None,
                            "Head and shoulders — bearish reversal"))
    if detect_inverse_head_and_shoulders(df):
        signals.append(_sig(ticker, timeframe, SignalType.STRONG_BUY, 0.75, None,
                            "Inverse head and shoulders — bullish reversal"))

    if detect_bull_flag(df):
        signals.append(_sig(ticker, timeframe, SignalType.BUY, 0.65, None, "Bull flag breakout"))
    if detect_bear_flag(df):
        signals.append(_sig(ticker, timeframe, SignalType.SELL, 0.65, None, "Bear flag breakdown"))

    if detect_bull_pennant(df):
        signals.append(_sig(ticker, timeframe, SignalType.BUY, 0.65, None, "Bull pennant breakout"))
    if detect_bear_pennant(df):
        signals.append(_sig(ticker, timeframe, SignalType.SELL, 0.65, None, "Bear pennant breakdown"))

    if detect_ascending_triangle(df):
        signals.append(_sig(ticker, timeframe, SignalType.BUY, 0.7, None,
                            "Ascending triangle — flat resistance, rising lows"))
    if detect_descending_triangle(df):
        signals.append(_sig(ticker, timeframe, SignalType.SELL, 0.7, None,
                            "Descending triangle — flat support, falling highs"))

    if detect_range_breakout(df):
        signals.append(_sig(ticker, timeframe, SignalType.BUY, 0.7, None,
                            "Range breakout — price cleared consolidation ceiling"))
    if detect_range_breakdown(df):
        signals.append(_sig(ticker, timeframe, SignalType.SELL, 0.7, None,
                            "Range breakdown — price broke below consolidation floor"))

    ch = detect_regression_channel_breakout(df)
    if ch.direction == 2 and ch.confidence > _MIN_FLOOR:
        signals.append(_sig(ticker, timeframe, SignalType.BUY, ch.confidence, None,
                            f"Regression channel bullish breakout "
                            f"(R²_res={ch.r2_high:.3f}, R²_sup={ch.r2_low:.3f}, conf={ch.confidence:.3f})"))
    elif ch.direction == 1 and ch.confidence > _MIN_FLOOR:
        signals.append(_sig(ticker, timeframe, SignalType.SELL, ch.confidence, None,
                            f"Regression channel bearish breakdown "
                            f"(R²_res={ch.r2_high:.3f}, R²_sup={ch.r2_low:.3f}, conf={ch.confidence:.3f})"))

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
