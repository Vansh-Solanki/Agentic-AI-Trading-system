import pandas as pd
import numpy as np
from typing import NamedTuple


# ── Head & Shoulders ──────────────────────────────────────────────────────────

def detect_head_and_shoulders(df: pd.DataFrame) -> bool:
    """Bearish reversal: head peak higher than two shoulder peaks, shoulders symmetric."""
    highs = df["High"].squeeze().tail(60).values
    if len(highs) < 20:
        return False
    third = len(highs) // 3
    left_shoulder  = highs[:third].max()
    head           = highs[third:2 * third].max()
    right_shoulder = highs[2 * third:].max()
    return (
        head > left_shoulder
        and head > right_shoulder
        and abs(left_shoulder - right_shoulder) / head < 0.05
    )


def detect_inverse_head_and_shoulders(df: pd.DataFrame) -> bool:
    """Bullish reversal: head trough lower than two shoulder troughs, shoulders symmetric."""
    lows = df["Low"].squeeze().tail(60).values
    if len(lows) < 20:
        return False
    third = len(lows) // 3
    left_shoulder  = lows[:third].min()
    head           = lows[third:2 * third].min()
    right_shoulder = lows[2 * third:].min()
    return (
        head < left_shoulder
        and head < right_shoulder
        and abs(left_shoulder - right_shoulder) / abs(head) < 0.05
    )


# ── Double Top / Bottom ───────────────────────────────────────────────────────

def detect_double_top(df: pd.DataFrame, tolerance: float = 0.02) -> bool:
    highs = df["High"].squeeze().tail(40).values
    if len(highs) < 10:
        return False
    peak1 = highs[:len(highs) // 2].max()
    peak2 = highs[len(highs) // 2:].max()
    return abs(peak1 - peak2) / max(peak1, peak2) < tolerance


def detect_double_bottom(df: pd.DataFrame, tolerance: float = 0.02) -> bool:
    lows = df["Low"].squeeze().tail(40).values
    if len(lows) < 10:
        return False
    trough1 = lows[:len(lows) // 2].min()
    trough2 = lows[len(lows) // 2:].min()
    return abs(trough1 - trough2) / min(trough1, trough2) < tolerance


# ── Flags ─────────────────────────────────────────────────────────────────────

def detect_bull_flag(df: pd.DataFrame) -> bool:
    """Strong up pole followed by tight sideways consolidation."""
    close = df["Close"].squeeze().tail(20).values
    if len(close) < 10:
        return False
    pole_gain            = (close[4] - close[0]) / close[0]
    consolidation_range  = (close[5:].max() - close[5:].min()) / close[5]
    return pole_gain > 0.05 and consolidation_range < 0.03


def detect_bear_flag(df: pd.DataFrame) -> bool:
    """Strong down pole followed by tight sideways consolidation."""
    close = df["Close"].squeeze().tail(20).values
    if len(close) < 10:
        return False
    pole_loss            = (close[0] - close[4]) / close[0]
    consolidation_range  = (close[5:].max() - close[5:].min()) / close[5]
    return pole_loss > 0.05 and consolidation_range < 0.03


# ── Pennants ──────────────────────────────────────────────────────────────────

def detect_bull_pennant(df: pd.DataFrame) -> bool:
    """Strong up pole followed by converging (triangular) consolidation."""
    if len(df) < 15:
        return False
    close = df["Close"].squeeze().tail(25).values
    highs = df["High"].squeeze().tail(25).values
    lows  = df["Low"].squeeze().tail(25).values

    pole_gain = (close[4] - close[0]) / close[0]
    if pole_gain < 0.05:
        return False

    # Consolidation window: highs contracting downward AND lows contracting upward
    consol_highs = highs[5:15]
    consol_lows  = lows[5:15]
    if len(consol_highs) < 5:
        return False
    return consol_highs[-1] < consol_highs[0] and consol_lows[-1] > consol_lows[0]


def detect_bear_pennant(df: pd.DataFrame) -> bool:
    """Strong down pole followed by converging (triangular) consolidation."""
    if len(df) < 15:
        return False
    close = df["Close"].squeeze().tail(25).values
    highs = df["High"].squeeze().tail(25).values
    lows  = df["Low"].squeeze().tail(25).values

    pole_loss = (close[0] - close[4]) / close[0]
    if pole_loss < 0.05:
        return False

    consol_highs = highs[5:15]
    consol_lows  = lows[5:15]
    if len(consol_highs) < 5:
        return False
    return consol_highs[-1] < consol_highs[0] and consol_lows[-1] > consol_lows[0]


# ── Triangles ─────────────────────────────────────────────────────────────────

def detect_ascending_triangle(df: pd.DataFrame) -> bool:
    """Flat resistance ceiling + rising lows — bullish breakout bias."""
    if len(df) < 15:
        return False
    highs = df["High"].squeeze().tail(30).values
    lows  = df["Low"].squeeze().tail(30).values

    # Resistance: the five highest highs stay within 2% of each other
    top_highs      = np.sort(highs)[-5:]
    flat_resistance = (top_highs.max() - top_highs.min()) / top_highs.max() < 0.02

    # Support: second half of lows averages higher than first half
    mid         = len(lows) // 2
    rising_lows = lows[mid:].mean() > lows[:mid].mean()

    return flat_resistance and rising_lows


def detect_descending_triangle(df: pd.DataFrame) -> bool:
    """Flat support floor + falling highs — bearish breakdown bias."""
    if len(df) < 15:
        return False
    highs = df["High"].squeeze().tail(30).values
    lows  = df["Low"].squeeze().tail(30).values

    # Support: the five lowest lows stay within 2% of each other
    bottom_lows  = np.sort(lows)[:5]
    flat_support = (bottom_lows.max() - bottom_lows.min()) / bottom_lows.max() < 0.02

    # Resistance: second half of highs averages lower than first half
    mid           = len(highs) // 2
    falling_highs = highs[mid:].mean() < highs[:mid].mean()

    return flat_support and falling_highs


# ── Range Breakout / Breakdown ────────────────────────────────────────────────

def detect_range_breakout(df: pd.DataFrame, lookback: int = 40) -> bool:
    """Price closes above a prior tight consolidation range — bullish."""
    if len(df) < lookback + 2:
        return False
    range_series  = df["Close"].squeeze().iloc[-(lookback + 2):-2]
    current_close = float(df["Close"].squeeze().iloc[-1])
    range_high    = float(range_series.max())
    range_low     = float(range_series.min())

    # Only treat it as a range if it was genuinely tight (< 8% width)
    if (range_high - range_low) / range_high > 0.08:
        return False

    return current_close > range_high * 1.005   # 0.5% clear of the ceiling


def detect_range_breakdown(df: pd.DataFrame, lookback: int = 40) -> bool:
    """Price closes below a prior tight consolidation range — bearish."""
    if len(df) < lookback + 2:
        return False
    range_series  = df["Close"].squeeze().iloc[-(lookback + 2):-2]
    current_close = float(df["Close"].squeeze().iloc[-1])
    range_high    = float(range_series.max())
    range_low     = float(range_series.min())

    if (range_high - range_low) / range_high > 0.08:
        return False

    return current_close < range_low * 0.995    # 0.5% clear of the floor


# ── Regression Channel Breakout ───────────────────────────────────────────────

class ChannelBreakoutResult(NamedTuple):
    direction: int    # 0=none, 1=bearish breakdown, 2=bullish breakout
    confidence: float # sigmoid-scored off average channel R²
    r2_high: float    # R² of resistance (upper) line
    r2_low: float     # R² of support (lower) line


def _ols_regression(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """OLS linear regression. Returns (slope, intercept, r²)."""
    x_mean, y_mean = x.mean(), y.mean()
    ss_xx = ((x - x_mean) ** 2).sum()
    if ss_xx < 1e-10:
        return 0.0, float(y_mean), 0.0
    slope     = ((x - x_mean) * (y - y_mean)).sum() / ss_xx
    intercept = y_mean - slope * x_mean
    ss_yy = ((y - y_mean) ** 2).sum()
    if ss_yy < 1e-10:
        return slope, intercept, 1.0
    ss_res = ((y - (slope * x + intercept)) ** 2).sum()
    return slope, intercept, max(0.0, 1.0 - ss_res / ss_yy)


def detect_regression_channel_breakout(
    df: pd.DataFrame,
    window: int = 5,
    backcandles: int = 40,
    parallel: float = 0.1,
    r_sq_min: float = 0.90,
) -> ChannelBreakoutResult:
    """
    Fits linear regression lines through recent pivot highs (resistance) and pivot
    lows (support) over the last `backcandles` bars. A breakout is confirmed when
    the current bar's open AND close both cross the respective channel line.

    Quality gates (both must pass):
      - R² >= r_sq_min for each line  (channel must be genuinely straight)
      - slope divergence < parallel   (lines must be roughly parallel)

    Confidence is sigmoid-scored off average R²: tighter channel → higher confidence.
    Confidence crosses 0.50 exactly at r_sq_min (0.90); at R²=0.95 it reads ~0.73.

    Parameters mirror the notebook: WINDOW=5, BACKCANDLES=40, PARALLEL=0.1, R_SQ_MIN=0.90.
    """
    _none = ChannelBreakoutResult(0, 0.0, 0.0, 0.0)

    if len(df) < backcandles + window + 2:
        return _none

    # Local window: `backcandles` bars, ending `window` bars before the current bar.
    # This leaves a gap so the pivot detector doesn't touch the current candle.
    local_end   = len(df) - 1 - window
    local_start = local_end - backcandles
    if local_start < 0:
        return _none

    h = df["High"].iloc[local_start:local_end].values.astype(float)
    l = df["Low"].iloc[local_start:local_end].values.astype(float)
    n = len(h)

    if n < window * 2 + 1:
        return _none

    # Pivot detection: bar i is a pivot high if its High equals the local max in [i-w, i+w]
    ph_idx, pl_idx = [], []
    for i in range(window, n - window):
        nbr_h = h[i - window : i + window + 1]
        nbr_l = l[i - window : i + window + 1]
        if h[i] >= nbr_h.max():   # >= max(neighbourhood) → tied-or-leading high
            ph_idx.append(i)
        if l[i] <= nbr_l.min():   # <= min(neighbourhood) → tied-or-leading low
            pl_idx.append(i)

    # Need ≥2 pivot highs, ≥2 pivot lows, ≥5 total (mirrors notebook's guard)
    if len(ph_idx) < 2 or len(pl_idx) < 2 or (len(ph_idx) + len(pl_idx)) < 5:
        return _none

    # Most recent 3 pivots of each side (last 3 = notebook's [-3:])
    ph_x = np.array(ph_idx[-3:], dtype=float)
    pl_x = np.array(pl_idx[-3:], dtype=float)
    ph_y = h[[int(i) for i in ph_x]]
    pl_y = l[[int(i) for i in pl_x]]

    sl_h, int_h, r2_h = _ols_regression(ph_x, ph_y)
    sl_l, int_l, r2_l = _ols_regression(pl_x, pl_y)

    # R² gate
    if r2_h < r_sq_min or r2_l < r_sq_min:
        return _none

    # Parallel gate: slope divergence fraction must be below threshold
    denom = abs(sl_h + sl_l)
    if denom > 1e-10 and abs((sl_l - sl_h) / denom / 2) >= parallel:
        return _none

    # Extend regression lines to current and previous bar.
    # Relative to the start of the local window, the current bar is at n + window,
    # and the previous bar is at n + window - 1.
    curr_rel  = float(n + window)
    prev_rel  = float(n + window - 1)
    upper_curr = sl_h * curr_rel + int_h
    upper_prev = sl_h * prev_rel + int_h
    lower_curr = sl_l * curr_rel + int_l
    lower_prev = sl_l * prev_rel + int_l

    curr_open  = float(df["Open"].iloc[-1])
    curr_close = float(df["Close"].iloc[-1])

    # Sigmoid confidence centered at r_sq_min — steepness=20 so 0.05 above threshold ≈ 0.73
    r2_avg     = (r2_h + r2_l) / 2.0
    confidence = round(1.0 / (1.0 + np.exp(-20.0 * (r2_avg - r_sq_min))), 3)

    # Bullish: open AND close both above the upper (resistance) channel line
    if curr_open > upper_curr and curr_close > upper_prev:
        return ChannelBreakoutResult(2, confidence, round(r2_h, 4), round(r2_l, 4))

    # Bearish: open AND close both below the lower (support) channel line
    if curr_open < lower_curr and curr_close < lower_prev:
        return ChannelBreakoutResult(1, confidence, round(r2_h, 4), round(r2_l, 4))

    return _none