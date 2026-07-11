import pandas as pd
import logging
from dataclasses import dataclass
from core.config import TradingMode, STOP_LOSS_ATR_MULT, TAKE_PROFIT_ATR_MULT
from core.constants import Timeframe, SignalType
from signals.timeframe_aggregator import aggregate_signals
from signals.indicators.volatility import calculate_atr
from backtesting.cost_model import apply_costs

logger = logging.getLogger(__name__)


@dataclass
class _OpenPosition:
    entry_bar: int
    entry_price: float
    stop_loss: float
    take_profit: float
    entry_date: object


class SignalReplayer:
    """
    Replays signals bar-by-bar on test data with no lookahead bias.

    Position lifecycle:
      - Enter on BUY / STRONG_BUY signal (next bar open approximated as current close).
      - Hold while signals are NEUTRAL or BUY.
      - Exit on whichever comes first:
          1. Bar low touches or crosses stop loss  → exit at stop loss price.
          2. Bar high touches or crosses take profit → exit at take profit price.
          3. SELL / STRONG_SELL signal fires        → exit at bar close.
      - End of test window with open position     → exit at last bar close.
    One position at a time per ticker.
    """

    def __init__(self, ticker: str, mode: TradingMode, timeframe: Timeframe):
        self.ticker = ticker
        self.mode = mode
        self.timeframe = timeframe

    def replay(self, train_df: pd.DataFrame, test_df: pd.DataFrame) -> list[dict]:
        results: list[dict] = []
        history = train_df.copy()
        position: _OpenPosition | None = None

        for i in range(len(test_df)):
            bar = test_df.iloc[i]
            bar_high = float(bar["High"])
            bar_low = float(bar["Low"])
            bar_close = float(bar["Close"])

            # ── If in a position, check exit conditions first ──────────────
            if position is not None:
                exit_price: float | None = None
                exit_reason: str = ""

                if bar_low <= position.stop_loss:
                    exit_price = position.stop_loss
                    exit_reason = "stop_loss"
                elif bar_high >= position.take_profit:
                    exit_price = position.take_profit
                    exit_reason = "take_profit"
                else:
                    # Check for a sell signal on this bar's data
                    signals = aggregate_signals(self.ticker, history, self.timeframe)
                    sells = [
                        s for s in signals
                        if s.signal_type in (SignalType.SELL, SignalType.STRONG_SELL)
                    ]
                    if sells:
                        exit_price = bar_close
                        exit_reason = "sell_signal"

                if exit_price is not None:
                    raw_pnl = exit_price - position.entry_price
                    net_pnl = apply_costs(raw_pnl, position.entry_price)
                    results.append({
                        "entry_date":  position.entry_date,
                        "exit_date":   bar.name,
                        "bars_held":   i - position.entry_bar,
                        "side":        "buy",
                        "entry":       position.entry_price,
                        "stop_loss":   position.stop_loss,
                        "take_profit": position.take_profit,
                        "exit":        exit_price,
                        "exit_reason": exit_reason,
                        "raw_pnl":     raw_pnl,
                        "pnl":         net_pnl,
                    })
                    logger.debug(
                        "[%s] Exit %s @ %.2f | pnl=%.2f | reason=%s",
                        self.ticker, bar.name, exit_price, net_pnl, exit_reason,
                    )
                    position = None

            # ── If flat, look for an entry signal ─────────────────────────
            if position is None:
                signals = aggregate_signals(self.ticker, history, self.timeframe)
                buys = [
                    s for s in signals
                    if s.signal_type in (SignalType.BUY, SignalType.STRONG_BUY)
                ]
                sells = [
                    s for s in signals
                    if s.signal_type in (SignalType.SELL, SignalType.STRONG_SELL)
                ]

                if buys and not sells:
                    atr = calculate_atr(history)
                    if atr is None:
                        logger.debug("[%s] ATR unavailable at bar %s — skipping entry", self.ticker, bar.name)
                        history = pd.concat([history, test_df.iloc[[i]]])
                        continue
                    entry_price = bar_close
                    stop_loss = entry_price - atr * STOP_LOSS_ATR_MULT
                    take_profit = entry_price + atr * TAKE_PROFIT_ATR_MULT
                    position = _OpenPosition(
                        entry_bar=i,
                        entry_price=entry_price,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        entry_date=bar.name,
                    )
                    logger.debug(
                        "[%s] Entry @ %.2f | SL=%.2f TP=%.2f | bar=%s",
                        self.ticker, entry_price, stop_loss, take_profit, bar.name,
                    )

            history = pd.concat([history, test_df.iloc[[i]]])

        # ── Position still open at end of window — record as unrealized, do not force-close ──
        if position is not None:
            last_close = float(test_df.iloc[-1]["Close"])
            unrealized = last_close - position.entry_price
            results.append({
                "entry_date":    position.entry_date,
                "exit_date":     None,
                "bars_held":     len(test_df) - 1 - position.entry_bar,
                "side":          "buy",
                "entry":         position.entry_price,
                "stop_loss":     position.stop_loss,
                "take_profit":   position.take_profit,
                "exit":          None,
                "exit_reason":   "open",
                "raw_pnl":       None,
                "pnl":           None,
                "unrealized_pnl": unrealized,
            })
            logger.debug(
                "[%s] Position still open at window end | entry=%.2f | unrealized=%.2f",
                self.ticker, position.entry_price, unrealized,
            )

        return results
