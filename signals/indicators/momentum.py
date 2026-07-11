import pandas as pd
import numpy as np
from dataclasses import dataclass


def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    close = df["Close"].squeeze()
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calculate_macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (macd_line, signal_line, histogram)."""
    close    = df["Close"].squeeze()
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd     = ema_fast - ema_slow
    sig      = macd.ewm(span=signal, adjust=False).mean()
    hist     = macd - sig
    return macd, sig, hist


@dataclass
class StochRSIResult:
    k: float   # %K — fast line (0–100)
    d: float   # %D — signal line, smoothed %K (0–100)

    @property
    def overbought(self) -> bool:
        return self.k > 80

    @property
    def oversold(self) -> bool:
        return self.k < 20

    @property
    def bullish_cross(self) -> bool:
        """Bullish when %K is above %D in the oversold zone."""
        return self.k > self.d and self.k < 50

    @property
    def bearish_cross(self) -> bool:
        """Bearish when %K is below %D in the overbought zone."""
        return self.k < self.d and self.k > 50


def calculate_stoch_rsi(
    df: pd.DataFrame,
    rsi_period: int = 14,
    stoch_period: int = 14,
    k_smooth: int = 3,
    d_smooth: int = 3,
) -> tuple[pd.Series, pd.Series]:
    """
    Stochastic RSI: applies the Stochastic oscillator formula to RSI values.
    Returns (%K series, %D series), both in [0, 100].
    Standard parameters: RSI(14) → Stoch(14) → smooth K(3) → smooth D(3).
    """
    rsi = calculate_rsi(df, rsi_period)

    rsi_min = rsi.rolling(stoch_period).min()
    rsi_max = rsi.rolling(stoch_period).max()
    rsi_range = (rsi_max - rsi_min).replace(0, np.nan)

    raw_k = 100 * (rsi - rsi_min) / rsi_range
    k     = raw_k.rolling(k_smooth).mean()   # smoothed %K
    d     = k.rolling(d_smooth).mean()        # %D signal line

    return k, d


def get_stoch_rsi(df: pd.DataFrame, **kwargs) -> StochRSIResult:
    """Convenience wrapper — returns current bar's StochRSI as a dataclass."""
    k, d = calculate_stoch_rsi(df, **kwargs)
    return StochRSIResult(
        k=float(k.iloc[-1]) if not k.isna().iloc[-1] else 50.0,
        d=float(d.iloc[-1]) if not d.isna().iloc[-1] else 50.0,
    )
