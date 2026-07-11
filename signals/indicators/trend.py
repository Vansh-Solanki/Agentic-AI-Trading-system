import pandas as pd
import numpy as np
from dataclasses import dataclass


def calculate_ema(df: pd.DataFrame, period: int) -> pd.Series:
    return df["Close"].squeeze().ewm(span=period, adjust=False).mean()


def calculate_sma(df: pd.DataFrame, period: int) -> pd.Series:
    return df["Close"].squeeze().rolling(period).mean()


@dataclass
class EMAStack:
    ema9:  float
    ema20: float
    ema50: float
    ema200: float
    price: float

    @property
    def fully_bullish(self) -> bool:
        """Price > EMA9 > EMA20 > EMA50 > EMA200 — textbook uptrend."""
        return self.price > self.ema9 > self.ema20 > self.ema50 > self.ema200

    @property
    def fully_bearish(self) -> bool:
        """Price < EMA9 < EMA20 < EMA50 < EMA200 — textbook downtrend."""
        return self.price < self.ema9 < self.ema20 < self.ema50 < self.ema200

    @property
    def bullish_count(self) -> int:
        """Number of EMAs below price (0–4). Higher = stronger uptrend."""
        emas = [self.ema9, self.ema20, self.ema50, self.ema200]
        return sum(1 for e in emas if self.price > e)

    @property
    def bearish_count(self) -> int:
        """Number of EMAs above price (0–4). Higher = stronger downtrend."""
        emas = [self.ema9, self.ema20, self.ema50, self.ema200]
        return sum(1 for e in emas if self.price < e)

    def golden_cross(self, prev: "EMAStack") -> bool:
        """EMA50 just crossed above EMA200."""
        return prev.ema50 < prev.ema200 and self.ema50 >= self.ema200

    def death_cross(self, prev: "EMAStack") -> bool:
        """EMA50 just crossed below EMA200."""
        return prev.ema50 > prev.ema200 and self.ema50 <= self.ema200


def calculate_ema_stack(df: pd.DataFrame) -> tuple[EMAStack, EMAStack]:
    """
    Returns (current_stack, previous_stack) for crossover detection.
    Requires at least 200 candles for EMA200 to be meaningful.
    """
    close = df["Close"].squeeze()
    e9    = close.ewm(span=9,   adjust=False).mean()
    e20   = close.ewm(span=20,  adjust=False).mean()
    e50   = close.ewm(span=50,  adjust=False).mean()
    e200  = close.ewm(span=200, adjust=False).mean()

    current = EMAStack(
        ema9=float(e9.iloc[-1]),
        ema20=float(e20.iloc[-1]),
        ema50=float(e50.iloc[-1]),
        ema200=float(e200.iloc[-1]),
        price=float(close.iloc[-1]),
    )
    previous = EMAStack(
        ema9=float(e9.iloc[-2]),
        ema20=float(e20.iloc[-2]),
        ema50=float(e50.iloc[-2]),
        ema200=float(e200.iloc[-2]),
        price=float(close.iloc[-2]),
    )
    return current, previous


@dataclass
class ADXResult:
    adx:   float   # 0–100; >25 = trending, >40 = strong, >60 = very strong
    plus_di: float  # +DI: buying pressure
    minus_di: float # -DI: selling pressure

    @property
    def is_trending(self) -> bool:
        return self.adx > 25

    @property
    def is_strong_trend(self) -> bool:
        return self.adx > 40

    @property
    def bullish(self) -> bool:
        return self.plus_di > self.minus_di

    @property
    def bearish(self) -> bool:
        return self.minus_di > self.plus_di


def calculate_adx(df: pd.DataFrame, period: int = 14) -> ADXResult:
    """
    Calculates ADX, +DI, and -DI (Wilder smoothing).
    Returns ADXResult. Requires at least 2*period candles.
    """
    high  = df["High"].squeeze()
    low   = df["Low"].squeeze()
    close = df["Close"].squeeze()

    # True Range
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    # Directional movement
    up_move   = high.diff()
    down_move = (-low.diff())
    plus_dm   = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm  = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    # Wilder smoothing (equivalent to EWM with com=period-1)
    atr_w      = tr.ewm(com=period - 1, min_periods=period).mean()
    plus_dm_w  = plus_dm.ewm(com=period - 1, min_periods=period).mean()
    minus_dm_w = minus_dm.ewm(com=period - 1, min_periods=period).mean()

    plus_di  = 100 * plus_dm_w  / atr_w.replace(0, np.nan)
    minus_di = 100 * minus_dm_w / atr_w.replace(0, np.nan)

    dx  = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(com=period - 1, min_periods=period).mean()

    return ADXResult(
        adx=float(adx.iloc[-1])      if not adx.isna().iloc[-1]      else 0.0,
        plus_di=float(plus_di.iloc[-1])   if not plus_di.isna().iloc[-1]   else 0.0,
        minus_di=float(minus_di.iloc[-1]) if not minus_di.isna().iloc[-1]  else 0.0,
    )


def trend_direction(df: pd.DataFrame, fast: int = 20, slow: int = 50) -> str:
    """Returns 'up', 'down', or 'sideways'."""
    ema_fast = calculate_ema(df, fast).iloc[-1]
    ema_slow = calculate_ema(df, slow).iloc[-1]
    diff_pct = (ema_fast - ema_slow) / ema_slow
    if diff_pct > 0.01:
        return "up"
    if diff_pct < -0.01:
        return "down"
    return "sideways"


def is_higher_high_higher_low(df: pd.DataFrame, lookback: int = 5) -> bool:
    highs = df["High"].squeeze().tail(lookback)
    lows  = df["Low"].squeeze().tail(lookback)
    return bool(highs.is_monotonic_increasing and lows.is_monotonic_increasing)
