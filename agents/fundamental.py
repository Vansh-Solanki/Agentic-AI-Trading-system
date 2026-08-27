import logging
from datetime import datetime, timezone
import numpy as np
from core.constants import SignalType, SignalSource, Timeframe
from core.models import AgentOutput, Signal, FundamentalData
from agents.base_agent import BaseAgent
from data.feeds.fundamentals_feed import fetch_fundamentals
from data.storage.fundamentals_store import get_fundamentals_cache, save_fundamentals_cache

logger = logging.getLogger(__name__)

_MIN_FLOOR = 0.50
_STALENESS_WARN_HOURS = 12


def _sigmoid_conf(x: float, steepness: float) -> float:
    return 1.0 / (1.0 + np.exp(-steepness * x))


def _pe_buy_confidence(pe: float, center: float = 15.0, steepness: float = 0.15) -> float:
    """Buy confidence rises as P/E drops below center (15). Crosses 0.50 at pe=center."""
    return _sigmoid_conf(center - pe, steepness)


def _pe_sell_confidence(pe: float, center: float = 40.0, steepness: float = 0.10) -> float:
    """Sell confidence rises as P/E climbs above center (40). Crosses 0.50 at pe=center."""
    return _sigmoid_conf(pe - center, steepness)


def _rev_growth_buy_confidence(growth: float, center: float = 0.20, steepness: float = 8.0) -> float:
    """Buy confidence rises as revenue growth climbs above center (20%). Crosses 0.50 at center."""
    return _sigmoid_conf(growth - center, steepness)


def _rev_growth_sell_confidence(growth: float, center: float = -0.05, steepness: float = 8.0) -> float:
    """Sell confidence rises as revenue growth falls below center (-5%). Crosses 0.50 at center."""
    return _sigmoid_conf(center - growth, steepness)


def _pb_buy_confidence(pb: float, center: float = 2.0, steepness: float = 0.60) -> float:
    """Buy confidence rises as P/B drops below center (2.0). Crosses 0.50 at pb=center."""
    return _sigmoid_conf(center - pb, steepness)


def _pb_sell_confidence(pb: float, center: float = 5.0, steepness: float = 0.40) -> float:
    """Sell confidence rises as P/B climbs above center (5.0). Crosses 0.50 at pb=center."""
    return _sigmoid_conf(pb - center, steepness)


def _de_sell_confidence(de: float, center: float = 1.5, steepness: float = 1.20) -> float:
    """Sell confidence rises as D/E climbs above center (1.5x). High leverage → bearish."""
    return _sigmoid_conf(de - center, steepness)


def _de_buy_confidence(de: float, center: float = 0.3, steepness: float = 4.0) -> float:
    """Buy confidence rises as D/E falls below center (0.3x). Low leverage → conservative/safe."""
    return _sigmoid_conf(center - de, steepness)


def _earn_growth_buy_confidence(growth: float, center: float = 0.15, steepness: float = 8.0) -> float:
    """Buy confidence rises as earnings growth climbs above center (15%). Crosses 0.50 at center."""
    return _sigmoid_conf(growth - center, steepness)


def _earn_growth_sell_confidence(growth: float, center: float = -0.05, steepness: float = 8.0) -> float:
    """Sell confidence rises as earnings growth falls below center (-5%). Crosses 0.50 at center."""
    return _sigmoid_conf(center - growth, steepness)


class FundamentalAgent(BaseAgent):
    @property
    def name(self) -> str:
        return "FundamentalAgent"

    def run(self, ticker: str, **kwargs) -> AgentOutput:
        # Session 38: fundamentals are quarterly-filing data that don't change
        # intraday. Read the pre-market batch's SQLite cache first (24h TTL) so
        # market-hours cycles never touch yfinance/_yf_lock; fall back to a
        # live fetch (and populate the cache) only on a cold cache — e.g. the
        # process started after the pre-market batch cutoff.
        data = get_fundamentals_cache(ticker)
        if data is None:
            data = fetch_fundamentals(ticker)
            save_fundamentals_cache(data)
        else:
            age_hours = (datetime.now(timezone.utc) - data.as_of).total_seconds() / 3600
            if age_hours > _STALENESS_WARN_HOURS:
                logger.warning(
                    "[%s] Fundamentals cache hit is %.1fh old (warn threshold %dh, TTL 24h).",
                    ticker, age_hours, _STALENESS_WARN_HOURS,
                )
        signals = self._score(data)
        summary = (
            f"P/E={data.pe_ratio}, P/B={data.pb_ratio}, "
            f"RevGrowth={data.revenue_growth}, EarnGrowth={data.earnings_growth}, "
            f"D/E={data.debt_to_equity}"
        )
        # Session 47 item 6: use the data's actual fetched_at/as_of timestamp
        # (accurate on both cache hit and live fetch) instead of AgentOutput's
        # default_factory, which would otherwise always stamp "now" even when
        # the served fundamentals are close to the 24h TTL boundary.
        output = AgentOutput(
            agent_name=self.name,
            ticker=ticker,
            signals=signals,
            summary=summary,
            raw_data=data.__dict__,
            timestamp=data.as_of,
        )
        self._log_output(output)
        return output

    def _score(self, data: FundamentalData) -> list[Signal]:
        signals: list[Signal] = []

        # Explicit "data unavailable" marker — confidence 0.0 distinguishes this from a real neutral
        if data.pe_ratio is None and data.revenue_growth is None:
            signals.append(Signal(
                ticker=data.ticker, source=SignalSource.FUNDAMENTAL,
                signal_type=SignalType.NEUTRAL, timeframe=Timeframe.MN1,
                confidence=0.0, value=None,
                reasoning="Fundamental data unavailable for this ticker",
            ))
            return signals

        # P/E scoring
        if data.pe_ratio is not None:
            if data.pe_ratio <= 0:
                # Negative/zero P/E means the company is unprofitable — not a value buy
                signals.append(Signal(
                    ticker=data.ticker, source=SignalSource.FUNDAMENTAL,
                    signal_type=SignalType.SELL, timeframe=Timeframe.MN1,
                    confidence=0.5, value=data.pe_ratio,
                    reasoning="Negative/zero P/E indicates unprofitability",
                ))
            else:
                buy_conf = _pe_buy_confidence(data.pe_ratio)
                sell_conf = _pe_sell_confidence(data.pe_ratio)
                if buy_conf >= sell_conf and buy_conf >= _MIN_FLOOR:
                    signals.append(Signal(
                        ticker=data.ticker, source=SignalSource.FUNDAMENTAL,
                        signal_type=SignalType.BUY, timeframe=Timeframe.MN1,
                        confidence=buy_conf, value=data.pe_ratio,
                        reasoning=f"P/E {data.pe_ratio:.1f} suggests undervaluation (conf={buy_conf:.3f})",
                    ))
                elif sell_conf > buy_conf and sell_conf >= _MIN_FLOOR:
                    signals.append(Signal(
                        ticker=data.ticker, source=SignalSource.FUNDAMENTAL,
                        signal_type=SignalType.SELL, timeframe=Timeframe.MN1,
                        confidence=sell_conf, value=data.pe_ratio,
                        reasoning=f"P/E {data.pe_ratio:.1f} suggests overvaluation (conf={sell_conf:.3f})",
                    ))
                # else: P/E in neutral range — no signal emitted (same floor-based pattern as RSI)

        # Revenue growth scoring
        if data.revenue_growth is not None:
            buy_conf = _rev_growth_buy_confidence(data.revenue_growth)
            sell_conf = _rev_growth_sell_confidence(data.revenue_growth)
            if buy_conf >= sell_conf and buy_conf >= _MIN_FLOOR:
                signals.append(Signal(
                    ticker=data.ticker, source=SignalSource.FUNDAMENTAL,
                    signal_type=SignalType.BUY, timeframe=Timeframe.MN1,
                    confidence=buy_conf, value=data.revenue_growth,
                    reasoning=f"Strong revenue growth {data.revenue_growth:.1%} (conf={buy_conf:.3f})",
                ))
            elif sell_conf > buy_conf and sell_conf >= _MIN_FLOOR:
                signals.append(Signal(
                    ticker=data.ticker, source=SignalSource.FUNDAMENTAL,
                    signal_type=SignalType.SELL, timeframe=Timeframe.MN1,
                    confidence=sell_conf, value=data.revenue_growth,
                    reasoning=f"Declining revenue {data.revenue_growth:.1%} (conf={sell_conf:.3f})",
                ))

        # P/B ratio scoring
        if data.pb_ratio is not None and data.pb_ratio > 0:
            buy_conf = _pb_buy_confidence(data.pb_ratio)
            sell_conf = _pb_sell_confidence(data.pb_ratio)
            if buy_conf >= sell_conf and buy_conf >= _MIN_FLOOR:
                signals.append(Signal(
                    ticker=data.ticker, source=SignalSource.FUNDAMENTAL,
                    signal_type=SignalType.BUY, timeframe=Timeframe.MN1,
                    confidence=buy_conf, value=data.pb_ratio,
                    reasoning=f"P/B {data.pb_ratio:.2f} suggests undervaluation (conf={buy_conf:.3f})",
                ))
            elif sell_conf > buy_conf and sell_conf >= _MIN_FLOOR:
                signals.append(Signal(
                    ticker=data.ticker, source=SignalSource.FUNDAMENTAL,
                    signal_type=SignalType.SELL, timeframe=Timeframe.MN1,
                    confidence=sell_conf, value=data.pb_ratio,
                    reasoning=f"P/B {data.pb_ratio:.2f} suggests overvaluation (conf={sell_conf:.3f})",
                ))

        # Debt-to-equity scoring (already normalized: yfinance 145.0 → stored as 1.45)
        if data.debt_to_equity is not None and data.debt_to_equity >= 0:
            buy_conf = _de_buy_confidence(data.debt_to_equity)
            sell_conf = _de_sell_confidence(data.debt_to_equity)
            if buy_conf >= sell_conf and buy_conf >= _MIN_FLOOR:
                signals.append(Signal(
                    ticker=data.ticker, source=SignalSource.FUNDAMENTAL,
                    signal_type=SignalType.BUY, timeframe=Timeframe.MN1,
                    confidence=buy_conf, value=data.debt_to_equity,
                    reasoning=f"D/E {data.debt_to_equity:.2f}x — conservative leverage (conf={buy_conf:.3f})",
                ))
            elif sell_conf > buy_conf and sell_conf >= _MIN_FLOOR:
                signals.append(Signal(
                    ticker=data.ticker, source=SignalSource.FUNDAMENTAL,
                    signal_type=SignalType.SELL, timeframe=Timeframe.MN1,
                    confidence=sell_conf, value=data.debt_to_equity,
                    reasoning=f"D/E {data.debt_to_equity:.2f}x — high leverage risk (conf={sell_conf:.3f})",
                ))

        # Earnings growth scoring
        if data.earnings_growth is not None:
            buy_conf = _earn_growth_buy_confidence(data.earnings_growth)
            sell_conf = _earn_growth_sell_confidence(data.earnings_growth)
            if buy_conf >= sell_conf and buy_conf >= _MIN_FLOOR:
                signals.append(Signal(
                    ticker=data.ticker, source=SignalSource.FUNDAMENTAL,
                    signal_type=SignalType.BUY, timeframe=Timeframe.MN1,
                    confidence=buy_conf, value=data.earnings_growth,
                    reasoning=f"Strong earnings growth {data.earnings_growth:.1%} (conf={buy_conf:.3f})",
                ))
            elif sell_conf > buy_conf and sell_conf >= _MIN_FLOOR:
                signals.append(Signal(
                    ticker=data.ticker, source=SignalSource.FUNDAMENTAL,
                    signal_type=SignalType.SELL, timeframe=Timeframe.MN1,
                    confidence=sell_conf, value=data.earnings_growth,
                    reasoning=f"Declining earnings {data.earnings_growth:.1%} (conf={sell_conf:.3f})",
                ))

        return signals
