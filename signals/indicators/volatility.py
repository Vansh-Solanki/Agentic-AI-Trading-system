import pandas as pd
import numpy as np


def calculate_bollinger_bands(
    df: pd.DataFrame, period: int = 20, std_dev: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (upper_band, middle_band, lower_band)."""
    close  = df["Close"].squeeze()
    mid    = close.rolling(period).mean()
    std    = close.rolling(period).std()
    upper  = mid + std_dev * std
    lower  = mid - std_dev * std
    return upper, mid, lower


def calculate_atr(df: pd.DataFrame, period: int = 14) -> float | None:
    """
    Returns the most recent ATR value, or None if the series is empty or the
    last value is NaN (fewer than `period` bars available).
    Callers must check for None — do NOT use a fallback magic number.
    """
    high  = df["High"].squeeze()
    low   = df["Low"].squeeze()
    close = df["Close"].squeeze()
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(com=period - 1, min_periods=period).mean()
    if atr.empty:
        return None
    val = float(atr.iloc[-1])
    return None if (val != val) else val  # nan check without importing math
