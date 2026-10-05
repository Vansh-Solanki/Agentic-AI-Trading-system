import pandas as pd
import numpy as np


def _as_series(column: pd.Series | pd.DataFrame) -> pd.Series:
    """
    Session 52 (Bug 7): return a DataFrame column as a plain Series whether
    it's already a Series or a one-column DataFrame (yfinance's two-level
    columns produce the latter for df["Close"] etc. before flattening).
    `.squeeze()` was used for this previously, but squeeze() collapses BOTH
    axes when both happen to be length 1 — a one-row, one-column frame
    squeezes all the way down to a bare numpy scalar, not a Series, which is
    exactly what made calculate_atr() crash on a 1-row frame instead of
    returning None. iloc[:, 0] only ever drops the column axis.
    """
    if isinstance(column, pd.DataFrame):
        return column.iloc[:, 0]
    return column


def calculate_bollinger_bands(
    df: pd.DataFrame, period: int = 20, std_dev: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (upper_band, middle_band, lower_band)."""
    close  = _as_series(df["Close"])
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
    if df.empty:
        return None
    high  = _as_series(df["High"])
    low   = _as_series(df["Low"])
    close = _as_series(df["Close"])
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
