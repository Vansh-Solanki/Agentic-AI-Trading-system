import numpy as np


def sigmoid_conf(x: float, steepness: float) -> float:
    """
    Generic sigmoid confidence helper, bounded in (0, 1).
    x should already be the signed distance from whatever center point
    the caller cares about. Positive x -> confidence rises toward 1.
    """
    return 1 / (1 + np.exp(-steepness * x))


def rsi_buy_confidence(rsi_val: float, center: float = 33.0, steepness: float = 0.18) -> float:
    """
    Confidence that RSI supports a BUY (oversold) signal.
    Centered just past the classic 30 threshold so RSI=30 reads as
    meaningfully oversold (~0.60-0.65 conf) rather than a 50/50 midpoint.
    Confidence crosses 0.50 at rsi_val == center (33).
    """
    return sigmoid_conf(center - rsi_val, steepness)


def rsi_sell_confidence(rsi_val: float, center: float = 67.0, steepness: float = 0.18) -> float:
    """
    Confidence that RSI supports a SELL (overbought) signal.
    Centered just before the classic 70 threshold so RSI=70 reads as
    meaningfully overbought (~0.60-0.65 conf) rather than a 50/50 midpoint.
    Confidence crosses 0.50 at rsi_val == center (67).
    """
    return sigmoid_conf(rsi_val - center, steepness)


def stoch_rsi_buy_confidence(k: float, center: float = 16.0, steepness: float = 0.18) -> float:
    """
    Confidence that Stochastic RSI %K supports a BUY (oversold) signal.
    Centered just past the classic 20 threshold so %K=20 reads as
    meaningfully oversold. Confidence crosses 0.50 at k == center (16).
    """
    return sigmoid_conf(center - k, steepness)


def stoch_rsi_sell_confidence(k: float, center: float = 84.0, steepness: float = 0.18) -> float:
    """
    Confidence that Stochastic RSI %K supports a SELL (overbought) signal.
    Centered just before the classic 80 threshold so %K=80 reads as
    meaningfully overbought. Confidence crosses 0.50 at k == center (84).
    """
    return sigmoid_conf(k - center, steepness)


def bb_buy_confidence(close: float, lower: float, upper: float, steepness: float = 3.0) -> float:
    """
    Confidence that Bollinger Band position supports a BUY (oversold) signal.
    Uses %B (percent bandwidth): 0 = at lower band, 1 = at upper band.
    Confidence crosses 0.50 at the lower band (%B = 0) and rises as price
    moves further below the band — symmetric to RSI/StochRSI pattern.
    """
    band_width = upper - lower
    if band_width <= 0:
        return 0.0
    pct_b = (close - lower) / band_width
    return sigmoid_conf(-pct_b, steepness)


def bb_sell_confidence(close: float, lower: float, upper: float, steepness: float = 3.0) -> float:
    """
    Confidence that Bollinger Band position supports a SELL (overbought) signal.
    Confidence crosses 0.50 at the upper band (%B = 1) and rises as price
    moves further above the band.
    """
    band_width = upper - lower
    if band_width <= 0:
        return 0.0
    pct_b = (close - lower) / band_width
    return sigmoid_conf(pct_b - 1.0, steepness)


def adx_trend_confidence(adx_val: float, center: float = 25.0, steepness: float = 0.10) -> float:
    """
    Confidence that ADX magnitude supports a trending-market signal.
    Threshold at center=25 (classic trending cutoff): ADX=25 → 0.50.
    ADX=30 → ~0.62, ADX=40 → ~0.82, ADX=50 → ~0.92.
    Only meaningful when adx.is_trending is True (ADX > 25).
    """
    return sigmoid_conf(adx_val - center, steepness)