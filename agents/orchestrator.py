import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
from core.config import TradingMode, WATCHLIST, ALPACA_API_KEY, ALPACA_SECRET_KEY, PAPER_TRADING
from core.constants import RegimeType, SignalType, OrderSide, OrderType, Timeframe
from core.models import AgentOutput, Signal, TradePlan
from agents.technical import TechnicalAgent
from agents.news_sentiment import NewsSentimentAgent
from agents.fundamental import FundamentalAgent
from agents.market_regime import MarketRegimeAgent
from agents.memory_agent import MemoryAgent
from agents.risk_manager import RiskManagerAgent
from agents.decision import DecisionAgent
from agents.trade_plan import TradePlanAgent
from agents.critic import CriticAgent
from agents.execution import ExecutionAgent
from data.storage.signal_store import save_signals
from data.storage.decision_store import (
    get_filled_trades, close_trade,
    has_pending_exit, get_open_filled_entry_trade,
    save_critic_verdict, save_hold_decision,
)
from data.cache.market_data_cache import get_candles
from data.feeds.price_feed import fetch_snapshot
from llm.prompts.spike_analysis import build_structured_summary
from risk.circuit_breakers import check_circuit_breakers
from execution.broker.alpaca import AlpacaBroker

logger = logging.getLogger(__name__)

_ET           = ZoneInfo("America/New_York")
_MARKET_OPEN  = time(9, 30)
_MARKET_CLOSE = time(16, 0)


def _easter_date(year: int) -> datetime.date:
    """Anonymous Gregorian algorithm (O'Beirne) — returns Easter Sunday for the given year."""
    from datetime import date as _date
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(114 + h + l - 7 * m, 31)
    return _date(year, month, day + 1)


def _nyse_holidays(year: int) -> set:
    """Return the set of NYSE-observed holiday dates for the given year.

    Uses pandas USFederalHolidayCalendar (covers 9 of 10 NYSE holidays) plus
    Good Friday (the one holiday pandas omits).
    """
    from datetime import timedelta
    import pandas as pd
    from pandas.tseries.holiday import USFederalHolidayCalendar
    cal = USFederalHolidayCalendar()
    federal = set(
        pd.Timestamp(d).date()
        for d in cal.holidays(start=f"{year}-01-01", end=f"{year}-12-31")
    )
    good_friday = _easter_date(year) - timedelta(days=2)
    return federal | {good_friday}


def _is_market_open() -> bool:
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_TRADING)
        return bool(client.get_clock().is_open)
    except Exception:
        pass
    now = datetime.now(_ET)
    if now.weekday() >= 5:
        return False
    if now.date() in _nyse_holidays(now.year):
        return False
    return _MARKET_OPEN <= now.time() < _MARKET_CLOSE


class Orchestrator:
    """Coordinates all agents in the correct order for each ticker."""

    def __init__(self, mode: TradingMode, portfolio_value: float = 100_000.0):
        self.mode            = mode
        self.portfolio_value = portfolio_value

        self.technical  = TechnicalAgent(mode)
        self.news       = NewsSentimentAgent(mode)
        self.fundamental = FundamentalAgent(mode)
        self.regime     = MarketRegimeAgent(mode)
        self.memory     = MemoryAgent(mode)
        self.risk       = RiskManagerAgent(mode)
        self.decision   = DecisionAgent(mode)
        self.trade_plan = TradePlanAgent(mode)
        self.critic     = CriticAgent(mode)
        self.execution  = ExecutionAgent(mode)

    def _reconcile_closed_trades(self) -> dict[str, dict]:
        """
        Detect per-lane position closures by querying Alpaca for a filled SELL order
        whose client_order_id matches the entry trade's "exit-{id}-{mode}" tag.
        This avoids the symbol-level ambiguity of get_open_symbols(): if two lanes
        (intraday + long_term) both hold the same ticker, closing one does not
        remove the symbol from Alpaca's positions, so symbol presence alone cannot
        distinguish which lane closed.
        """
        filled = get_filled_trades(strategy_mode=self.mode.value)
        if not filled:
            return {}

        try:
            broker = AlpacaBroker()
        except Exception as exc:
            logger.warning("Could not initialise broker for reconciliation: %s", exc)
            return {}

        closed_outcomes: dict[tuple[str, str], dict] = {}
        now = datetime.now(timezone.utc)

        for trade in filled:
            ticker = trade["ticker"]
            trade_id = trade["id"]
            strategy_mode = trade["strategy_mode"]

            # The exit order for this specific lane carries this client_order_id
            exit_coid = f"exit-{trade_id}-{strategy_mode}"
            exit_price = broker.get_fill_by_client_order_id(exit_coid)

            if exit_price is None:
                continue  # no filled exit order yet — lane still open

            pnl = self._estimate_pnl(trade, exit_price)
            try:
                close_trade(trade_id, pnl, now)
            except Exception:
                logger.exception("[%s][%s] DB write failed for close_trade(id=%d) — will retry next cycle",
                                 ticker, strategy_mode, trade_id)
                continue
            closed_outcomes[(ticker, strategy_mode)] = {
                "pnl": pnl,
                "closed_at": now.isoformat(),
                "reasoning": trade.get("reasoning", ""),
            }
            logger.info("[%s][%s] Position closed via exit coid — pnl=%.2f",
                        ticker, strategy_mode, pnl or 0.0)

        return closed_outcomes

    def _estimate_pnl(self, trade: dict, exit_price: float) -> float | None:
        """Compute realized PnL from the confirmed exit fill price."""
        entry_price = trade.get("filled_price") or trade.get("entry_price")
        qty = trade.get("quantity", 0)
        if not entry_price or not qty:
            return None
        raw = (exit_price - entry_price) * qty
        return round(-raw if trade.get("side") == "sell" else raw, 2)

    def _flat_position(self, entry_trade: dict, reason: str) -> None:
        """Submit a market order to close an open position (long or short).

        Closing side opposes the open side: BUY to close a short, SELL to close a long.
        Delegates entirely to ExecutionAgent so DB management, client_order_id
        construction (exit-{entry_id}-{mode}), and order routing all go through
        the same code path as normal signal-driven trades.
        """
        ticker   = entry_trade["ticker"]
        qty      = entry_trade.get("quantity", 0)
        mode_str = entry_trade.get("strategy_mode", self.mode.value)

        tf = Timeframe.M15 if self.mode == TradingMode.INTRADAY else Timeframe.D1
        df = get_candles(ticker, tf)
        current_price = float(df["Close"].iloc[-1]) if not df.empty else (entry_trade.get("entry_price") or 0.0)

        open_side = entry_trade.get("side", "buy")
        closing_side = OrderSide.BUY if open_side == "sell" else OrderSide.SELL

        exit_plan = TradePlan(
            ticker=ticker,
            side=closing_side,
            order_type=OrderType.MARKET,
            quantity=qty,
            entry_price=current_price,
            stop_loss=0.0,
            take_profit=999_999.0,
            confidence=1.0,
            reasoning=reason,
            strategy_mode=mode_str,
        )
        logger.info("[%s][%s] Submitting flat exit (%s) — %s", ticker, mode_str, closing_side.value, reason)
        self.execution.run(ticker, plan=exit_plan)

    def check_sl_tp_exits(self) -> None:
        """Check all open positions for SL/TP breaches and submit market exits if triggered.

        Market hours guard at top prevents submitting orders when the exchange is closed.
        Double-submission is blocked by has_pending_exit() before every exit attempt.
        """
        if not _is_market_open():
            return

        filled = get_filled_trades(self.mode.value)

        for trade in filled:
            ticker     = trade["ticker"]
            trade_id   = trade["id"]
            stop_loss  = trade.get("stop_loss")
            take_profit = trade.get("take_profit")
            qty        = trade.get("quantity", 0)

            if stop_loss is None or take_profit is None or not qty:
                continue

            if has_pending_exit(trade_id, self.mode.value):
                continue

            try:
                current_price = fetch_snapshot(ticker).price
            except Exception as exc:
                logger.warning("[%s] Could not fetch live price for SL/TP check: %s", ticker, exc)
                continue

            trade_side = trade.get("side", "buy")
            if trade_side == "sell":
                sl_breach = current_price >= stop_loss
                tp_breach = current_price <= take_profit
            else:
                sl_breach = current_price <= stop_loss
                tp_breach = current_price >= take_profit

            if sl_breach:
                breach = "SL"
            elif tp_breach:
                breach = "TP"
            else:
                continue

            logger.info("[%s][%s] Price %.2f breached %s (SL=%.2f TP=%.2f) — submitting exit",
                        ticker, self.mode.value, current_price, breach, stop_loss, take_profit)
            self._flat_position(trade, reason=f"Price breached {breach}")

    def run_ticker(self, ticker: str, closed_trade: dict | None = None) -> dict[str, AgentOutput]:
        logger.info("=== Running pipeline for %s [%s] ===", ticker, self.mode.value)
        results: dict[str, AgentOutput] = {}

        # Phase 1: gather signals — agents are independent, run in parallel
        _phase1 = {
            "regime":      self.regime,
            "technical":   self.technical,
            "news":        self.news,
            "memory":      self.memory,
            "fundamental": self.fundamental,
        }
        with ThreadPoolExecutor(max_workers=5) as p1_pool:
            p1_futures = {name: p1_pool.submit(agent.run, ticker) for name, agent in _phase1.items()}
            for name, fut in p1_futures.items():
                try:
                    results[name] = fut.result(timeout=30)
                except TimeoutError:
                    logger.error("[%s] Phase 1 agent '%s' timed out — using empty output", ticker, name)
                    results[name] = AgentOutput(agent_name=name, ticker=ticker, signals=[], summary="Timed out.")
                except Exception:
                    logger.exception("[%s] Phase 1 agent '%s' failed — using empty output", ticker, name)
                    results[name] = AgentOutput(agent_name=name, ticker=ticker, signals=[], summary="Failed.")

        # Phase 2: risk gate
        results["risk"] = self.risk.run(ticker)

        # Extract regime label for downstream agents
        regime_label: str = results["regime"].raw_data.get("regime", RegimeType.UNKNOWN.value)

        # Phase 3: aggregate decision
        all_signals: list[Signal] = []
        for output in results.values():
            all_signals.extend(output.signals)
        save_signals(all_signals)

        results["decision"] = self.decision.run(ticker, signals=all_signals, regime=regime_label)
        decision_signal = results["decision"].signals[0] if results["decision"].signals else None

        # Memory write-back: triggered once per closed trade, after we have the new bias
        if closed_trade is not None and decision_signal is not None:
            new_bias = (
                "bullish" if decision_signal.signal_type.value in ("buy", "strong_buy")
                else "bearish" if decision_signal.signal_type.value in ("sell", "strong_sell")
                else "neutral"
            )
            self.memory.update(ticker, {
                "directional_bias": new_bias,
                "bias_confidence": decision_signal.confidence,
                "reasoning": results["decision"].summary,
                "pnl": closed_trade.get("pnl"),
                "closed_at": closed_trade.get("closed_at"),
            })
            logger.info("[%s] Memory written back — new bias=%s pnl=%.2f",
                        ticker, new_bias, closed_trade.get("pnl") or 0.0)

        if decision_signal is None or decision_signal.signal_type == SignalType.NEUTRAL:
            logger.info("[%s] No actionable signal (%s) — skipping trade plan", ticker,
                        decision_signal.signal_type.value if decision_signal else "None")
            return results

        # Hard gate: suppress new long entries in a confirmed bear trend
        if (regime_label == RegimeType.BEAR_TREND.value
                and decision_signal.signal_type in (SignalType.BUY, SignalType.STRONG_BUY)):
            logger.info("[%s] Regime BEAR_TREND — suppressing new long entry", ticker)
            return results

        # Reversal exit: if an open position opposes the new signal, go flat and skip Phase 4.
        # This prevents a stale long from sitting open when the system issues a SELL.
        open_entry = get_open_filled_entry_trade(ticker, self.mode.value)
        if open_entry is not None:
            open_side = open_entry.get("side", "buy")
            signal_is_sell = decision_signal.signal_type in (SignalType.SELL, SignalType.STRONG_SELL)
            signal_is_buy  = decision_signal.signal_type in (SignalType.BUY,  SignalType.STRONG_BUY)
            reversal = (open_side == "buy" and signal_is_sell) or (open_side == "sell" and signal_is_buy)
            if reversal:
                logger.info("[%s][%s] Signal reversal detected — exiting %s position",
                            ticker, self.mode.value, open_side)
                if not has_pending_exit(open_entry["id"], self.mode.value):
                    self._flat_position(open_entry, reason="Signal reversal")
                return results

        # HOLD logging: open position + non-reversal signal → record hold, skip Phase 4
        # Hold fires when the new signal agrees with the open side (same direction = no action).
        if open_entry is not None:
            if open_side == "sell":
                signal_is_hold = decision_signal.signal_type in (SignalType.SELL, SignalType.STRONG_SELL)
            else:
                signal_is_hold = decision_signal.signal_type in (SignalType.BUY, SignalType.STRONG_BUY)
            if signal_is_hold:
                tf = Timeframe.M15 if self.mode == TradingMode.INTRADAY else Timeframe.D1
                df = get_candles(ticker, tf)
                current_price = float(df["Close"].iloc[-1]) if not df.empty else None
                entry_price = open_entry.get("entry_price") or open_entry.get("filled_price")
                qty = open_entry.get("quantity", 0)
                raw_pnl = (
                    (current_price - entry_price) * qty
                    if current_price is not None and entry_price is not None
                    else None
                )
                unrealized_pnl = (
                    round(-raw_pnl if open_side == "sell" else raw_pnl, 2)
                    if raw_pnl is not None else None
                )
                try:
                    save_hold_decision(
                        ticker=ticker,
                        strategy_mode=self.mode.value,
                        signal_type=decision_signal.signal_type.value,
                        confidence=decision_signal.confidence,
                        regime=regime_label,
                        reasoning=results["decision"].summary,
                        entry_price=entry_price,
                        current_price=current_price,
                        unrealized_pnl=unrealized_pnl,
                    )
                except Exception:
                    logger.exception("[%s] Failed to save hold decision.", ticker)
                logger.info("[%s] HOLD — open %s position retained. Signal=%s unrealized_pnl=%s",
                            ticker, open_side, decision_signal.signal_type.value, unrealized_pnl)
                return results
            # Every combination (reversal, hold) is handled above. Reaching here means an
            # unmatched signal+side pair — block Phase 4 rather than risk a duplicate entry.
            logger.error(
                "[%s] Unmatched fall-through with open %s position and signal %s — blocking Phase 4",
                ticker, open_side, decision_signal.signal_type.value,
            )
            return results

        # ETB pre-flight: block short entries on hard-to-borrow securities.
        # Only applies when there is no open position (otherwise we hit hold/reversal above).
        if decision_signal.signal_type in (SignalType.SELL, SignalType.STRONG_SELL):
            try:
                if not AlpacaBroker().is_etb(ticker):
                    logger.warning("[%s] ETB check failed — blocking short entry", ticker)
                    return results
            except Exception:
                logger.exception("[%s] ETB check error — blocking short entry as fail-safe", ticker)
                return results

        # Phase 4: plan + critique + execute
        results["trade_plan"] = self.trade_plan.run(
            ticker,
            decision_signal=decision_signal,
            portfolio_value=self.portfolio_value,
            regime=regime_label,
        )
        plan = results["trade_plan"].raw_data.get("plan")

        structured_summary = build_structured_summary(regime_label, all_signals)
        results["critic"] = self.critic.run(ticker, plan=plan, supporting_summary=structured_summary)

        critic_rd = results["critic"].raw_data
        entry_price = plan.entry_price if plan else None
        rr_ratio = (
            round((plan.take_profit - (entry_price or 0)) / max((entry_price or 0) - plan.stop_loss, 0.01), 2)
            if plan else None
        )
        critic_signal = results["critic"].signals[0] if results["critic"].signals else None
        if critic_signal is None:
            logger.warning("[%s] Critic returned no signal — blocking trade as fail-safe.", ticker)
        critic_blocked = critic_signal is None or critic_signal.signal_type.value in ("sell", "strong_sell")

        if critic_blocked:
            logger.info("[%s] Critic blocked the trade.", ticker)

        trade_id: int | None = None
        if not critic_blocked and plan:
            ok, reason = self.risk.validate_plan(plan, self.portfolio_value)
            if ok:
                results["execution"] = self.execution.run(ticker, plan=plan, all_signals=all_signals)
                exec_rd = results["execution"].raw_data if "execution" in results else {}
                trade_id = exec_rd.get("trade_id")
            else:
                logger.warning("[%s] Risk validation failed: %s", ticker, reason)

        try:
            save_critic_verdict(
                ticker=ticker,
                strategy_mode=self.mode.value,
                approved=critic_rd.get("approved", False),
                confidence=critic_rd.get("confidence"),
                reasoning=critic_rd.get("reasoning"),
                biggest_risk=critic_rd.get("biggest_risk"),
                failure_scenario=critic_rd.get("failure_scenario"),
                entry_price=entry_price,
                stop_loss=plan.stop_loss if plan else None,
                take_profit=plan.take_profit if plan else None,
                rr_ratio=rr_ratio,
                trade_id=trade_id,
            )
        except Exception:
            logger.exception("[%s] Failed to save critic verdict.", ticker)

        return results

    def _refresh_portfolio_and_reconcile(self) -> dict[tuple[str, str], dict]:
        try:
            self.portfolio_value = AlpacaBroker().get_account()["portfolio_value"]
            logger.info("Portfolio value refreshed: $%.2f", self.portfolio_value)
        except Exception as exc:
            logger.warning("Could not refresh portfolio value (%s) — using last known $%.2f", exc, self.portfolio_value)

        closed_outcomes = self._reconcile_closed_trades()
        self.check_sl_tp_exits()
        return closed_outcomes

    def run_maintenance(self) -> None:
        """
        Session 34, Decision 2: lightweight 60-second cycle. Only SL/TP breach
        checks and closed-trade reconciliation — both depend on live price, not
        candle data, so they still need to run every 60s. The full 11-agent
        pipeline (signals, LLM, execution) runs separately every 5 minutes via
        run_all(), since 1-minute candles were removed and re-running every
        agent every 60s wasted Ollama/yfinance/FinBERT calls on unchanged data.
        """
        if not _is_market_open():
            return
        triggered, reason = check_circuit_breakers()
        if triggered:
            logger.warning("CIRCUIT BREAKER TRIGGERED — halting maintenance cycle. Reason: %s", reason)
            return
        self._refresh_portfolio_and_reconcile()

    def run_all(self) -> dict[str, dict[str, AgentOutput]]:
        if not _is_market_open():
            logger.info("Market closed — skipping cycle.")
            return {}

        triggered, reason = check_circuit_breakers()
        if triggered:
            logger.warning("CIRCUIT BREAKER TRIGGERED — halting full cycle. Reason: %s", reason)
            return {}

        closed_outcomes = self._refresh_portfolio_and_reconcile()

        all_results: dict[str, dict[str, AgentOutput]] = {}
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(self.run_ticker, ticker, closed_outcomes.get((ticker, self.mode.value))): ticker
                for ticker in WATCHLIST
            }
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    all_results[ticker] = future.result(timeout=45)
                except TimeoutError:
                    logger.error("[%s] run_ticker timed out after 45s — skipping ticker.", ticker)
                except Exception:
                    logger.exception("[%s] Unhandled error in run_ticker — skipping ticker.", ticker)
        return all_results
