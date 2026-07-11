import logging
import pandas as pd
from datetime import datetime
from core.config import TradingMode
from core.constants import Timeframe
from data.feeds.price_feed import fetch_candles
from backtesting.cost_model import apply_costs
from backtesting.replay import SignalReplayer

logger = logging.getLogger(__name__)


class BacktestEngine:
    """
    Walk-forward backtest runner.
    Splits history into train/test windows and replays signals with no lookahead.
    """

    def __init__(
        self,
        ticker: str,
        mode: TradingMode,
        timeframe: Timeframe = Timeframe.D1,
        train_days: int = 252,
        test_days: int = 63,
    ):
        self.ticker     = ticker
        self.mode       = mode
        self.timeframe  = timeframe
        self.train_days = train_days
        self.test_days  = test_days

    def run(self) -> pd.DataFrame:
        """Returns a DataFrame of trade results across all walk-forward windows."""
        df = fetch_candles(self.ticker, self.timeframe)
        if df.empty:
            logger.error("No data for %s", self.ticker)
            return pd.DataFrame()

        all_results: list[dict] = []
        step = self.test_days
        total = self.train_days + self.test_days

        for start in range(0, len(df) - total, step):
            train_df = df.iloc[start : start + self.train_days]
            test_df  = df.iloc[start + self.train_days : start + total]
            replayer = SignalReplayer(self.ticker, self.mode, self.timeframe)
            window_results = replayer.replay(train_df, test_df)
            all_results.extend(window_results)

        results_df = pd.DataFrame(all_results)
        logger.info("Backtest complete: %d trades | %s", len(results_df), self.ticker)
        return results_df

    def summary(self, results: pd.DataFrame) -> dict:
        if results.empty:
            return {}
        closed = results[results["exit_reason"] != "open"].copy()
        open_pos = results[results["exit_reason"] == "open"]
        wins  = closed[closed["pnl"] > 0]
        total = len(closed)
        exit_counts = closed["exit_reason"].value_counts().to_dict() if "exit_reason" in closed.columns else {}
        return {
            "ticker":            self.ticker,
            "total_trades":      total,
            "open_positions":    len(open_pos),
            "win_rate":          len(wins) / total if total else 0,
            "total_pnl":         closed["pnl"].sum(),
            "avg_pnl":           closed["pnl"].mean(),
            "avg_bars_held":     closed["bars_held"].mean() if "bars_held" in closed.columns else None,
            "max_drawdown":      self._max_drawdown(closed["pnl"].cumsum()),
            "sharpe":            self._sharpe(closed["pnl"]),
            "exits_by_reason":   exit_counts,
            "unrealized_pnl":    float(open_pos["unrealized_pnl"].sum()) if not open_pos.empty else 0.0,
        }

    def _max_drawdown(self, cum_pnl: pd.Series) -> float:
        roll_max = cum_pnl.cummax()
        drawdown = cum_pnl - roll_max
        return float(drawdown.min())

    def _sharpe(self, pnl: pd.Series, risk_free: float = 0.05) -> float:
        if pnl.std() == 0:
            return 0.0
        return float((pnl.mean() * 252 - risk_free) / (pnl.std() * (252 ** 0.5)))
