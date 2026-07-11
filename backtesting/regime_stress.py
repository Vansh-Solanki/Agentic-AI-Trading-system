import logging
import pandas as pd
from core.config import TradingMode
from core.constants import Timeframe, RegimeType
from backtesting.engine import BacktestEngine

logger = logging.getLogger(__name__)

# Hand-labeled regime periods for SPY as a rough proxy
_REGIME_PERIODS = {
    RegimeType.BULL_TREND:      ("2020-04-01", "2021-12-31"),
    RegimeType.BEAR_TREND:      ("2022-01-01", "2022-10-31"),
    RegimeType.HIGH_VOLATILITY: ("2020-02-01", "2020-03-31"),
    RegimeType.RANGING:         ("2023-03-01", "2023-08-31"),
}


def run_regime_stress(ticker: str, mode: TradingMode) -> dict[str, dict]:
    """Run backtests over each labeled regime period and return per-regime summaries."""
    results: dict[str, dict] = {}

    for regime, (start, end) in _REGIME_PERIODS.items():
        engine = BacktestEngine(ticker, mode)
        try:
            from data.feeds.price_feed import fetch_candles
            df = fetch_candles(ticker, Timeframe.D1)
            if df.empty:
                continue
            mask = (df.index >= start) & (df.index <= end)
            sliced = df[mask]
            if len(sliced) < 30:
                continue

            mid = len(sliced) // 2
            from backtesting.replay import SignalReplayer
            replayer = SignalReplayer(ticker, mode, Timeframe.D1)
            trades = replayer.replay(sliced.iloc[:mid], sliced.iloc[mid:])
            trade_df = pd.DataFrame(trades)
            results[regime.value] = engine.summary(trade_df) if not trade_df.empty else {"note": "no trades"}
        except Exception:
            logger.exception("Regime stress failed for %s / %s", ticker, regime.value)

    return results
