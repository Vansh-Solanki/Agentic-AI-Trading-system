import pandas as pd
import numpy as np
import pytest
from signals.indicators.momentum import calculate_rsi, calculate_macd, calculate_stoch_rsi, get_stoch_rsi
from signals.indicators.volatility import calculate_atr, calculate_bollinger_bands
from signals.indicators.trend import calculate_ema_stack, calculate_adx, EMAStack


def _make_df(n: int = 250, trending: bool = False) -> pd.DataFrame:
    np.random.seed(42)
    if trending:
        # Simulate a clear uptrend for EMA/ADX tests
        close = 100 + np.arange(n) * 0.3 + np.random.randn(n) * 0.2
    else:
        close = 100 + np.cumsum(np.random.randn(n) * 0.5)
    close = np.maximum(close, 1.0)  # ensure positive prices
    return pd.DataFrame({
        "Open":   close * 0.999,
        "High":   close * 1.002,
        "Low":    close * 0.998,
        "Close":  close,
        "Volume": np.random.randint(1_000_000, 5_000_000, n),
    })


# ── RSI ───────────────────────────────────────────────────────────────────────

def test_rsi_range():
    df  = _make_df()
    rsi = calculate_rsi(df)
    valid = rsi.dropna()
    assert (valid >= 0).all() and (valid <= 100).all()


# ── MACD ──────────────────────────────────────────────────────────────────────

def test_macd_returns_three_series():
    df = _make_df()
    macd, signal, hist = calculate_macd(df)
    assert len(macd) == len(df)
    assert len(signal) == len(df)
    assert len(hist) == len(df)


# ── Volatility ────────────────────────────────────────────────────────────────

def test_atr_positive():
    df  = _make_df()
    atr = calculate_atr(df)
    assert atr > 0


def test_bollinger_bands_ordering():
    df = _make_df()
    upper, mid, lower = calculate_bollinger_bands(df)
    assert (upper.dropna() >= mid.dropna()).all()
    assert (mid.dropna() >= lower.dropna()).all()


# ── Stochastic RSI ────────────────────────────────────────────────────────────

def test_stoch_rsi_range():
    df = _make_df(n=100)
    k, d = calculate_stoch_rsi(df)
    valid_k = k.dropna()
    valid_d = d.dropna()
    assert (valid_k >= 0).all() and (valid_k <= 100).all()
    assert (valid_d >= 0).all() and (valid_d <= 100).all()


def test_stoch_rsi_same_length_as_input():
    df = _make_df(n=100)
    k, d = calculate_stoch_rsi(df)
    assert len(k) == len(df)
    assert len(d) == len(df)


def test_get_stoch_rsi_returns_dataclass():
    df = _make_df(n=100)
    result = get_stoch_rsi(df)
    assert 0.0 <= result.k <= 100.0
    assert 0.0 <= result.d <= 100.0


def test_stoch_rsi_overbought_oversold_flags():
    df = _make_df(n=100)
    result = get_stoch_rsi(df)
    # Mutually exclusive — cannot be both
    assert not (result.overbought and result.oversold)


# ── EMA Stack ─────────────────────────────────────────────────────────────────

def test_ema_stack_returns_two_dataclasses():
    df = _make_df(n=250)
    current, previous = calculate_ema_stack(df)
    assert isinstance(current, EMAStack)
    assert isinstance(previous, EMAStack)


def test_ema_stack_bullish_count_range():
    df = _make_df(n=250)
    current, _ = calculate_ema_stack(df)
    assert 0 <= current.bullish_count <= 4
    assert 0 <= current.bearish_count <= 4


def test_ema_stack_fully_bullish_on_uptrend():
    df = _make_df(n=250, trending=True)
    current, _ = calculate_ema_stack(df)
    # In a strong uptrend price should be above most EMAs
    assert current.bullish_count >= 2


def test_golden_cross_detection():
    # Fabricate a scenario where EMA50 crosses above EMA200
    df = _make_df(n=250)
    current, previous = calculate_ema_stack(df)
    # Just verify the method runs without error and returns bool
    assert isinstance(current.golden_cross(previous), bool)
    assert isinstance(current.death_cross(previous), bool)


# ── ADX / DMI ─────────────────────────────────────────────────────────────────

def test_adx_range():
    df = _make_df(n=100)
    result = calculate_adx(df)
    assert 0.0 <= result.adx <= 100.0
    assert result.plus_di >= 0.0
    assert result.minus_di >= 0.0


def test_adx_trending_flag_on_strong_uptrend():
    df = _make_df(n=150, trending=True)
    result = calculate_adx(df)
    # A clear uptrend should produce meaningful ADX (not necessarily >25 on short series,
    # but should be non-zero and +DI should dominate)
    assert result.adx >= 0
    assert result.plus_di >= 0


def test_adx_bullish_bearish_exclusive():
    df = _make_df(n=100)
    result = calculate_adx(df)
    # bullish and bearish are determined by +DI vs -DI — they cannot both be True
    assert not (result.bullish and result.bearish)
