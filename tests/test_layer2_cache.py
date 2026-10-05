"""
Bug 4: news/layer2_premarket.py::run_premarket_analysis() ran three times
per ticker in a premarket run (batch + intraday agent + long-term agent),
each a fresh fetch + FinBERT pass + LLM call for the same daily-bias
question within minutes of each other.

Temporary SQLite only (monkeypatches data.storage.db.DB_PATH, calls
init_schema()), never trading_agent.db. fetch_premarket_news, score_batch,
and query_llm are all faked with call-counting Mocks — no network.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import core.config as cfg
import data.storage.db as db
from core.config import TradingMode
from core.constants import SignalSource, SignalType, Timeframe
from core.models import NewsItem, Signal
from news.layer2_premarket import run_premarket_analysis
from news.layer2_cache import get_cached_layer2_signal, save_layer2_signal


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_trading_agent.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_schema()
    return db_path


def _fake_items(ticker: str, n: int = 1) -> list[NewsItem]:
    now = datetime.now(timezone.utc)
    return [
        NewsItem(headline=f"{ticker} headline {i}", source="test", url=f"u{i}",
                 published_at=now, tickers=[ticker], summary="")
        for i in range(n)
    ]


_DEFAULT_LLM_RETURN = object()


def _install_fakes(monkeypatch, fetch_return=None, fetch_side_effect=None,
                    score_return=(0.6,), llm_return=_DEFAULT_LLM_RETURN):
    if llm_return is _DEFAULT_LLM_RETURN:
        llm_return = json.dumps({"stance": "bullish", "catalyst": "c", "risk": "r"})
    fetch_mock = Mock(side_effect=fetch_side_effect) if fetch_side_effect else \
        Mock(return_value=fetch_return if fetch_return is not None else _fake_items("AAPL"))
    score_mock = Mock(return_value=list(score_return))
    llm_mock = Mock(return_value=llm_return)
    monkeypatch.setattr("news.layer2_premarket.fetch_premarket_news", fetch_mock)
    monkeypatch.setattr("news.layer2_premarket.score_batch", score_mock)
    monkeypatch.setattr("news.layer2_premarket.query_llm", llm_mock)
    return fetch_mock, score_mock, llm_mock


# ─────────────────────────────────────────────────────────────────────────────
# Cache hit / miss basics
# ─────────────────────────────────────────────────────────────────────────────

class TestCacheHitMiss:

    def test_second_call_within_ttl_is_a_cache_hit_with_no_work(self, temp_db, monkeypatch):
        fetch_mock, score_mock, llm_mock = _install_fakes(
            monkeypatch, fetch_side_effect=lambda tickers: _fake_items(tickers[0]))

        first = run_premarket_analysis("AAPL")
        assert len(first) == 1
        assert llm_mock.call_count == 1

        second = run_premarket_analysis("AAPL")

        assert fetch_mock.call_count == 1
        assert score_mock.call_count == 1
        assert llm_mock.call_count == 1
        assert len(second) == 1
        assert second[0].signal_type == first[0].signal_type
        assert second[0].confidence == pytest.approx(first[0].confidence)
        assert second[0].metadata["cached"] is True

    def test_force_refresh_ignores_cache_and_overwrites(self, temp_db, monkeypatch):
        fetch_mock, score_mock, llm_mock = _install_fakes(
            monkeypatch, fetch_side_effect=lambda tickers: _fake_items(tickers[0]))

        run_premarket_analysis("AAPL")
        assert llm_mock.call_count == 1

        run_premarket_analysis("AAPL", force_refresh=True)
        assert llm_mock.call_count == 2
        assert fetch_mock.call_count == 2

    def test_zero_articles_never_cached_and_always_retried(self, temp_db, monkeypatch):
        fetch_mock, score_mock, llm_mock = _install_fakes(monkeypatch, fetch_return=[])

        result1 = run_premarket_analysis("AAPL")
        result2 = run_premarket_analysis("AAPL")

        assert result1 == []
        assert result2 == []
        assert fetch_mock.call_count == 2  # no cache ever written, so every call re-fetches

    def test_llm_failure_never_cached_and_always_retried(self, temp_db, monkeypatch):
        fetch_mock, score_mock, llm_mock = _install_fakes(
            monkeypatch, fetch_side_effect=lambda tickers: _fake_items(tickers[0]), llm_return=None)

        result1 = run_premarket_analysis("AAPL")
        assert result1[0].metadata["llm_failed"] is True

        result2 = run_premarket_analysis("AAPL")
        assert result2[0].metadata["llm_failed"] is True
        assert llm_mock.call_count == 2  # not cached, so the second call retried the LLM too


# ─────────────────────────────────────────────────────────────────────────────
# TTL
# ─────────────────────────────────────────────────────────────────────────────

class TestTTL:

    def test_expired_entry_is_recomputed(self, temp_db, monkeypatch):
        fetch_mock, score_mock, llm_mock = _install_fakes(
            monkeypatch, fetch_side_effect=lambda tickers: _fake_items(tickers[0]))
        run_premarket_analysis("AAPL")
        assert llm_mock.call_count == 1

        from data.storage.stock_memory_store import get_memory, upsert_memory
        raw = get_memory("AAPL", "shared", "layer2_signal")
        payload = json.loads(raw)
        stale = datetime.now(timezone.utc) - timedelta(minutes=cfg.LAYER2_CACHE_TTL_MINUTES + 5)
        payload["saved_at"] = stale.isoformat()
        upsert_memory("AAPL", "shared", "layer2_signal", json.dumps(payload))

        run_premarket_analysis("AAPL")
        assert llm_mock.call_count == 2

    def test_young_entry_is_reused(self, temp_db, monkeypatch):
        fetch_mock, score_mock, llm_mock = _install_fakes(
            monkeypatch, fetch_side_effect=lambda tickers: _fake_items(tickers[0]))
        run_premarket_analysis("AAPL")
        run_premarket_analysis("AAPL")
        assert llm_mock.call_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# Round trip / corruption
# ─────────────────────────────────────────────────────────────────────────────

class TestRoundTrip:

    def test_fields_preserved_and_cached_flag_added(self, temp_db):
        sig = Signal(
            ticker="AAPL", source=SignalSource.NEWS, signal_type=SignalType.BUY,
            timeframe=Timeframe.D1, confidence=0.73, value=0.41,
            reasoning="some reasoning text", metadata={"layer": 2},
        )
        save_layer2_signal("AAPL", sig, article_count=3)

        cached = get_cached_layer2_signal("AAPL", max_age_minutes=90)

        assert cached is not None
        assert isinstance(cached.signal_type, SignalType)
        assert isinstance(cached.timeframe, Timeframe)
        assert cached.signal_type == SignalType.BUY
        assert cached.timeframe == Timeframe.D1
        assert cached.confidence == pytest.approx(0.73)
        assert cached.value == pytest.approx(0.41)
        assert cached.reasoning == "some reasoning text"
        assert cached.metadata["cached"] is True

    def test_corrupt_json_is_a_cache_miss(self, temp_db):
        from data.storage.stock_memory_store import upsert_memory
        upsert_memory("AAPL", "shared", "layer2_signal", "{not valid json")
        assert get_cached_layer2_signal("AAPL", 90) is None

    def test_missing_entry_is_a_cache_miss(self, temp_db):
        assert get_cached_layer2_signal("NOPE", 90) is None


# ─────────────────────────────────────────────────────────────────────────────
# Full premarket-run simulation
# ─────────────────────────────────────────────────────────────────────────────

class TestPremarketRunSimulation:

    def test_n_tickers_give_n_llm_calls_not_3n(self, temp_db, monkeypatch):
        tickers = ["AAPL", "MSFT", "NVDA"]
        fetch_mock, score_mock, llm_mock = _install_fakes(
            monkeypatch, fetch_side_effect=lambda tks: _fake_items(tks[0]))
        monkeypatch.setattr("news.news_ingest.fetch_fundamentals", lambda ticker: SimpleNamespace())
        monkeypatch.setattr("news.news_ingest.save_fundamentals_cache", lambda data: None)

        from news.news_ingest import run_premarket_news_fetch
        run_premarket_news_fetch(tickers)  # batch path: force_refresh=True internally

        assert llm_mock.call_count == len(tickers)

        intraday_results = {t: run_premarket_analysis(t) for t in tickers}   # agent path, "intraday"
        longterm_results = {t: run_premarket_analysis(t) for t in tickers}   # agent path, "long_term"

        assert llm_mock.call_count == len(tickers)  # both agent passes were cache hits
        for t in tickers:
            assert len(intraday_results[t]) == 1
            assert len(longterm_results[t]) == 1
            assert intraday_results[t][0].signal_type == longterm_results[t][0].signal_type
            assert intraday_results[t][0].confidence == pytest.approx(longterm_results[t][0].confidence)

    def test_batch_checkpoint_still_skips_completed_tickers(self, temp_db, monkeypatch):
        fetch_mock, score_mock, llm_mock = _install_fakes(
            monkeypatch, fetch_side_effect=lambda tks: _fake_items(tks[0]))
        monkeypatch.setattr("news.news_ingest.fetch_fundamentals", lambda ticker: SimpleNamespace())
        monkeypatch.setattr("news.news_ingest.save_fundamentals_cache", lambda data: None)

        from news.news_ingest import run_premarket_news_fetch
        run_premarket_news_fetch(["AAPL"])
        run_premarket_news_fetch(["AAPL"])  # same NY batch_date -> checkpoint should skip it

        assert fetch_mock.call_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# NewsSentimentAgent: degraded decided from signals, not items_out
# ─────────────────────────────────────────────────────────────────────────────

class TestNewsSentimentDegradedFlag:

    def test_cache_hit_does_not_set_news_degraded(self, temp_db, monkeypatch):
        from agents.news_sentiment import NewsSentimentAgent
        monkeypatch.setattr("agents.orchestrator._is_market_open", lambda: False)
        cached_signal = Signal(
            ticker="AAPL", source=SignalSource.NEWS, signal_type=SignalType.BUY,
            timeframe=Timeframe.D1, confidence=0.6, value=0.3, reasoning="x",
            metadata={"cached": True},
        )
        monkeypatch.setattr("agents.news_sentiment.run_premarket_analysis", lambda ticker: [cached_signal])

        agent = NewsSentimentAgent(TradingMode.LONG_TERM)
        output = agent.run("AAPL")

        assert "degraded" not in output.summary

    def test_no_articles_still_sets_news_degraded(self, temp_db, monkeypatch):
        from agents.news_sentiment import NewsSentimentAgent
        monkeypatch.setattr("agents.orchestrator._is_market_open", lambda: False)
        monkeypatch.setattr("agents.news_sentiment.run_premarket_analysis", lambda ticker: [])

        agent = NewsSentimentAgent(TradingMode.LONG_TERM)
        output = agent.run("AAPL")

        assert "degraded" in output.summary
