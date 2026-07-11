import pandas as pd
import numpy as np


def calculate_vwap(df: pd.DataFrame) -> pd.Series:
    """Intraday VWAP — resets daily."""
    typical = (df["High"].squeeze() + df["Low"].squeeze() + df["Close"].squeeze()) / 3
    volume  = df["Volume"].squeeze()
    cum_tp_vol = (typical * volume).cumsum()
    cum_vol    = volume.cumsum()
    return cum_tp_vol / cum_vol.replace(0, np.nan)


def calculate_obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume."""
    close  = df["Close"].squeeze()
    volume = df["Volume"].squeeze()
    direction = close.diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    return (direction * volume).cumsum()


def volume_spike(df: pd.DataFrame, lookback: int = 20, threshold: float = 2.0) -> bool:
    """True if current volume is threshold× the recent average."""
    volume = df["Volume"].squeeze()
    avg    = volume.iloc[-lookback-1:-1].mean()
    return float(volume.iloc[-1]) > avg * threshold
