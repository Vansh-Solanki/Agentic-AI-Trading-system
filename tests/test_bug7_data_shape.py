"""
Bug 7: yfinance's two-level (Price, Ticker) columns propagated unflattened
through the whole pipeline, calculate_atr() crashed on a 1-row frame instead
of returning None, and the "H4" timeframe was really 1-hour bars.

No network. Synthetic frames only.
"""
import pandas as pd
import pytest

from data.feeds.price_feed import _flatten_yf_columns, _resample_1h_to_4h_session
from signals.indicators.volatility import calculate_atr, calculate_bollinger_bands


def _two_level_frame(n: int, ticker: str = "AAPL") -> pd.DataFrame:
    cols = pd.MultiIndex.from_product(
        [["Open", "High", "Low", "Close", "Volume"], [ticker]], names=["Price", "Ticker"]
    )
    base = list(range(1, n + 1))
    data = {
        ("Open", ticker):   base,
        ("High", ticker):   [v + 1 for v in base],
        ("Low", ticker):    [v - 1 for v in base],
        ("Close", ticker):  base,
        ("Volume", ticker): [1000] * n,
    }
    df = pd.DataFrame(data)
    df.columns = cols
    return df


def _flat_frame(n: int) -> pd.DataFrame:
    base = list(range(1, n + 1))
    return pd.DataFrame({
        "Open":   base,
        "High":   [v + 1 for v in base],
        "Low":    [v - 1 for v in base],
        "Close":  base,
        "Volume": [1000] * n,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Flatten helper
# ─────────────────────────────────────────────────────────────────────────────

class TestFlattenYfColumns:

    def test_two_level_columns_become_flat(self):
        df = _two_level_frame(5)
        flat = _flatten_yf_columns(df)
        assert list(flat.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert not isinstance(flat.columns, pd.MultiIndex)

    def test_already_flat_frame_unchanged(self):
        df = _flat_frame(5)
        out = _flatten_yf_columns(df)
        pd.testing.assert_frame_equal(out, df)

    def test_flattened_frame_gives_series_not_dataframe_columns(self):
        df = _flatten_yf_columns(_two_level_frame(5))
        assert isinstance(df["Close"], pd.Series)


# ─────────────────────────────────────────────────────────────────────────────
# calculate_atr robustness + flat/two-level equivalence
# ─────────────────────────────────────────────────────────────────────────────

class TestCalculateAtr:

    @pytest.mark.parametrize("n", [0, 1, 3, 13])
    def test_returns_none_below_period(self, n):
        assert calculate_atr(_flat_frame(n)) is None
        assert calculate_atr(_flatten_yf_columns(_two_level_frame(n))) is None

    @pytest.mark.parametrize("n", [14, 15, 30])
    def test_flat_and_two_level_give_identical_values(self, n):
        flat_val = calculate_atr(_flat_frame(n))
        two_level_val = calculate_atr(_flatten_yf_columns(_two_level_frame(n)))
        assert flat_val is not None
        assert two_level_val is not None
        assert flat_val == pytest.approx(two_level_val)

    def test_never_raises_on_empty_frame(self):
        empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        assert calculate_atr(empty) is None

    def test_never_raises_on_one_row_two_level_frame(self):
        # The original bug: .squeeze() on a 1-row, 1-column frame collapses
        # to a bare scalar, and scalar.shift() raised AttributeError.
        df = _flatten_yf_columns(_two_level_frame(1))
        assert calculate_atr(df) is None


class TestBollingerBands:

    def test_flat_and_two_level_give_identical_values(self):
        flat = calculate_bollinger_bands(_flat_frame(30))
        two_level = calculate_bollinger_bands(_flatten_yf_columns(_two_level_frame(30)))
        for a, b in zip(flat, two_level):
            pd.testing.assert_series_equal(a.reset_index(drop=True), b.reset_index(drop=True))


# ─────────────────────────────────────────────────────────────────────────────
# H4 session-aligned resampling
# ─────────────────────────────────────────────────────────────────────────────

def _hourly_ny_frame(day_ranges: list[tuple[str, str]]) -> pd.DataFrame:
    """Build a 1-hour-bar frame (UTC-indexed) spanning each (start, end) local
    NY wall-clock range given, one bar per hour, sequential int OHLCV values."""
    frames = []
    for start, end in day_ranges:
        idx = pd.date_range(start, end, freq="1h", tz="America/New_York")
        frames.append(idx)
    full_index = frames[0]
    for f in frames[1:]:
        full_index = full_index.append(f)
    n = len(full_index)
    values = list(range(1, n + 1))
    df = pd.DataFrame({
        "Open": values, "High": values, "Low": values, "Close": values, "Volume": [100] * n,
    }, index=full_index.tz_convert("UTC"))
    return df


class TestResample1hTo4hSession:

    def test_normal_day_gives_two_groups_with_correct_labels_and_values(self):
        df = _hourly_ny_frame([("2026-03-10 09:30", "2026-03-10 15:30")])
        out = _resample_1h_to_4h_session(df)

        assert len(out) == 2
        g1, g2 = out.iloc[0], out.iloc[1]

        # Labeled with each group's first bar's timestamp, in NY: 09:30 and 13:30.
        local = out.index.tz_convert("America/New_York")
        assert (local.hour[0], local.minute[0]) == (9, 30)
        assert (local.hour[1], local.minute[1]) == (13, 30)

        # Group 1: bars 1,2,3,4 (09:30-12:30) -> Open=1 High=4 Low=1 Close=4 Vol=400
        assert (g1["Open"], g1["High"], g1["Low"], g1["Close"], g1["Volume"]) == (1, 4, 1, 4, 400)
        # Group 2: bars 5,6,7 (13:30-15:30) -> Open=5 High=7 Low=5 Close=7 Vol=300
        assert (g2["Open"], g2["High"], g2["Low"], g2["Close"], g2["Volume"]) == (5, 7, 5, 7, 300)

    def test_early_close_day_gives_only_first_group(self):
        df = _hourly_ny_frame([("2026-03-10 09:30", "2026-03-10 12:30")])
        out = _resample_1h_to_4h_session(df)
        assert len(out) == 1
        local = out.index.tz_convert("America/New_York")
        assert (local.hour[0], local.minute[0]) == (9, 30)

    def test_two_days_give_four_groups_in_order(self):
        df = _hourly_ny_frame([
            ("2026-03-10 09:30", "2026-03-10 15:30"),
            ("2026-03-11 09:30", "2026-03-11 15:30"),
        ])
        out = _resample_1h_to_4h_session(df)
        assert len(out) == 4
        assert list(out.index) == sorted(out.index)

    def test_trailing_group_still_forming_is_kept(self):
        # Only 2 of the ~4 expected afternoon bars have printed so far.
        df = _hourly_ny_frame([("2026-03-10 09:30", "2026-03-10 14:30")])
        out = _resample_1h_to_4h_session(df)
        assert len(out) == 2
        g2 = out.iloc[1]
        # bars 5,6 (13:30, 14:30) only
        assert (g2["Open"], g2["High"], g2["Low"], g2["Close"], g2["Volume"]) == (5, 6, 5, 6, 200)

    def test_across_dst_transition_both_days_align_to_local_session_times(self):
        # 2026-03-06 is EST (-05:00); 2026-03-09 is EDT (-04:00) — DST starts
        # 2026-03-08. Both single-session days should still produce exactly
        # one group each, labeled 09:30 local on their respective day.
        df = _hourly_ny_frame([
            ("2026-03-06 09:30", "2026-03-06 12:30"),
            ("2026-03-09 09:30", "2026-03-09 12:30"),
        ])
        out = _resample_1h_to_4h_session(df)
        assert len(out) == 2
        local = out.index.tz_convert("America/New_York")
        assert all(h == 9 and m == 30 for h, m in zip(local.hour, local.minute))
        assert local[0].date().isoformat() == "2026-03-06"
        assert local[1].date().isoformat() == "2026-03-09"

    def test_nan_close_rows_are_dropped(self):
        df = _hourly_ny_frame([("2026-03-10 09:30", "2026-03-10 12:30")])
        df.loc[df.index[1], "Close"] = float("nan")
        out = _resample_1h_to_4h_session(df)
        assert len(out) == 1
        g1 = out.iloc[0]
        # bars 1, 3, 4 only (bar 2 dropped for NaN Close)
        assert g1["Open"] == 1
        assert g1["Close"] == 4

    def test_empty_frame_returns_empty(self):
        empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        out = _resample_1h_to_4h_session(empty)
        assert out.empty

    def test_output_index_is_utc_aware(self):
        df = _hourly_ny_frame([("2026-03-10 09:30", "2026-03-10 12:30")])
        out = _resample_1h_to_4h_session(df)
        assert str(out.index.tz) == "UTC"
