"""
Bug 3: news/layer3_spike.py's spike gate had no market-open check and used
the still-forming last bar instead of the latest COMPLETED bar, firing for
19/20 WATCHLIST tickers in the Oct 4 premarket run (market closed) and
comparing partial-bar volume against full-bar averages during market hours.

No network — synthetic candle frames only, built relative to real "now" so
bar completeness (start + 5min <= now) holds without needing to patch
datetime itself.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from news.layer3_spike import _detect_market_event, run_spike_detector
from core.models import NewsItem


def _bar_start(n_ago: int) -> datetime:
    """Start time of the bar n_ago*5 minutes before now. n_ago=0 is the
    currently-forming bar (incomplete); n_ago>=1 is always a completed bar
    (start + 5min <= now, with margin — see module docstring)."""
    return datetime.now(timezone.utc) - timedelta(minutes=5 * n_ago)


def _make_m5_df(n_completed=21, completed_volume=1000.0, completed_move=0.5,
                 spike_at_n_ago=None, spike_volume=None, spike_move=None,
                 include_forming=False, forming_volume=None, forming_move=None):
    """Build an M5 candle frame. Completed bars run from n_ago=n_completed
    down to n_ago=1 (oldest to newest), each at `completed_volume`/`completed_move`
    unless n_ago == spike_at_n_ago, which gets spike_volume/spike_move instead.
    An optional forming bar (n_ago=0) can be added with its own volume/move."""
    rows = []
    index = []
    base_price = 100.0
    for n_ago in range(n_completed, 0, -1):
        vol = spike_volume if n_ago == spike_at_n_ago else completed_volume
        move = spike_move if n_ago == spike_at_n_ago else completed_move
        index.append(_bar_start(n_ago))
        rows.append({
            "Open": base_price, "High": base_price + move + 0.1,
            "Low": base_price - 0.1, "Close": base_price + move, "Volume": vol,
        })
    if include_forming:
        index.append(_bar_start(0))
        rows.append({
            "Open": base_price, "High": base_price + (forming_move or 0) + 0.1,
            "Low": base_price - 0.1, "Close": base_price + (forming_move or 0),
            "Volume": forming_volume or 0.0,
        })
    df = pd.DataFrame(rows, index=pd.DatetimeIndex(index))
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Market-closed guard
# ─────────────────────────────────────────────────────────────────────────────

class TestMarketClosedGuard:

    def test_market_closed_returns_no_event_even_with_huge_spike(self, monkeypatch):
        monkeypatch.setattr("agents.orchestrator._is_market_open", lambda: False)
        huge_spike_df = _make_m5_df(n_completed=21, spike_at_n_ago=1,
                                     spike_volume=50_000.0, spike_move=10.0)
        get_candles_mock = Mock(return_value=huge_spike_df)
        monkeypatch.setattr("news.layer3_spike.get_candles", get_candles_mock)

        detected, desc = _detect_market_event("AAPL")

        assert (detected, desc) == (False, "")
        # The market-closed check short-circuits before ever fetching candles.
        get_candles_mock.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Forming bar excluded
# ─────────────────────────────────────────────────────────────────────────────

class TestFormingBarExcluded:

    def test_forming_bar_huge_volume_completed_normal_no_event(self, monkeypatch):
        monkeypatch.setattr("agents.orchestrator._is_market_open", lambda: True)
        df = _make_m5_df(n_completed=21, completed_volume=1000.0, completed_move=0.3,
                          include_forming=True, forming_volume=100_000.0, forming_move=20.0)
        monkeypatch.setattr("news.layer3_spike.get_candles", Mock(return_value=df))

        detected, desc = _detect_market_event("AAPL")

        assert (detected, desc) == (False, "")


# ─────────────────────────────────────────────────────────────────────────────
# Completed-bar spike still detected; bar-count floor still enforced
# ─────────────────────────────────────────────────────────────────────────────

class TestCompletedBarSpike:

    def test_completed_bar_3x_volume_triggers_event(self, monkeypatch):
        monkeypatch.setattr("agents.orchestrator._is_market_open", lambda: True)
        df = _make_m5_df(n_completed=21, completed_volume=1000.0, completed_move=0.1,
                          spike_at_n_ago=1, spike_volume=3_500.0, spike_move=0.1)
        monkeypatch.setattr("news.layer3_spike.get_candles", Mock(return_value=df))

        detected, desc = _detect_market_event("AAPL")

        assert detected is True
        assert "volume" in desc.lower()

    def test_fewer_than_21_completed_bars_returns_no_event(self, monkeypatch):
        monkeypatch.setattr("agents.orchestrator._is_market_open", lambda: True)
        df = _make_m5_df(n_completed=15, completed_volume=1000.0, completed_move=0.1,
                          spike_at_n_ago=1, spike_volume=10_000.0, spike_move=5.0)
        monkeypatch.setattr("news.layer3_spike.get_candles", Mock(return_value=df))

        detected, desc = _detect_market_event("AAPL")

        assert (detected, desc) == (False, "")


# ─────────────────────────────────────────────────────────────────────────────
# run_spike_detector's no-event path still persists news
# ─────────────────────────────────────────────────────────────────────────────

class TestNoEventStillSavesNews:

    def test_no_event_sets_layer_3_and_calls_save_news(self, monkeypatch):
        monkeypatch.setattr("news.layer3_spike._detect_market_event", lambda ticker: (False, ""))
        save_news_mock = Mock()
        monkeypatch.setattr("news.layer3_spike.save_news", save_news_mock)

        items = [
            NewsItem(headline="h1", source="s", url="u1",
                     published_at=datetime.now(timezone.utc), tickers=["AAPL"], layer=2),
            NewsItem(headline="h2", source="s", url="u2",
                     published_at=datetime.now(timezone.utc), tickers=["AAPL"], layer=2),
        ]

        signals = run_spike_detector("AAPL", items)

        assert signals == []
        assert all(item.layer == 3 for item in items)
        save_news_mock.assert_called_once_with(items)
