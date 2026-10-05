"""
Tests for the NaN-close guard in agents/trade_plan.py.

Covers the bug seen in the Session 14 premarket run: TradePlanAgent for
long_term mode was reading Timeframe.D1's last candle, whose Close can be
NaN when the daily bar hasn't been finalized yet by the data feed, and
propagating that NaN straight into entry_price / stop_loss / take_profit
without ever checking it.

Drop this into your test suite (e.g. tests/test_trade_plan.py) and run:
    pytest tests/test_trade_plan.py -v

ASSUMPTIONS made about your codebase that you may need to adjust:
  - BaseAgent's __init__ accepts `mode` as a keyword or positional arg and
    stores it as `self.mode`. If your BaseAgent constructor differs, adjust
    the `_make_agent()` helper below.
  - decision_signal only needs `.signal_type` and `.confidence` read on it
    (confirmed from the run() method shown) — a SimpleNamespace stand-in is
    used instead of a real core.models.Signal to avoid depending on that
    class's exact constructor.
"""
import math
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

import core.config as cfg
from agents.trade_plan import TradePlanAgent
from core.config import TradingMode
from core.constants import SignalType


def _make_df(closes, highs=None, lows=None):
    """Minimal OHLC dataframe. High/Low default to a small band around Close
    (falling back to a flat 100/99 band on NaN closes) so calculate_atr has
    something real to chew on regardless of which rows are NaN."""
    n = len(closes)
    highs = highs or [ (c if c == c else 101.0) + 1.0 for c in closes ]
    lows  = lows  or [ (c if c == c else 99.0)  - 1.0 for c in closes ]
    return pd.DataFrame({"Open": lows, "High": highs, "Low": lows, "Close": closes})


def _make_agent(mode: TradingMode) -> TradePlanAgent:
    return TradePlanAgent(mode)


def _buy_decision(confidence=0.7):
    return SimpleNamespace(signal_type=SignalType.BUY, confidence=confidence)


@pytest.fixture(autouse=True)
def _no_kelly_history():
    """Force the ATR-sizing branch (not Kelly) so tests don't need a real
    trade-history DB — patches the name as imported into trade_plan.py."""
    with patch("agents.trade_plan.get_historical_performance", return_value=(0, 0, 0)):
        yield


#  Session 52 (Bug 7): calculate_atr() genuinely needs >=14 bars (by design —
#  see signals/indicators/volatility.py's docstring) and these tests were
#  only ever feeding 1-3 candles, so TradePlanAgent.run() was hitting its
#  "ATR unavailable (insufficient data)" early return before any of the
#  NaN-close-guard logic these tests exist to check ever ran — a test-data
#  problem, not a regression in the guard itself. _run_with_candles() now
#  prepends 17 neutral, valid earlier candles (17 + the 3-candle tails below
#  = 20 rows, comfortably past the 14-bar minimum) so ATR resolves and the
#  guard logic actually executes. Prepending only shifts earlier indices —
#  iloc[-1]/iloc[-2] (what the guard and these assertions read) are still
#  exactly the last/second-to-last values of the `closes` list as written.
_NEUTRAL_HISTORY = [100.0 + i * 0.1 for i in range(17)]


def _run_with_candles(closes, mode=TradingMode.LONG_TERM, portfolio_value=100_000.0):
    df = _make_df(_NEUTRAL_HISTORY + list(closes))
    agent = _make_agent(mode)
    with patch("agents.trade_plan.get_candles", return_value=df):
        return agent.run(
            ticker="TEST",
            decision_signal=_buy_decision(),
            portfolio_value=portfolio_value,
        )


def _run_with_raw_candles(closes, mode=TradingMode.LONG_TERM, portfolio_value=100_000.0):
    """No history prepended — used only by the two genuine single-row edge
    cases below, where the test is specifically about there being no history
    at all. (With real history, ATR would resolve and this helper would be
    identical to _run_with_candles().)"""
    df = _make_df(closes)
    agent = _make_agent(mode)
    with patch("agents.trade_plan.get_candles", return_value=df):
        return agent.run(
            ticker="TEST",
            decision_signal=_buy_decision(),
            portfolio_value=portfolio_value,
        )


class TestNanCloseGuard:

    def test_normal_case_uses_todays_close(self):
        """Sanity check: unaffected happy path still works after the patch."""
        output = _run_with_candles([170.0, 171.5, 172.85])
        plan = output.raw_data["plan"]
        assert plan.entry_price == pytest.approx(172.85)
        assert plan.quantity > 0
        assert not math.isnan(plan.stop_loss)
        assert not math.isnan(plan.take_profit)

    def test_todays_close_nan_falls_back_to_yesterday(self):
        """The core bug scenario: today's D1 bar isn't finalized yet."""
        output = _run_with_candles([170.0, 171.5, float("nan")])
        plan = output.raw_data["plan"]
        assert plan.entry_price == pytest.approx(171.5)
        assert plan.quantity > 0
        assert not math.isnan(plan.stop_loss)
        assert not math.isnan(plan.take_profit)
        assert "nan" not in output.summary.lower()

    def test_today_and_yesterday_both_nan_does_not_fall_back_further(self):
        """Must refuse the trade, not silently reach back to t-2 or beyond."""
        output = _run_with_candles([170.0, float("nan"), float("nan")])
        assert output.raw_data == {} or "plan" not in output.raw_data
        assert "no valid close price" in output.summary.lower()
        assert "t and t-1" in output.summary.lower() or "t-1" in output.summary.lower()

    def test_single_row_and_it_is_nan(self):
        """Edge case: only one candle exists at all, and it's NaN.

        Session 52 (Bug 7): with genuinely no history, calculate_atr() (which
        needs >=14 bars) returns None before TradePlanAgent.run() ever
        reaches the NaN-close guard this test class is about — the real
        failure is "ATR unavailable," not "no valid close price." Updated to
        assert what actually happens: no plan is built, and nothing raises."""
        output = _run_with_raw_candles([float("nan")])
        assert output.raw_data == {} or "plan" not in output.raw_data
        assert "atr" in output.summary.lower() or "no valid close price" in output.summary.lower()

    def test_single_row_and_it_is_valid(self):
        """Edge case: only one candle exists, and it's a real number.

        Session 52 (Bug 7): this used to assert a plan gets built, but with
        genuinely no history calculate_atr() always returns None regardless
        of whether the one candle's Close is valid or NaN — a single row can
        never reach the 14-bar minimum. Updated to assert what actually
        happens: no plan is built, and nothing raises."""
        output = _run_with_raw_candles([172.85])
        assert output.raw_data == {} or "plan" not in output.raw_data

    def test_no_nan_reaches_the_final_trade_plan_object(self):
        """Belt-and-suspenders: whatever path is taken, nothing NaN should
        ever end up inside the TradePlan object itself."""
        for closes in ([170.0, 171.5, 172.85], [170.0, 171.5, float("nan")]):
            output = _run_with_candles(closes)
            plan = output.raw_data["plan"]
            assert not math.isnan(plan.entry_price)
            assert not math.isnan(plan.stop_loss)
            assert not math.isnan(plan.take_profit)
            assert not math.isnan(plan.quantity)

    def test_intraday_mode_unaffected_by_this_fix(self):
        """M15 candles (intraday) always land on a closed bar — confirm the
        guard doesn't change behavior when there's no NaN to begin with."""
        output = _run_with_candles([170.0, 171.2, 171.8], mode=TradingMode.INTRADAY)
        plan = output.raw_data["plan"]
        assert plan.entry_price == pytest.approx(171.8)


class TestPositionCap:
    """
    Bug 2: neither ATR sizing nor the Kelly min ever applied
    cfg.MAX_POSITION_SIZE_PCT (5%) — RiskManagerAgent.validate_plan() resizes
    later, but TradePlanAgent itself produced grossly oversized quantities in
    the meantime (e.g. the real Oct 4 premarket run: AAPL intraday 1603
    shares, ~$535k notional, against a 5% cap of ~15 shares).
    """

    def test_quantity_exceeding_cap_is_reduced_to_cap(self):
        output = _run_with_candles([172.0, 173.0, 174.85], portfolio_value=100_000.0)
        plan = output.raw_data["plan"]
        max_shares = math.floor(100_000.0 * cfg.MAX_POSITION_SIZE_PCT / plan.entry_price)
        assert plan.quantity == max_shares
        assert "capped" in plan.reasoning.lower()

    def test_quantity_below_cap_is_unchanged(self):
        # Same ATR-ish sizing (the synthetic candles' fixed +/-1 High/Low
        # band keeps ATR roughly price-independent), but a cheap enough
        # price that the 5%-of-portfolio cap is nowhere near binding.
        output = _run_with_candles([9.0, 9.5, 10.0], portfolio_value=100_000.0)
        plan = output.raw_data["plan"]
        max_shares = math.floor(100_000.0 * cfg.MAX_POSITION_SIZE_PCT / plan.entry_price)
        assert plan.quantity <= max_shares
        assert "capped" not in plan.reasoning.lower()

    def test_zero_max_shares_returns_no_plan_and_does_not_raise(self):
        output = _run_with_candles([900_000.0, 950_000.0, 1_000_000.0], portfolio_value=100_000.0)
        assert output.raw_data == {} or "plan" not in output.raw_data
        assert "cap" in output.summary.lower()

    def test_kelly_path_is_also_capped(self):
        with patch("agents.trade_plan.get_historical_performance", return_value=(0.6, 100.0, 50.0)), \
             patch("agents.trade_plan.kelly_position_size", return_value=100.0):
            output = _run_with_candles([172.0, 173.0, 174.85], portfolio_value=100_000.0)
        plan = output.raw_data["plan"]
        max_shares = math.floor(100_000.0 * cfg.MAX_POSITION_SIZE_PCT / plan.entry_price)
        assert plan.quantity == max_shares
        assert "kelly" in plan.reasoning.lower()
        assert "capped" in plan.reasoning.lower()

    def test_oct_4_aapl_numbers_give_15_shares(self):
        """Direct regression check against the real numbers from the task:
        intraday AAPL @ 333.79, portfolio $100,628.91 -> 15 shares."""
        output = _run_with_candles([330.0, 332.0, 333.79], mode=TradingMode.INTRADAY,
                                    portfolio_value=100_628.91)
        plan = output.raw_data["plan"]
        assert plan.entry_price == pytest.approx(333.79)
        assert plan.quantity == 15
