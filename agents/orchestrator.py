import logging
import threading
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait, FIRST_COMPLETED
from datetime import datetime, time, timezone
from time import monotonic
from zoneinfo import ZoneInfo
from core.config import (
    TradingMode, WATCHLIST, ALPACA_API_KEY, ALPACA_SECRET_KEY, PAPER_TRADING,
    PHASE1_AGENT_TIMEOUT_SECONDS, TICKER_TIMEOUT_SECONDS,
    PREMARKET_PHASE1_TIMEOUT_SECONDS, PREMARKET_TICKER_TIMEOUT_SECONDS,
)
from core.constants import RegimeType, SignalType, OrderSide, OrderType, Timeframe, INTRADAY_TIMEFRAMES, LONGTERM_TIMEFRAMES
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
from data.cache.market_data_cache import get_candles, warm_up
from data.feeds.price_feed import fetch_snapshot
from llm.prompts.spike_analysis import build_structured_summary
from risk.circuit_breakers import check_circuit_breakers
from execution.broker.alpaca import AlpacaBroker
from execution.order_sync import sync_broker_orders

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

        # Session 39, item 68: once SL/TP monitoring runs on its own thread
        # (see scripts/run_paper.py's maintenance loop), this same Orchestrator
        # instance's _reconcile_closed_trades() and check_sl_tp_exits() can be
        # entered concurrently from that thread and from run_all() on the main
        # thread. Without this lock, two overlapping calls could both see the
        # same trade as still-open and both close it / both submit an exit
        # order. Held only around each individual method body, not across the
        # whole pipeline, so it doesn't reintroduce the starvation problem
        # the maintenance thread was split out to fix.
        self._maintenance_lock = threading.Lock()

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
        with self._maintenance_lock:
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
        Double-submission is blocked by has_pending_exit() before every exit attempt — but
        that check-then-act is itself only race-free within one thread at a time, so the
        whole method is also serialized per-instance via _maintenance_lock (Session 39,
        item 68: this can now be entered from the maintenance thread and from run_all()
        concurrently on the same Orchestrator).
        """
        if not _is_market_open():
            return

        with self._maintenance_lock:
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

    def run_ticker(self, ticker: str, closed_trade: dict | None = None, premarket_only: bool = False) -> dict[str, AgentOutput]:
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
        # Not a `with` block on purpose: exiting `with ThreadPoolExecutor(...)`
        # calls shutdown(wait=True), which blocks until every submitted thread
        # finishes — including ones already given up on below via
        # fut.result(timeout=...). That silently defeated
        # PHASE1_AGENT_TIMEOUT_SECONDS entirely (confirmed in production logs:
        # 35s to 3+ minutes between a logged "timed out" message and the
        # pipeline actually continuing). shutdown(wait=False) releases the
        # pool without waiting on stragglers — they keep running in the
        # background (Python can't cancel a running thread), but nothing
        # reads their result again.
        # Session 51 (51B): premarket gets a longer Phase 1 budget than live —
        # see core/config.py for the measured numbers behind 240s.
        phase1_timeout = PREMARKET_PHASE1_TIMEOUT_SECONDS if premarket_only else PHASE1_AGENT_TIMEOUT_SECONDS
        p1_pool = ThreadPoolExecutor(max_workers=5)
        p1_futures = {name: p1_pool.submit(agent.run, ticker) for name, agent in _phase1.items()}
        for name, fut in p1_futures.items():
            try:
                results[name] = fut.result(timeout=phase1_timeout)
            except TimeoutError:
                logger.error("[%s] Phase 1 agent '%s' timed out — using empty output", ticker, name)
                results[name] = AgentOutput(agent_name=name, ticker=ticker, signals=[], summary="Timed out.")
            except Exception:
                logger.exception("[%s] Phase 1 agent '%s' failed — using empty output", ticker, name)
                results[name] = AgentOutput(agent_name=name, ticker=ticker, signals=[], summary="Failed.")
        p1_pool.shutdown(wait=False)

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
            if reversal and not premarket_only:
                logger.info("[%s][%s] Signal reversal detected — exiting %s position",
                            ticker, self.mode.value, open_side)
                # Session 39 fix: this check-then-act (has_pending_exit() then
                # _flat_position()) races check_sl_tp_exits() on the maintenance
                # thread — both can pass has_pending_exit() for the same trade
                # before either writes the exit, submitting two exit orders for
                # one position. _maintenance_lock is per-Orchestrator-instance
                # (already used by check_sl_tp_exits()/_reconcile_closed_trades()
                # at that same granularity), so serializing this narrow section
                # against it closes the race without holding the lock for the
                # rest of run_ticker().
                with self._maintenance_lock:
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

        # premarket_only runs never reach execution, so critiquing a plan that
        # can't be traded just burns an Ollama round-trip for a verdict nobody
        # reads. Trade plan itself is still produced and returned —
        # run_premarket.py writes it to SQLite.
        if premarket_only:
            logger.info("[%s] premarket_only — skipping critic.", ticker)
            return results

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
        # Session 52 (Bug 6, Part C): must run before the market-closed and
        # circuit-breaker returns below — a broker order can resolve (fill,
        # cancel, expire) after hours or while the breaker is tripped, and
        # still needs to be finalized in the DB regardless of whether the
        # rest of this cycle runs.
        try:
            sync_broker_orders()
        except Exception:
            logger.exception("Order sync failed during maintenance — continuing.")

        if not _is_market_open():
            return

        # Circuit breaker repair: this used to return here when tripped,
        # skipping reconciliation AND check_sl_tp_exits for as long as the
        # trip lasted — every open position's stop-loss went completely
        # unmonitored during exactly the scenario (losses) the breaker exists
        # to react to. The breaker only blocks NEW entries (validate_plan());
        # it must never stop watching positions that are already open.
        self._refresh_portfolio_and_reconcile()

    def run_all(self, premarket_only: bool = False) -> dict[str, dict[str, AgentOutput]]:
        # Session 52 (Bug 6, Part C): same reasoning as run_maintenance() —
        # orders can resolve at any time, finalize them before anything else.
        try:
            sync_broker_orders()
        except Exception:
            logger.exception("Order sync failed at start of run_all — continuing.")

        if not premarket_only and not _is_market_open():
            logger.info("Market closed — skipping cycle.")
            return {}

        # Circuit breaker repair: no early return on a trip anymore — this
        # used to return {} here, before _refresh_portfolio_and_reconcile()
        # ever ran, meaning a tripped breaker also silently stopped
        # reconciliation and SL/TP monitoring for this lane's entire cycle.
        # Reconcile/sync always run first; premarket_only runs ignore the
        # breaker entirely (premarket never reaches execution, so there are
        # no new entries for it to block).
        closed_outcomes = self._refresh_portfolio_and_reconcile()

        triggered, reason = (False, "") if premarket_only else check_circuit_breakers()

        # When tripped, running the full pipeline on a ticker with no open
        # position can only ever produce a blocked entry (validate_plan()
        # rejects it) — pure wasted Ollama/yfinance time. Tickers that DO
        # hold an open filled entry in this lane still need the full
        # pipeline: holds, signal-reversal exits, and SL/TP-driven exits all
        # depend on it, and none of those go through validate_plan().
        tickers_to_run = WATCHLIST
        if triggered:
            tickers_to_run = [
                t for t in WATCHLIST
                if get_open_filled_entry_trade(t, self.mode.value) is not None
            ]
            logger.warning(
                "CIRCUIT BREAKER TRIPPED (%s) — new entries blocked; running pipeline for %d/%d "
                "tickers with an open %s position.",
                reason, len(tickers_to_run), len(WATCHLIST), self.mode.value,
            )

        # Session 36, Problem 2: pre-fetch all candles sequentially (one lock
        # holder at a time) before the parallel phase launches. Without this,
        # up to 50 Phase 1 agent threads (10 tickers x 5 agents) fight over
        # _yf_lock simultaneously at timeframe collision points (e.g. 1:30pm
        # when M5+M15+H1+H4 all expire together), and the last thread in the
        # queue can burn most of its 30s Phase 1 budget just waiting for the
        # lock. Warming the cache here means every Phase 1 agent call below
        # hits a cache hit instead of a live yfinance call.
        timeframes = INTRADAY_TIMEFRAMES if self.mode == TradingMode.INTRADAY else LONGTERM_TIMEFRAMES
        warm_up(tickers_to_run, timeframes)

        # Not a `with` block, and not as_completed() — same shutdown(wait=True)
        # problem as Phase 1's pool above, plus a second bug: as_completed()
        # only ever yields a future AFTER it has already finished, so
        # future.result(timeout=TICKER_TIMEOUT_SECONDS) was called on an
        # already-resolved future and could never actually raise
        # TimeoutError. TICKER_TIMEOUT_SECONDS and the "skipping ticker" log
        # below were both unreachable dead code — run_all() always waited for
        # whichever ticker was slowest that cycle, no matter how long. This
        # polls with a real per-ticker deadline via wait(timeout=1, ...)
        # instead, so an expired future is abandoned (not waited on) rather
        # than silently continuing to block on it.
        all_results: dict[str, dict[str, AgentOutput]] = {}
        # Lowered from 10: each ticker's Phase 1 news/technical agents compete
        # for the single Ollama generation slot (llm/client.py's
        # _ollama_semaphore). 10 concurrent tickers meant up to ~20
        # simultaneous LLM callers all blocking on that one slot; 5 halves
        # per-wave demand on that bottleneck. (Session 51: the queue-depth
        # limiter that used to sit in front of the semaphore was removed
        # entirely — see llm/client.py — so this max_workers value is now the
        # only lever controlling how many threads pile up waiting on it.)
        max_workers = 5
        executor = ThreadPoolExecutor(max_workers=max_workers)

        # Session addendum: with 20 tickers and 10 workers, the second wave
        # of futures sits queued behind the first. Stamping the deadline at
        # submission time (previously `monotonic() + TICKER_TIMEOUT_SECONDS`
        # for every future in one loop, right after executor.submit()) meant
        # queued tickers inherited a countdown that started before their
        # thread ever ran — a ticker could lose most of its budget just
        # waiting for a worker to free up. start_times is only written once
        # per ticker, from inside that ticker's own worker thread, so the
        # deadline now reflects when the ticker's run_ticker actually began.
        start_times: dict[str, float] = {}

        def _timed_run_ticker(ticker: str, closed_outcome, premarket_only: bool):
            start_times[ticker] = monotonic()
            return self.run_ticker(ticker, closed_outcome, premarket_only)

        futures = {
            executor.submit(
                _timed_run_ticker, ticker, closed_outcomes.get((ticker, self.mode.value)), premarket_only
            ): ticker
            for ticker in tickers_to_run
        }

        # Edge case (per-start fairness alone can hang run_all() forever):
        # ThreadPoolExecutor cannot kill a running thread, so a future we
        # abandon on timeout keeps occupying its worker for as long as it
        # keeps running. If enough first-wave tickers genuinely hang, every
        # worker can stay permanently busy for the rest of this call, and a
        # later-queued ticker never gets dispatched at all — no start_times
        # entry, so it would never expire under the per-start deadline alone
        # and the polling loop below would never terminate. batch_deadline is
        # a backstop only for futures that never started: sized to the
        # worst-case legitimate queue depth (ceil(tickers / workers) waves)
        # so it doesn't cut off a ticker that's still fairly waiting its
        # turn, but still guarantees this method returns even if some
        # workers are wedged.
        # Session 51 (51B): raised in lockstep with phase1_timeout above so
        # the outer bound doesn't abandon a premarket ticker before its own
        # (longer) Phase 1 timeout ever gets a chance to fire.
        ticker_timeout = PREMARKET_TICKER_TIMEOUT_SECONDS if premarket_only else TICKER_TIMEOUT_SECONDS
        num_waves = -(-len(futures) // max_workers)  # ceil division
        batch_deadline = monotonic() + num_waves * ticker_timeout

        pending = set(futures)
        while pending:
            done, pending = futures_wait(pending, timeout=1, return_when=FIRST_COMPLETED)
            for fut in done:
                ticker = futures[fut]
                try:
                    all_results[ticker] = fut.result()
                except Exception:
                    logger.exception("[%s] Unhandled error in run_ticker — skipping ticker.", ticker)
            now = monotonic()
            expired = set()
            for fut in pending:
                ticker = futures[fut]
                start = start_times.get(ticker)
                if start is not None:
                    if now >= start + ticker_timeout:
                        logger.error("[%s] run_ticker timed out after %ds — skipping ticker.", ticker, ticker_timeout)
                        expired.add(fut)
                elif now >= batch_deadline:
                    logger.error(
                        "[%s] never got a worker within the %ds batch deadline — skipping ticker.",
                        ticker, num_waves * ticker_timeout,
                    )
                    expired.add(fut)
            for fut in expired:
                pending.discard(fut)
        executor.shutdown(wait=False)
        return all_results
