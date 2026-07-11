# Trading Agent - Architecture Progress

## Project Goal

Build a production-grade AI trading agent that delivers intraday and long term buy/sell/hold signals for 20 stocks simultaneously. Quality target: top 1% institutional level, comparable to systems used at firms like JPMC. The system must be fully explainable, observable, and controllable by a solo trader via a personal dashboard.

---

## Current Status

**Active implementation phase.** Core scaffold complete. Signal engine complete. Fully autonomous pipeline. Watchlist finalized at 20 tickers. Backtesting replay realistic with no forced exits. Critic uses structured JSON parser. Circuit breakers fetch live Alpaca portfolio value. Market hours gate active (Alpaca clock + ET timezone fallback). All 5 news layers wired and audited correct (daily, weekly, monthly, quarterly, spike). Polygon.io and Benzinga feeds built with yfinance/Finnhub fallback. Regime label integrated end-to-end into Decision, TradePlan, and Orchestrator. VaR computed from actual open positions using 60-day realized vol, wired into `validate_plan` with incremental candidate-trade VaR. Kelly sizing wired from closed-trade history. Risk Manager `run()` surfaces full portfolio-level status for dashboard. Sector concentration veto removed. Resize-instead-of-reject on position size cap. Decision weight feedback loop fully implemented: `agent_weights` DB table, `signal_snapshot` attribution at trade insert, `scripts/update_weights.py` weekly batch, DB-backed `_load_weights()` with 5-min TTL cache and hardcoded fallback. Memory Agent write-back wired in Orchestrator (triggered at trade close). Fundamental Agent now scores P/B, debt-to-equity, and earnings growth in addition to P/E and revenue growth.

**Session 27 audit (Trade Plan Agent) — all 4 gaps implemented.** Exit infrastructure: `has_pending_exit()` added to `decision_store.py`; `check_sl_tp_exits()` added to `Orchestrator` (market hours guard, price-breach detection via candle cache, delegates exit submission to `ExecutionAgent`); called every intraday cycle via `run_all()` and every 300s for long-term via separate timer in `run_paper.py`. Signal-reversal exit: reversal block in `run_ticker()` before Phase 4 — detects opposing open position via `get_open_filled_entry_trade()`, calls `_flat_position()` → `ExecutionAgent`, skips new entry same cycle. ATR mode differentiation: four new constants in `core/config.py` (intraday SL×1.5/TP×2.5, long-term SL×2.5/TP×5.0); `trade_plan.py` picks by mode. Two post-implementation corrections applied: `_flat_position()` rewritten to delegate entirely to `self.execution.run()` (original erroneously bypassed `route_order()` and duplicated DB management); `TAKE_PROFIT_ATR_MULT_INTRADAY` corrected from 3.0 to design-locked 2.5. Trailing stop design not yet locked — deferred.

**Session 28 audit (Critic Agent) — 9 bugs fixed, 1 cleanup applied, 3 design gaps fully implemented.** Full code-level audit of `agents/critic.py`, `agents/orchestrator.py`, and `llm/prompts/spike_analysis.py` against actual files. Type mismatch on `plan` object fixed. Exception handling added to Critic LLM call and Orchestrator ticker loop. Sequential 20-ticker loop replaced with `ThreadPoolExecutor(max_workers=10)`. ATR now passed to Critic via `plan.metadata`. `SignalSource.CRITIC` added to enum. Fail-closed gate on empty Critic signals. Confidence defaults corrected in both JSON and keyword fallback paths. "valid" keyword removed from fallback scan. R/R formula fallback fixed. NEUTRAL signal gated before Phase 4. Critic timeframe label made mode-aware. Dead code (`SignalType.NEUTRAL` in HOLD check) removed. All 3 design gaps implemented: `critic_log` table stores every Critic verdict with `biggest_risk`, `failure_scenario`, and `trade_id` FK; structured per-timeframe signal block replaces flat 500-char summary in Critic prompt; HOLD decisions logged to new `hold_decisions` table with unrealized PnL. 2 design gaps (add-to-position on STRONG_BUY, trim-on-SELL) remain deferred pending reconciliation rework.

**Session 30 audit (Orchestrator) complete.** 6 bugs fixed: live snapshot price replacing stale candle price in `check_sl_tp_exits()`; falsy check on `stop_loss` replaced with explicit `None` guard; NYSE holiday calendar added to `_is_market_open()` fallback via `_nyse_holidays()` helper (pandas USFederalHolidayCalendar plus Good Friday); `future.result(timeout=45)` with dedicated `TimeoutError` isolation added to outer ticker pool; Phase 1 agents parallelised inside `run_ticker()` with inner `ThreadPoolExecutor(max_workers=5)` and 30s per-agent timeout with empty AgentOutput fallback on failure; `close_trade()` wrapped in per-trade try/except inside `_reconcile_closed_trades()` so one DB write failure no longer aborts reconciliation of remaining trades.

**Session 31: Short selling enabled. Design locked, pending implementation.** Three previously latent bugs promoted to active and design locked for fix: `_estimate_pnl()` sign flip for short positions; `check_sl_tp_exits()` breach direction for short positions; `signal_is_hold` fall-through to Phase 4 when an open position exists and signal is not in the matching direction. Short selling ETB pre-flight check design locked for execution path. `_flat_position()` closing-side fix, TradePlan SL/TP direction for short entries, and `decision_store.py` side-agnostic open-trade query all design locked. Thread safety of shared agent instances flagged as open verification item pending read of all 10 agent files. Add/trim permanently deferred. Open Decisions updated: long-only restriction lifted for paper trading phase.

**Session 32: Pre-flight health check script implemented and hardened.** `scripts/health_check.py` created. Three-tier check: Tier 1 verifies packages, Alpaca credentials, DB tables, and agent class imports; Tier 2 verifies Alpaca `get_account()` + `get_clock()`, price feed, LLM backend (PASS either way — Claude API or Ollama), Finnhub key (WARN if missing), Polygon key (WARN if missing), Benzinga key (WARN if missing), and paper trading flag (WARN if `PAPER_TRADING=False`); Tier 3 instantiates all 10 agents in both modes. Zero side effects. Collect-all-then-report. Exits code 1 on any FAIL, code 0 on warnings-only or all pass. `AlpacaBroker.__init__()` hardened: bare `except Exception` added alongside existing `except ImportError` so any auth or connection error during broker construction falls to stub mode instead of crashing `ExecutionAgent` instantiation in Tier 3. `.env` file created from `.env.example` at project root so `load_dotenv()` picks up all keys.

**Session 34: News architecture overhaul + cycle optimization — implemented.** Pre-implementation gate (`scripts/test_finnhub_ws.py`) failed: Finnhub's `subscribe-news` returned "You don't have access to this resource" — WebSocket news streaming unavailable on the current plan. Per user decision, implemented the design's own documented fallback (REST + `threading.Semaphore(2)` + 5-minute TTL cache in `news_feed.py`) in place of the WebSocket layer, and everything else as locked: M1 timeframe removed (5-minute is new floor); orchestrator split into `run_maintenance()` (60s: SL/TP + reconcile only) and `run_all()` (5-minute full pipeline); `news_events` SQLite table + `news/news_ingest.py` pre-market batch job (FinBERT + Ollama horizon classification) persist news across sessions; `OLLAMA_MODEL` confirmed already correct from Session 33. Live verification surfaced one new bug outside original scope — Ollama itself getting hammered by concurrent LLM calls once the Finnhub cascade was fixed — mitigated with the same semaphore pattern (`llm/client.py`), though a genuine CPU-throughput ceiling remains and is flagged for a future session.

**Session 33: First live `run_paper.py` run — 4 real infra bugs found and fixed.** yfinance intermittently threw `HTTP Error 401: Invalid Crumb` because `data/feeds/fundamentals_feed.py` called `yfinance` directly, unlocked, from the same `ThreadPoolExecutor` pool as candle fetches — the resulting concurrent burst raced Yahoo's session crumb until it was invalidated, and yfinance has no built-in recovery once that happens (it keeps reusing the same bad crumb forever within the process). Fixed with a shared `_yf_lock` serializing all yfinance calls in `price_feed.py`, a `_reset_yf_crumb()` helper that clears the cached crumb/cookie and retries once on empty results, and routing `fundamentals_feed.py` through the same lock. Benzinga news requests were throwing `JSONDecodeError` because Benzinga defaults to an XML response body and the code called `resp.json()` unconditionally — fixed by sending `Accept: application/json`, which Benzinga honors via content negotiation (not a query param). Ollama (`llama3`) was not installed, causing every local-LLM call to fail with `ConnectionRefusedError` on `localhost:11434` (caught gracefully already, but silently degraded to `"[LLM unavailable]"`) — installed Ollama, pulled `llama3` (resumed cleanly after a stalled/retried download), verified end-to-end via `query_llm()`. Pandas `FutureWarning` spam (`float(single_element_series)` deprecated) from ~20 call sites across `signals/patterns/candlestick.py`, `signals/timeframe_aggregator.py`, and `agents/market_regime.py` is cosmetic only (still evaluates correctly; breaks in a future pandas major version) — suppressed via a scoped `warnings.filterwarnings()` in `run_paper.py` rather than touching the call sites, since the user wants it ignored, not fixed.

---

## Implementation Log

### Session 34: News Architecture Overhaul + Cycle Optimization — Implemented (WebSocket layer blocked, REST+cache substituted)
**Status:** Implemented and verified live. `scripts/test_finnhub_ws.py` run as the pre-implementation gate — **failed**: Finnhub returned `"You don't have access to this resource"` for `subscribe-news`, meaning WebSocket news streaming is not available on the current Finnhub plan. Per explicit user decision, proceeded with the design's own documented fallback (REST + semaphore + TTL cache) in place of Decision 3's Layer 2 WebSocket, implementing everything else in the design as locked. One new bug (Ollama concurrency) was found during live verification and fixed as an addendum — see below.
**Files modified:** `core/config.py`, `core/constants.py`, `data/storage/db.py`, `data/storage/news_store.py`, `data/feeds/news_feed.py`, `agents/orchestrator.py`, `scripts/run_paper.py`, `news/layer2_premarket.py`, `news/layer3_spike.py`, `llm/client.py`
**Files created:** `news/news_ingest.py`
**Files verified, no change needed:** `core/config.py`'s `OLLAMA_MODEL` (Bug A) was already `"llama3.2:3b"` from Session 33; `transformers` (Bug C) is already at 5.13.0, well past the 4.47.0 floor, and FinBERT was already confirmed returning real non-zero sentiment scores in Session 33 — the PR-branch weight loading (`refs/pr/29`) is fragile but currently functional, no action taken beyond noting it still loads that way.

#### Pre-implementation gate result: WebSocket news streaming unavailable on current Finnhub plan
`scripts/test_finnhub_ws.py` connected successfully (`wss://ws.finnhub.io`) but the server rejected every `subscribe-news` subscription with an error frame: `"You don't have access to this resource"`. The script's own exit contract (`RESULT: FAIL — ... the REST + TTL cache interim fix should be used instead`) fired as designed. User was asked how to proceed and chose to implement the documented interim fix as the substitute for Layer 2 rather than pause or skip the news architecture work.

#### What was implemented instead of Layer 2 WebSocket
- **Bug B fix (the actual root cause of the news-timeout cascade):** `data/feeds/news_feed.py` — added a module-level `threading.Semaphore(2)` around the Finnhub REST call and a 5-minute TTL cache at the `_fetch_ticker_news()` dispatcher level (keyed by `(ticker, hours_back)`, since exact `from_dt`/`to_dt` shift by seconds on every call and would never hit a naive cache). This caps concurrent Finnhub calls regardless of how many ticker threads are running and prevents the same ticker/window from being re-fetched more than once per 5 minutes — directly matches the design's own documented interim fix text.
- **Decision 4 (news persistence) + partial Decision 3 (Layer 1):** New `news/news_ingest.py` — `sentiment_label(score)` (bullish/bearish/neutral, same 0.15/-0.15 thresholds the Decision Agent uses), `ingest_news_item()` (FinBERT score + optional Ollama horizon classification, persists to `news_events` via `save_news_event()`), and `run_premarket_news_fetch(tickers)` — sequential REST fetch across the watchlist, FinBERT sentiment + Ollama horizon classification per article, all persisted to `news_events`. Wired into `run_paper.py` as `_maybe_run_premarket_news_fetch()`, gated on ET time < 9:20am (matches the design's skip-if-late logic), called once at startup before the main loop.
- `data/storage/db.py` — `news_events` table added exactly as specified: `(ticker, headline, source, published_at, sentiment_score, sentiment_label, horizon, raw_summary, stored_at)`, plus `UNIQUE(ticker, headline, published_at)` so re-fetching the same article across cycles is a silent no-op instead of a duplicate row. Index on `(ticker, published_at)`.
- `data/storage/news_store.py` — `save_news_event()` and `get_recent_news_events(ticker, hours_back)` added alongside the existing `save_news()`/`get_recent_news()` pair (which serve the separate `news` table used by the 5-layer architecture — left untouched).
- `news/layer2_premarket.py` and `news/layer3_spike.py` — both already compute a FinBERT score for every fetched article; added one line each (`save_news_event(item, sentiment_score=score, sentiment_label=sentiment_label(score))`) to persist to `news_events` at zero extra FinBERT cost, keeping the table populated by live cycles as well as the pre-market batch.

**What was NOT implemented and why:** the literal "Layer 3 reads from SQLite, not HTTP, zero REST calls during market hours" requirement was not built as a hard read-from-SQLite-only path in `agents/news_sentiment.py`. That requirement's entire rationale was the WebSocket feed keeping `news_events` continuously fresh — without it, forcing Layer 3 to read only from SQLite would mean reading stale-or-empty data between pre-market runs, not an improvement. The in-memory TTL cache + semaphore in `news_feed.py` already gives every caller (including `news_sentiment.py`, transitively) the same practical outcome the requirement was chasing — at most one REST hit per ticker per 5 minutes — without the risk of rewriting the hot path to convert SQLite rows back into `NewsItem` objects under time pressure. If a Finnhub plan upgrade unlocks `subscribe-news` in the future, Layer 2 (WebSocket) and the hard SQLite-only Layer 3 read path from the original design can be implemented then.

#### Everything else in the design, implemented as locked
- **Decision 1 (remove M1):** `core/constants.py` — `INTRADAY_TIMEFRAMES` changed from `[M1, M5, M15, H1]` to `[M5, M15, H1]`.
- **Decision 2 (cycle frequency split):** `core/config.py` — new `INTRADAY_CYCLE_SECS=60` and `PIPELINE_CYCLE_SECS=300` constants. `agents/orchestrator.py` — `run_all()` split into a new lightweight `run_maintenance()` (SL/TP breach checks + closed-trade reconciliation only, extracted via a shared `_refresh_portfolio_and_reconcile()` helper so both methods stay in sync) and `run_all()` (unchanged full 11-agent pipeline). `scripts/run_paper.py` — main loop now calls `intraday_orch.run_maintenance()` every `INTRADAY_CYCLE_SECS` (60s) and `intraday_orch.run_all()` every `PIPELINE_CYCLE_SECS` (300s) via a `last_pipeline_run` timer, mirroring the existing long-term SL/TP-vs-full-pipeline pattern that was already in the file.
- **Bug E (Benzinga key):** confirmed `BENZINGA_API_KEY` is set and working (fixed in Session 33's `Accept: application/json` header fix) — the `"Benzinga returned no items"` warnings seen in live logs are genuine empty results for those tickers/windows, not a broken key, consistent with what Session 33 already verified.
- **Bug D (Alpaca broker quadruple-init):** left deferred, as explicitly stated in the design — not worth fixing until a broker singleton is needed for other reasons.

#### New bug found during live verification (not in original Session 34 scope): Ollama concurrency
Live reruns after the above fixes still showed Phase-1 `news`/`regime` timeouts across nearly every ticker — but now caused by Ollama itself, not Finnhub. `news/layer2_premarket.py`'s `run_premarket_analysis()` calls `query_llm()` unconditionally for every ticker, every cycle (all modes, not just intraday). With up to 10 tickers hitting this concurrently and Ollama serializing generation on a single local model instance, requests queue behind each other and tail requests blow through both Ollama's 60s read timeout and the orchestrator's 30s Phase-1 agent timeout — the exact same "unbounded concurrency into a shared serialized resource" pattern as Bug B, just for Ollama instead of Finnhub.
- **Fix:** `llm/client.py` — added `_ollama_semaphore = threading.Semaphore(2)` around the `requests.post()` call in `_query_ollama()`, same pattern as the Finnhub fix.
- **Not fully resolved:** the semaphore makes queuing orderly but doesn't reduce total Ollama compute — a local 3B model on CPU serving 10 tickers' worth of LLM calls within a 30s Phase-1 window is a genuine throughput ceiling, not a code bug. Timeouts were still observed in the final verification run. This was previously masked by the Finnhub-hang cascade (Bug B) forcing early timeouts before Ollama's load ever became the bottleneck; fixing Bug B exposed it. Flagged as an open item for a future session — options include raising the Phase-1 timeout, reducing how many tickers call the LLM per cycle, or moving to a faster backend (Claude API via `ANTHROPIC_API_KEY`, or GPU-accelerated Ollama).

#### Verification
Live `run_paper.py` reruns confirmed: pre-market fetch correctly skipped when started past 9:20am ET with the expected log line; full pipeline runs with `run_maintenance()`/`run_all()` split wired correctly; zero `JSONDecodeError` or crash exceptions from the news layers (Session 33's Benzinga fix + Session 34's cache both holding); `TechnicalAgent` signal counts consistent with M1 removed. Ollama throughput ceiling (documented above) remains the one unresolved item preventing a fully clean end-to-end cycle completion under current hardware.

#### Context
Third live run (`run_paper.py`) executed during market hours. Log analysis surfaced 6 issues. Parallel to bug triage, a full architectural review of the news pipeline was completed. Four decisions locked: remove 1-minute timeframe, split orchestrator cycle frequency, overhaul news architecture to event-driven WebSocket, and persist news across sessions via SQLite.

#### Bugs Found From Third Live Run Log

**Bug A (Critical): `OLLAMA_MODEL` still set to `llama3` — causes 404 and ReadTimeout on every LLM call**
Every Critic and news reasoning call timed out at exactly 60 seconds across all tickers in the third run. Root cause: `core/config.py` default for `OLLAMA_MODEL` was never updated from `"llama3"` to `"llama3.2:3b"` after the model swap. Ollama responds with a 404 for an unrecognized model name, then the connection hangs until the `requests` timeout fires.
- Fix: update `OLLAMA_MODEL` default in `core/config.py` (and/or `.env`) from `"llama3"` to `"llama3.2:3b"`.

**Bug B (Critical): News agent timed out for all 10 tickers in first batch**
Every ticker in the first parallel batch (PLTR, UNH, ISRG, MSFT, JPM, LLY, GS, NVDA, V, AAPL) hit the Phase 1 `news` timeout and used empty output. Chain: Benzinga returned nothing (no key configured or key not activated) → fallback to Finnhub → all 5 Phase 1 threads hit Finnhub concurrently → free-tier rate limit causes connection hang instead of clean 429 → Phase 1 30-second timeout fires → empty output for all tickers. Zero news sentiment fed into any decision this cycle. Root fix is the WebSocket architecture locked in this session (see Design Decisions below). Interim fix (if running before WebSocket implementation): add module-level `threading.Semaphore(2)` and 5-minute TTL cache in `news_feed.py` as described in Session 34 design.

**Bug C (Moderate): FinBERT loading weights from a non-merged PR branch (`refs/pr/29`), not `main`**
`model.safetensors` HEAD on `ProsusAI/finbert` main returns 404. The HuggingFace library crawls commits and discussions to find weights on an open PR (`refs/pr/29`). FinBERT does load and run, but this is fragile — if the PR closes or the Hub resolves differently the weights silently disappear. Tied to the known `transformers` version incompatibility (`ValueError: ModelOutput is already registered as pytree node`). Fix: upgrade `transformers>=4.47.0` + `pip install --force-reinstall torch`. Verify FinBERT is actually returning non-zero scores (not just loading) before marking resolved.

**Bug D (Low): Alpaca broker initialized 4 times per startup**
`Alpaca broker initialized (paper=True)` fires at 11:47:36, 11:47:38, 11:47:39, and 11:47:40. Four separate agent constructors each create their own `AlpacaBroker` instance. Not a functional bug but wasteful overhead. Deferred — not worth touching until a broker singleton pattern is needed for other reasons.

**Bug E (Low): Benzinga returning nothing for most tickers**
`Benzinga returned no items for <ticker> — falling back to Finnhub` fires for NVDA, LLY, PLTR, JPM, V, ISRG, UNH, GS across both passes. Either `BENZINGA_API_KEY` is not set or the key is not activated for the `company-news` endpoint. Non-blocking since Finnhub fallback exists, but contributes to Bug B by pushing all load onto Finnhub. Emptying `BENZINGA_API_KEY` in `.env` would skip the Benzinga call entirely and avoid the wasted HTTP round-trip.

#### Design Decisions Locked

**Decision 1: Remove 1-minute timeframe from TechnicalAgent**
1-minute candles at a 60-second cycle produce identical signals on consecutive runs and amplify noise. 5-minute is the new floor. Removing M1 also reduces multicollinearity inflation in the Decision Agent signal count.
- Fix: remove `Timeframe.M1` from TechnicalAgent's timeframe list (or equivalent config constant).

**Decision 2: Split orchestrator cycle frequency**
With the 1-minute timeframe removed, running the full 11-agent pipeline every 60 seconds wastes Ollama calls, yfinance calls, and FinBERT passes on data that has not changed.

```
Every 60 seconds:   check_sl_tp_exits() + _reconcile_closed_trades() only
Every 5 minutes:    full pipeline — all Phase 1-4 agents, LLM, execution
```

This reduces Ollama load by 5x during market hours with zero loss of signal quality. SL/TP and reconciliation remain at 60-second frequency since they depend on live price, not candle data.
- Fix: in `run_paper.py`, split the two loops. The 60-second loop calls only the lightweight methods. A separate 5-minute loop (or a counter inside the 60-second loop) fires `run_all()`.

**Decision 3: Three-layer event-driven news architecture**

Layer 1 — Pre-market sequential REST fetch (runs once at startup, before 9:30am ET):
- Iterates all 20 tickers sequentially, one REST call per ticker to Finnhub
- FinBERT scores each article for sentiment
- Ollama classifies each article as `intraday`, `long_term`, or `both`
- All results written to new `news_events` SQLite table with `(ticker, headline, source, published_at, sentiment_score, sentiment_label, horizon, raw_summary)` columns
- Completes in 3-6 minutes — well within the pre-market window even for a 9:05am start
- If started after 9:20am, pre-market fetch is skipped and WebSocket connects immediately

Layer 2 — WebSocket event-driven during market hours (9:30am to 4:00pm ET):
- Single persistent connection to `wss://ws.finnhub.io?token=<FINNHUB_API_KEY>`
- Subscribes to all 20 tickers via `{"type":"subscribe-news","symbol":"<TICKER>"}` on connect
- Runs in a background `threading.Thread` (daemon=True)
- On news message arrival: writes article to `news_events` SQLite table, sets flag in a shared `threading.Event` or dict keyed by ticker
- Auto-reconnect with exponential backoff (1s, 2s, 4s, max 30s) if connection drops
- Fallback: if WebSocket has been disconnected for >60 seconds, resume Finnhub REST polling with TTL cache until reconnected
- Zero REST news calls during market hours when WebSocket is live

Layer 3 — NewsSentimentAgent reads from SQLite, not HTTP:
- On every 5-minute cycle, `NewsSentimentAgent.run(ticker)` reads from `news_events` table filtered by ticker and published_at window (last 1hr for intraday, last 24hr for long-term)
- If ticker has a fresh-news flag set by the WebSocket thread, also triggers re-run of FinBERT + Ollama classification on the new articles before reading
- No `fetch_intraday_news()` or `fetch_news()` REST calls made during market hours

Cost: $0. Finnhub WebSocket is free for up to 50 symbols. Watchlist is 20 tickers. Free tier confirmed sufficient. Non-commercial use only on free tier — paper trading qualifies.

**Pre-implementation gate:** Run `scripts/test_finnhub_ws.py` (created this session) to confirm `subscribe-news` is active on the specific API key before proceeding. WebSocket trade/quote streaming is confirmed free; news streaming must be verified per-key.

**Decision 4: Persist news across sessions via `news_events` SQLite table**
Currently all news is ephemeral — restarting the process loses all articles and context. With `news_events` in SQLite, the pre-market fetch on day 2 pre-populates context from yesterday's events (filtered by `published_at > 24 hours ago`). MemoryAgent JSON write-back already persists directional bias per ticker. Together these give the system meaningful cross-session context rather than a completely cold start each morning.
- Fix: add `news_events` table to `data/storage/db.py` schema. Add `save_news_event()` and `get_recent_news_events(ticker, hours_back)` to a new `data/storage/news_store.py`.

#### Timing Analysis (Pre-market fetch)
| Phase | Time |
|---|---|
| FinBERT pre-warm | ~40 seconds (one-time) |
| Sequential REST fetch + FinBERT + Ollama per ticker | ~10-17 seconds |
| 20 tickers total | 3-6 minutes |
| Buffer before 9:30am market open (9:05am start) | ~15 minutes |

15-minute buffer before first cycle is sufficient. Earliest safe start is 9:05am.

#### Files Required for Implementation
- `run_paper.py` — pre-market hook, WebSocket startup, cycle frequency split
- `data/feeds/news_feed.py` — WebSocket class, SQLite write path, REST polling fallback
- `data/storage/db.py` — `news_events` table schema
- `agents/news_sentiment.py` — read from SQLite instead of HTTP
- `core/config.py` — `OLLAMA_MODEL` fix + new cycle interval constants (`INTRADAY_CYCLE_SECS=60`, `PIPELINE_CYCLE_SECS=300`)
- `agents/technical.py` (or timeframe config) — remove `Timeframe.M1`

---

### Session 33: First Live Paper Trading Run — yfinance Crumb Bug, Benzinga XML/JSON Bug, Ollama Setup, Warning Cleanup
**Status:** Complete. 3 real bugs found and fixed via live `run_paper.py` execution (not static audit). 1 cosmetic item suppressed per user request, not fixed. Ollama installed and verified end-to-end.
**Files modified:** `data/feeds/price_feed.py`, `data/feeds/fundamentals_feed.py`, `data/feeds/news_feed.py`, `scripts/run_paper.py`
**Environment changes:** Ollama installed (`winget`/manual), `llama3` model pulled (4.7 GB), `yfinance` upgraded via `pip install -U yfinance` (did not alone fix the crumb bug — see below).

This was the first time `run_paper.py` was actually executed end-to-end rather than statically audited. Running it surfaced three real bugs that code review had not caught, plus one cosmetic warning the user chose to suppress rather than fix.

#### Bug 1 (High): yfinance `HTTP Error 401: Invalid Crumb` — permanent once triggered, not a race the lock alone fixed
**Symptom:** Intermittent `401 Unauthorized: Invalid Crumb` errors from yfinance during live pipeline runs, affecting candle fetches for arbitrary tickers including `^VIX`/`SPY` (market regime) and fundamentals.

**Root cause, found in two passes:**
1. First pass: `agents/orchestrator.py` runs up to 10 tickers concurrently (`ThreadPoolExecutor(max_workers=10)`), each spawning 5 more Phase-1 agent threads (`max_workers=5`) — up to 50 concurrent yfinance calls at startup. Added a module-level `threading.Lock` (`_yf_lock`) in `price_feed.py` to serialize `_yf_fetch_candles()`/`_yf_fetch_snapshot()`. This did **not** fully fix it — 401s persisted.
2. Second pass: `data/feeds/fundamentals_feed.py` called `yfinance` directly (`yf.Ticker(ticker).info`), completely bypassing the new lock, from the same `ThreadPoolExecutor` pool as candle fetches. This was the actual source of the continued race.
3. Deeper investigation via `yfinance/data.py` source: `YfData` is a singleton with its own internal `_cookie_lock`, so cookie/crumb *acquisition* was already thread-safe. The real defect is that yfinance caches the minted crumb in-process (`self._crumb`) for the life of the run and has **no recovery path** if Yahoo later invalidates it (e.g. under concurrent load) — every subsequent request keeps reusing the same stale crumb and fails with 401 forever, even after `pip install -U yfinance` to 1.5.1.

**Fix:**
- `data/feeds/price_feed.py`: `_yf_lock` (`threading.Lock`) serializes all yfinance calls. New `_reset_yf_crumb()` helper reaches into the `yfinance.data.YfData` singleton and clears `_crumb`/`_cookie` under its lock, forcing a fresh cookie/crumb handshake. `_yf_fetch_candles()` and `_yf_fetch_snapshot()` now retry once via `_reset_yf_crumb()` if the first attempt returns empty.
- `data/feeds/fundamentals_feed.py`: `_fetch_fundamentals_live()` now imports `_yf_lock` and `_reset_yf_crumb` from `price_feed.py` and wraps its yfinance call in the same lock, with the same empty-result retry.
- Verified with a live rerun: zero `401 Invalid Crumb` errors across a full pipeline cycle for all 20 watchlist symbols.

#### Bug 2 (Medium): Benzinga news requests failing with `JSONDecodeError`
**Symptom:** `data.feeds.news_feed: Benzinga news request failed for <ticker>` with `json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)`, falling back to Finnhub on every call.

**Root cause:** Benzinga's `/api/v2/news` endpoint defaults to an **XML** response body (`Content-Type: application/xml`) unless JSON is explicitly negotiated. `_fetch_ticker_news_benzinga()` called `resp.json()` unconditionally, which fails to parse the XML body. Confirmed by direct request: default response was `<?xml version="1.0" encoding="UTF-8"?><result></result>`; adding an `Accept: application/json` header returned a proper JSON array. (A `format=json` query param, tried first, did not change the response — Benzinga negotiates format via header, not query param.)

**Fix:** `data/feeds/news_feed.py` — `_fetch_ticker_news_benzinga()` now sends `headers={"Accept": "application/json"}` on the request. Verified with a live call returning real articles for `AAPL`; the Finnhub-fallback path for tickers with genuinely no Benzinga articles in the lookback window was confirmed to be correct existing behavior, not a bug.

#### Bug 3 (Low, environment): Ollama not installed — every local-LLM call failed
**Symptom:** `requests.exceptions.ConnectionError: ... Failed to establish a new connection: [WinError 10061] ... target machine actively refused it` on `localhost:11434` from `llm/client.py::_query_ollama()`. Already caught gracefully (`except Exception: return "[LLM unavailable]"`) so the pipeline did not crash, but every LLM-driven signal silently degraded.

**Root cause:** `ANTHROPIC_API_KEY` is unset, so `query_llm()` defaults to the local Ollama backend (`core.config.OLLAMA_BASE_URL = http://localhost:11434`), but Ollama was never installed on this machine.

**Fix:** User installed Ollama manually. `ollama pull llama3` (4.7 GB) was run via `C:\Users\<user>\AppData\Local\Programs\Ollama\ollama.exe` (not yet on PATH in the current shell session). First pull attempt stalled at 4.34/4.7 GB — the largest partial blob's `LastWriteTime` stopped advancing while Ollama kept spawning new zero-byte retry-chunk files, indicating a stuck network/retry loop rather than a disk-space issue (29 GB free confirmed). Killed and re-ran `ollama pull llama3`, which resumed from the partial download and completed successfully. Verified end-to-end with `llm.client.query_llm()` returning a real model response — not just `ollama list` showing the model.

#### Item 4 (Cosmetic, not fixed — suppressed per user instruction): pandas `FutureWarning` spam
**Symptom:** ~20 call sites logged `FutureWarning: Calling float on a single element Series is deprecated and will raise a TypeError in the future. Use float(ser.iloc[0]) instead` across `signals/patterns/candlestick.py`, `signals/timeframe_aggregator.py:77`, and `agents/market_regime.py:43` — all doing `float(row["Open"])`-style access on a single-element Series instead of `float(row["Open"].iloc[0])`.

**Explicit user decision:** Ignore rather than fix — harmless today, would only become a hard error in a future pandas major version, and rewriting ~20 call sites for a cosmetic warning was judged not worth it right now.

**Applied:** `scripts/run_paper.py` — `warnings.filterwarnings("ignore", category=FutureWarning, message="Calling float on a single element Series")` added at the top of the entry point, scoped to that exact message only (other warnings, including real errors, still surface normally). Verified: 0 `FutureWarning` lines in a full run through warm-up plus first pipeline cycle.

---

### Session 32: Pre-Flight Health Check Script Design Lock
**Status:** Complete. `scripts/health_check.py` created and hardened. Broker init catch fix applied. `.env` created.
**Files created:** `scripts/health_check.py`, `.env`
**Files modified:** `execution/broker/alpaca.py` — broker init catch broadened to `except Exception` so missing/invalid keys fall to stub mode rather than crashing Tier 3 agent instantiation.

#### Purpose
A pre-flight checklist run manually before starting paper trading each session. Confirms every dependency is alive and every agent can be constructed before a single order is ever placed. If anything critical is broken, the script exits with a non-zero code and a clear error table before the trading session begins.

#### Zero Side Effects Constraint
The script must produce absolutely no side effects. It must not:
- Write to the SQLite database
- Write to any memory JSON files
- Store signals, news items, or agent outputs
- Call agent `run()` methods (which would trigger DB writes, LLM calls, and news storage)
- Consume API quota on Finnhub, Claude, or Alpaca beyond what is needed for a single connectivity ping

The only permitted external calls are: one Alpaca `get_account()` call, one `get_candles("AAPL", Timeframe.D1)` call, and key presence checks for LLM and Finnhub. All calls are read-only with no state mutation.

#### Three-Tier Check Structure

**Tier 1: Environment (instant, no network)**
All Tier 1 failures are hard FAILs. These checks never fail unless something is misconfigured.

| Check | Pass condition |
|---|---|
| Package imports | `alpaca`, `yfinance`, `anthropic`, `transformers`, `pandas`, `numpy`, `finnhub` all import without error |
| Required env vars | `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ANTHROPIC_API_KEY`, `FINNHUB_API_KEY` all present and non-empty |
| DB tables exist | `trade_decisions`, `agent_weights`, `critic_log`, `hold_decisions`, `stock_memory_store` all exist in the SQLite DB |
| Agent class imports | All 10 agent classes importable from their modules without error |

**Tier 2: Connectivity (network, read-only)**
Alpaca and price feed failures are hard FAILs. LLM and Finnhub key presence failures are WARNs (both have fallbacks in the live system).

| Check | Result on failure |
|---|---|
| Alpaca: `get_account()` succeeds and returns portfolio value | FAIL |
| Alpaca: `get_clock()` succeeds (records whether market is open, does not block) | FAIL |
| Price feed: `get_candles("AAPL", Timeframe.D1)` returns a non-empty DataFrame | FAIL |
| `ANTHROPIC_API_KEY` present and non-empty | WARN (Critic has fail-open fallback to Ollama) |
| `FINNHUB_API_KEY` present and non-empty | WARN (news feed falls back to yfinance) |

**Tier 3: Agent instantiation (no network, no DB writes)**
Calls only `AgentClass(TradingMode.INTRADAY)` and `AgentClass(TradingMode.LONG_TERM)` for all 10 agents. Does not call `run()`. Confirms all internal imports, constants, and constructor logic resolve without error. Any instantiation failure is a hard FAIL.

Agents checked: `TechnicalAgent`, `NewsSentimentAgent`, `FundamentalAgent`, `MarketRegimeAgent`, `MemoryAgent`, `RiskManagerAgent`, `DecisionAgent`, `TradePlanAgent`, `CriticAgent`, `ExecutionAgent`.

#### Result Reporting
Collect-all-then-report. All checks across all three tiers run to completion regardless of failures encountered. Results are printed as a single table at the end:

```
[PASS] Tier 1: alpaca-py importable
[PASS] Tier 1: anthropic importable
[PASS] Tier 1: all required env vars present
[PASS] Tier 1: all 5 DB tables exist
[PASS] Tier 1: all 10 agent classes importable
[PASS] Tier 2: Alpaca connected — portfolio $100,000.00
[PASS] Tier 2: market CLOSED (pre-market)
[PASS] Tier 2: AAPL D1 price feed OK (252 bars)
[WARN] Tier 2: ANTHROPIC_API_KEY missing — Critic will fall back to Ollama
[PASS] Tier 2: FINNHUB_API_KEY present
[PASS] Tier 3: all 10 agents instantiated (INTRADAY + LONG_TERM)

Result: 1 WARNING, 0 FAILURES
System is ready. You may start run_paper.py.
```

If any FAIL exists:
```
Result: 0 WARNINGS, 2 FAILURES
Fix the above failures before starting paper trading.
```
Exits with code 1 on any FAIL, code 0 if only warnings or all pass.

#### Decisions Locked

| Decision | Choice |
|---|---|
| Integration with run_paper.py | Standalone only. run_paper.py is not modified. User runs health_check.py manually before run_paper.py. |
| LLM connectivity check | Key presence only. No real API call. No quota consumed. |
| Finnhub connectivity check | Key presence only. No real API call. |
| Failure behaviour | Collect all results across all tiers, print full table, then exit. No fail-fast. |
| LLM/Finnhub failure severity | WARN (both have fallbacks in the live system). Alpaca and price feed are FAIL. |

---

### Session 31: Short Selling Feature Design Lock
**Status:** Complete. All bugs fixed and verified. Thread safety audit completed (Stub 26).
**Files modified:** `agents/orchestrator.py`, `execution/broker/alpaca.py`, `data/storage/decision_store.py`, `agents/execution.py`, `agents/decision.py`
**Files verified (no change needed):** `agents/trade_plan.py` — SL/TP direction and order side for shorts already correct.

#### Context
Short selling enabled for paper trading phase. Alpaca supports short selling via API with $0 borrow fees on ETB (easy-to-borrow) securities. Paper trading has no borrow fees or margin interest, so no overhead cost during testing. Add-to-position and trim are permanently deferred.

Short selling scope locked: a short entry occurs only when there is no open position in the lane. A SELL order submitted to close an existing long is not a short entry; it is handled entirely by the existing reversal block and `_flat_position()` path. No change needed to that logic.

---

#### Latent Bugs Promoted to Active: Design Locked

**Bug L1 (High): `_estimate_pnl()` wrong sign for short positions**
`(exit_price - entry_price) * qty` gives a positive result when price rises, which is profit for a long but a loss for a short. All short trade PnL will be recorded with the wrong sign, corrupting the memory write-back, decision weight feedback loop, and dashboard.
- `agents/orchestrator.py`: Read `side` from the trade dict. If `side == "sell"`, negate the result: `round(-((exit_price - entry_price) * qty), 2)`.

**Bug L2 (High): `check_sl_tp_exits()` breach direction wrong for short positions**
Current logic: `current_price <= stop_loss` triggers SL, `current_price >= take_profit` triggers TP. This is correct for longs only. For a short, stop loss is above entry and take profit is below entry, so both conditions are inverted. A short that hits its take profit will never be caught and will sit open indefinitely.
- `agents/orchestrator.py`: Gate breach checks on `trade.get("side", "buy")`. For `side == "buy"`: existing logic unchanged. For `side == "sell"`: `sl_breach = current_price >= stop_loss`, `tp_breach = current_price <= take_profit`.

**Bug L3 (High): `signal_is_hold` falls through to Phase 4 when False with an open position**
`signal_is_hold` only checks `BUY` and `STRONG_BUY`. With an open short position and a new SELL signal (same direction, not a reversal), `signal_is_hold` evaluates to `False`. The `if signal_is_hold` block does not fire and control falls through to Phase 4, attempting to open a second short in the same lane.
- `agents/orchestrator.py`: Change `signal_is_hold` to evaluate direction relative to the open position side. If `open_side == "buy"`, hold fires on `BUY` or `STRONG_BUY`. If `open_side == "sell"`, hold fires on `SELL` or `STRONG_SELL`. Add explicit `logger.error` and early return as a safety net for any unmatched fall-through to Phase 4 with an open position.

---

#### New Short Selling Changes: Design Locked

**Change 1: `_flat_position()` closing side must oppose open side**
`side=OrderSide.SELL` is hardcoded. Closing a short requires a BUY order. If a short position hits SL/TP or a reversal, `_flat_position()` currently submits another SELL, deepening the short instead of closing it.
- `agents/orchestrator.py`: Read `open_side = entry_trade.get("side", "buy")`. Set `closing_side = OrderSide.BUY if open_side == "sell" else OrderSide.SELL`. Use `closing_side` in the TradePlan constructed inside `_flat_position()`.

**Change 2: TradePlanAgent SL/TP direction and order side for short entries**
SL and TP are calculated assuming a long entry. For a short, both are inverted: stop loss is above entry, take profit is below.
- `agents/trade_plan.py`: Gate direction on signal side. For `SELL`/`STRONG_SELL` signals: `stop_loss = entry_price + atr * SL_MULT`, `take_profit = entry_price - atr * TP_MULT`. Set `side = OrderSide.SELL` on the TradePlan for short entries.

**Change 3: ETB pre-flight check before short entry**
Alpaca rejects short orders on HTB (hard-to-borrow) securities without a locate. TSLA and other high-short-interest names can flip between ETB and HTB overnight.
- `execution/broker/alpaca.py`: Add `is_etb(ticker) -> bool` method using the Assets endpoint. Returns `True` only if `asset.shortable == True` and `asset.easy_to_borrow == True`.
- ETB check placement (TradePlan stage vs Execution stage) to be decided at implementation time.

**Change 4: Verify `get_open_filled_entry_trade()` is side-agnostic**
If this function filters on `side = 'buy'` only, an open short will be invisible and a duplicate short entry could be placed in the same lane.
- `data/storage/decision_store.py`: Confirm the query filters only on `ticker` and `strategy_mode`, not on `side`. No code change expected but must be verified before implementation ships.

---

#### Design Gap: Thread Safety Verification Pending

All 10 agent instances are constructed once in `Orchestrator.__init__()` and shared across all 20 ticker threads simultaneously via `run_ticker()`. This is safe only if every agent's `run()` method is fully stateless (no writes to `self.*` mutable fields during execution). Not yet verified.

**Files to read and verify:**

| File | What to check |
|---|---|
| `agents/technical.py` | Any `self.*` writes inside `run()` |
| `agents/news_sentiment.py` | FinBERT model calls and news fetching should be local-variable only |
| `agents/fundamental.py` | TTL cache writes if stored on instance |
| `agents/market_regime.py` | VIX/vol calculations should be local |
| `agents/memory_agent.py` | High risk: memory reads/writes could use instance-level state |
| `agents/risk_manager.py` | Portfolio exposure state could be instance-level |
| `agents/decision.py` | `_load_weights()` cache (`self._weights`, `self._weights_loaded_at`) is the most likely race condition |
| `agents/trade_plan.py` | ATR and sizing calculations should be local |
| `agents/critic.py` | LLM call results should be local |
| `agents/execution.py` | Broker client if stored on instance |

If any agent writes to `self.*` during `run()`, fix options are: (a) move the mutable state to local variables inside `run()`, or (b) use a `threading.Lock` around the write. Option (a) is always preferred.

---

### Session 30: Orchestrator Audit: 6 Bugs Fixed
**Status:** Complete. 6 bugs found and fixed. All verified against returned file.
**Files modified:** `agents/orchestrator.py`

#### Bug Fixes

**Bug 1 (High): `check_sl_tp_exits()` used stale candle price instead of live snapshot**
`get_candles(ticker, tf)` returns cached OHLCV data up to 15 minutes old for M15 candles. A stock can blow through a stop loss and recover in that window. Session 27 design explicitly specified `fetch_snapshot(ticker).price` for live prices in this method and the implementation had drifted.
- `agents/orchestrator.py`: Replaced `get_candles()` and `df["Close"].iloc[-1]` with `fetch_snapshot(ticker).price` inside a try/except. On fetch failure, logs a warning and `continue`s to the next trade rather than crashing the loop.
- Added `from data.feeds.price_feed import fetch_snapshot` to imports.

**Bug 2 (Medium): `check_sl_tp_exits()` silently skipped trades with `stop_loss == 0.0`**
`if not stop_loss` is a falsy check. `not 0.0` evaluates to `True` in Python, so any position where `stop_loss` was stored as `0.0` was silently skipped every cycle with no log.
- `agents/orchestrator.py`: Changed to `if stop_loss is None or take_profit is None or not qty:` using explicit `None` checks for the price fields.

**Bug 3 (Medium): `_is_market_open()` fallback had no knowledge of NYSE holidays**
If the Alpaca API call failed, the fallback checked only weekday and ET time. On NYSE holidays falling on weekdays (Christmas, Thanksgiving, MLK Day, July 4th, Good Friday) the fallback would incorrectly return `True` and fire a full cycle.
- `agents/orchestrator.py`: Added `_easter_date(year)` (O'Beirne Gregorian algorithm) and `_nyse_holidays(year)` (pandas `USFederalHolidayCalendar` plus Good Friday). Fallback now checks `if now.date() in _nyse_holidays(now.year): return False` before the time range check. Note: `USFederalHolidayCalendar` includes Veterans Day and Columbus Day which are not NYSE holidays; on those two days the fallback conservatively returns `False`. The primary Alpaca clock path is unaffected.

**Bug 4 (Medium): No timeout on `future.result()` meant a hanging thread blocked the entire cycle**
`future.result()` with no timeout meant a single hanging agent (network call that never returns, FinBERT freeze, SQLite lock) would block `run_all()` indefinitely. The 60-second intraday cycle would never complete.
- `agents/orchestrator.py`: Changed to `future.result(timeout=45)` with a dedicated `except TimeoutError` handler that logs the error and skips the ticker, separate from the general `Exception` catch.

**Bug 5 (Medium): Phase 1 agents ran sequentially inside `run_ticker()` creating a 10-15s bottleneck**
Regime, Technical, News, Memory, and Fundamental agents ran one after the other despite being fully independent at Phase 1. Conservative estimate of 2-3s per agent equals 10-15s Phase 1 wall time per ticker, consuming most of the 60s intraday cycle budget.
- `agents/orchestrator.py`: Phase 1 replaced with an inner `ThreadPoolExecutor(max_workers=5)`. All 5 agents submitted simultaneously via `p1_pool.submit(agent.run, ticker)`. Each agent has a 30s individual timeout. On timeout or exception, falls back to an empty `AgentOutput` with an appropriate summary so one agent failure no longer blocks the other 4 or kills the ticker.

**Bug 6 (Low-Medium): `close_trade()` had no exception handling inside the `_reconcile_closed_trades()` loop**
A DB write failure on trade N caused an exception to propagate out of the loop entirely. All remaining trades after N were not reconciled that cycle, their PnL was not recorded, and they remained open in the DB. The failure was silent with no log entry.
- `agents/orchestrator.py`: `close_trade()` call wrapped in try/except with `logger.exception` logging ticker, mode, and trade ID. `continue` after the exception so the loop proceeds to the next trade.

---

### Session 28 — Critic Agent Deep Audit: Bugs Fixed, Design Gaps Implemented
**Status:** Complete. 9 bugs fixed, 1 dead-code cleanup applied, 3 design gaps fully implemented. 2 design gaps deferred.
**Files modified:** `agents/critic.py`, `agents/orchestrator.py`, `agents/trade_plan.py`, `llm/prompts/spike_analysis.py`, `core/constants.py`, `data/storage/db.py`, `data/storage/decision_store.py`
**Files verified (no change needed):** `core/models.py` (TradePlan already has `metadata: dict[str, Any]` field with `default_factory=dict`)

#### Bug Fixes — All Verified Against Actual Files

**Bug 1 (Critical): Type mismatch on `plan` — one of Critic or Execution always broken**
`TradePlanAgent` was storing `plan.__dict__` (a dict) in `raw_data["plan"]`. `build_critic_prompt` accessed it with attribute notation (`plan.side.value`) which fails on a dict. `Orchestrator` then did `TradePlan(**plan)` which requires a dict. One of the two paths was always broken.
- `agents/trade_plan.py`: Changed `raw_data={"plan": plan.__dict__}` to `raw_data={"plan": plan}` — stores the TradePlan object itself
- `agents/orchestrator.py`: Removed `trade = TradePlan(**plan)` — plan is already a TradePlan object; passed directly to `validate_plan()` and `execution.run()`

**Bug 2 (Critical): No exception handling — one crash kills all 20 tickers**
`query_llm()` had no try/except in `critic.py`. `self.critic.run()` had no try/except in `orchestrator.py`. The dict comprehension in `run_all()` halted entirely on first exception.
- `agents/critic.py`: Wrapped `query_llm(prompt)` in try/except; fallback injects synthetic approved JSON (`approved=true, confidence=0.5`) so pipeline continues through Risk Manager
- `agents/orchestrator.py`: Replaced dict comprehension with explicit for loop; each ticker wrapped in try/except with `logger.exception` for full traceback; one ticker failure skips that ticker and continues the remaining 19

**Parallelism fix: Sequential 20-ticker loop replaced with ThreadPoolExecutor**
`run_all()` was fully sequential — one ticker completed entirely before the next started. At 1–2s per ticker minimum, 20 tickers = 20–40s per cycle against a 60s intraday window. On busy days with multiple LLM calls, cycles would stack.
- `agents/orchestrator.py`: Added `from concurrent.futures import ThreadPoolExecutor, as_completed`; `run_all()` now submits all 20 tickers immediately with `max_workers=10`; `future.result()` per-ticker exception handling preserves the per-ticker isolation from Bug 2 fix
- SQLite safety confirmed: `get_connection()` creates a new connection per call with WAL mode — concurrent writes from multiple threads do not collide
- `self.portfolio_value` is set once before the pool starts and only read inside threads — no race condition

**Bug 3 (High): ATR block rule in prompt but ATR never provided**
Prompt rule said "stop is wider than 3x ATR" but ATR was never passed to the Critic — LLM could not evaluate this rule.
- `agents/trade_plan.py`: Added `metadata={"atr": round(atr, 4)}` to TradePlan construction
- `llm/prompts/spike_analysis.py`: `build_critic_prompt` reads `plan.metadata.get("atr")`; injects `ATR (14): {atr}` as a dedicated field; replaces vague rule with concrete computed comparison e.g. `"stop width (1.23) is wider than 3x ATR (3.69)"`. Graceful fallback when ATR is None.

**Bug 4 (High): `SignalSource.TECHNICAL` on Critic signal corrupts weight feedback loop**
Critic labelled its BUY/STRONG_SELL output as TECHNICAL. Weekly `update_weights.py` attributed Critic veto decisions to Technical agent accuracy scores.
- `core/constants.py`: Added `CRITIC = "critic"` to `SignalSource` enum
- `agents/critic.py`: Changed `source=SignalSource.TECHNICAL` to `source=SignalSource.CRITIC`

**Bug 5 (Medium): Fail-open when Critic returns empty signals list**
Empty signals list set `critic_signal=None`, `critic_blocked=False` — trade silently bypassed the Critic gate.
- `agents/orchestrator.py`: `critic_blocked` now defaults `True` when `critic_signal is None`; `logger.warning` fires so the bypass is always visible in logs

**Bug 6 (Medium): Default confidence inverted — rejections defaulted higher than approvals**
When LLM omitted confidence field: approvals defaulted 0.70, rejections 0.75. STRONG_SELL veto appeared more certain than BUY approval.
- `agents/critic.py`: Swapped defaults in JSON parse path to `0.75 if approved else 0.70`
- `agents/critic.py`: Swapped defaults in keyword fallback path to `0.75 if approved else 0.70` (fixed in follow-up pass after initial Bug 6 fix missed the fallback path)

**Bug 7 (Medium): "valid" keyword in fallback scan causes false approvals**
Prompt contains "Respond with ONLY valid JSON". If LLM echoed "Here is valid JSON..." and JSON parsing failed, fallback saw "valid" and returned `approved=True`.
- `agents/critic.py`: Removed `"valid" in lower` from keyword scan. Only `"approve"` and `"proceed"` remain — unambiguous intent words that do not appear in prompt boilerplate

**Bug 8 (Low): R/R formula uses inconsistent fallbacks for same field**
Numerator used `entry_price or 0`, denominator used `entry_price or 1` — different fallback values for the same field in the same formula.
- `llm/prompts/spike_analysis.py`: Both sides now use `entry_price or 0` consistently

**Bug 9 (High): NEUTRAL signal not gated before Phase 4**
`decision_signal is None` check did not catch NEUTRAL. A NEUTRAL signal flowed into Phase 4, called TradePlanAgent (which returned early internally), then called Critic with `plan=None` (which returned empty signals), then `critic_blocked=True` blocked the non-existent trade. Wasteful and semantically wrong.
- `agents/orchestrator.py`: Gate now reads `if decision_signal is None or decision_signal.signal_type == SignalType.NEUTRAL` — both exit before Phase 4 with a log message that includes the actual signal type for auditability

**Gap 2 / Bug 11 (Low): `Timeframe.D1` hardcoded regardless of mode — manually fixed**
Critic always emitted a D1-timeframe signal regardless of intraday or long-term mode. In intraday mode this is a semantic mismatch with M5/M15/H1 signals.
- `agents/critic.py`: One-line manual fix — `timeframe=Timeframe.H1 if self.mode == TradingMode.INTRADAY else Timeframe.D1`
- Note: H1 is a representative label for intraday, not per-signal precision. Per-timeframe precision deferred until Gap 3 restructures the Critic's input (see below).

---

#### Design Gaps — All Implemented

**Gap 1 / Bug 10: Critic reasoning never stored — IMPLEMENTED**
`raw_data` stored only `llm_response` and `approved`. `biggest_risk` and `failure_scenario` were discarded. Blocked trades left no DB record of why they were blocked.
- `data/storage/db.py`: Added `critic_log` table to `init_schema()` with columns for `approved`, `confidence`, `reasoning`, `biggest_risk`, `failure_scenario`, `entry_price`, `stop_loss`, `take_profit`, `rr_ratio`, `trade_id` (NULL if blocked, FK to `trade_decisions.id` if executed). Index on `ticker`.
- `data/storage/decision_store.py`: Added `save_critic_verdict()` function.
- `agents/critic.py`: `_parse_response` now returns 5 values (added `biggest_risk`, `failure_scenario`); both fields added to `raw_data`.
- `agents/orchestrator.py`: Calls `save_critic_verdict()` after every Critic run; `trade_id` from `execution.raw_data` passed when trade executed (NULL when blocked or risk-failed).

**Gap 3 / Bug 12 + Gap 4 / Bug 13: Conflict rule unevaluable, no regime in Critic prompt — IMPLEMENTED**
Supporting summary was a flat 500-char truncated string. LLM could not evaluate "signals conflict across timeframes" rule and had no regime label.
- `llm/prompts/spike_analysis.py`: Added `build_structured_summary(regime, signals)` helper that groups signals by source and timeframe, formatting each as `Source [TF]: SIGNAL_TYPE confidence | reasoning[:80]`. Regime is the first line. Prompt parameter renamed `structured_summary`; 500-char truncation removed.
- `agents/orchestrator.py`: Imports and calls `build_structured_summary(regime_label, all_signals)` before the Critic call; passes result as `supporting_summary`.

**Gap 5 / Bug 14: HOLD as first-class decision — IMPLEMENTED (logging scope)**
HOLD was a silent no-op. Open positions carried each cycle with no auditable record.

Position management rule set:

| Situation | Action | Status |
|---|---|---|
| Open position + STRONG_BUY | Add 20% of original quantity | Deferred — reconciliation rework needed |
| Open position + BUY | Hold, log to DB with regime and unrealized PnL | **Implemented** |
| Open position + SELL | Trim 30% of current quantity | Deferred — `client_order_id` collision, `current_quantity` column needed |
| Open position + STRONG_SELL | Full exit via reversal block | Already implemented (Session 27) |

- `data/storage/db.py`: Added `hold_decisions` table with `signal_type`, `confidence`, `regime`, `reasoning`, `entry_price`, `current_price`, `unrealized_pnl`. Index on `ticker`.
- `data/storage/decision_store.py`: Added `save_hold_decision()` function.
- `agents/orchestrator.py`: HOLD block inserted after reversal check, before Phase 4. When open position exists and signal is `BUY`: fetches current price from candle cache, computes `unrealized_pnl = (current_price - entry_price) * quantity`, calls `save_hold_decision()`, logs and returns without entering Phase 4. `SignalType.NEUTRAL` removed from hold check — it is dead code (NEUTRAL is already gated at line 236 and can never reach this point).

**Cleanup: Dead code removed**
- `agents/orchestrator.py`: `signal_is_hold` tuple changed from `in (SignalType.BUY, SignalType.NEUTRAL)` to `== SignalType.BUY` — NEUTRAL is gated before this point and could never match.

---

### Session 29 — Execution / Reconciliation Bug Audit: 8 Bugs Fixed
**Status:** Complete. 8 bugs fixed across `agents/execution.py`, `execution/broker/alpaca.py`, `agents/orchestrator.py`, `data/storage/decision_store.py`, `data/storage/db.py`.
**Files modified:** `agents/execution.py`, `execution/broker/alpaca.py`, `agents/orchestrator.py`, `data/storage/decision_store.py`, `data/storage/db.py`

#### Bug Fixes

**Bug 1 (High): Exit fallback `client_order_id` breaks reconciliation permanently**
When `get_open_filled_entry_trade()` returned `None`, the exit execution fell back to `f"exit-{trade_id}-{strategy_mode}"` using the SELL row's own ID. The reconciliation loop builds `exit_coid = f"exit-{entry_trade_id}-{strategy_mode}"` using the ENTRY row's ID — these never match. The order executed on Alpaca but the entry row was never closed, PnL was never recorded, and the entry accumulated in `get_filled_trades()` indefinitely.
- `agents/execution.py`: Removed fallback entirely. When `get_open_filled_entry_trade()` returns `None`: log error with ticker/mode/trade_id, mark the SELL row `FAILED`, return early without touching the broker.

**Bug 2 (Medium/High): Market order fill price falls back to 0.0, not `entry_price`**
`submit_market_order()` used `order.filled_avg_price or 0` while limit and stop orders both used `or plan.entry_price`. If `filled_avg_price` was `None` (order accepted but not yet confirmed), market orders stored `0.0` as fill price in the DB. `_estimate_pnl` recovered silently via falsy fallback, but `filled_price = 0.0` was permanently stored and visible to all other consumers of that column.
- `execution/broker/alpaca.py` line 39: Changed `or 0` to `or plan.entry_price`, matching the limit/stop fallback pattern.

**Bug 3 (High): `get_filled_trades()` fetched all modes — manually fixed by user**
`_reconcile_closed_trades()` called `get_filled_trades()` without a `strategy_mode` filter, so each orchestrator instance reconciled positions from both modes. Fixed by user: `get_filled_trades(strategy_mode=self.mode.value)`.

**Bug 4 (Medium): `closed_outcomes` keyed by ticker alone, not `(ticker, mode)`**
If two filled entry rows for the same ticker closed in the same reconciliation cycle, the second write silently overwrote the first in the dict. The first trade's PnL was lost and its memory write-back never fired.
- `agents/orchestrator.py`: Changed key from `ticker` to `(ticker, strategy_mode)` in `_reconcile_closed_trades()`. Updated type annotation to `dict[tuple[str, str], dict]`. Updated lookup at the call site to `closed_outcomes.get((ticker, self.mode.value))`.

**Bug 5 (Medium): `close_trade()` overwrites `filled_at` with the close timestamp**
`close_trade()` wrote the exit fill time into `filled_at`, destroying the entry fill timestamp that `update_trade_status()` had stored there. Entry hold-time calculations and dashboard entry timestamps were wrong for all closed trades.
- `data/storage/db.py`: Added `closed_at TEXT` column to `trade_decisions` schema. Added idempotent `ALTER TABLE trade_decisions ADD COLUMN closed_at TEXT` migration.
- `data/storage/decision_store.py`: `close_trade()` now writes `closed_at` instead of `filled_at`. `filled_at` is now permanently the entry fill timestamp for all rows.

**Bug 7 (Low): `OrderStatus` imported inside method body on every reconciliation call**
`get_fill_by_client_order_id()` ran `from alpaca.trading.enums import OrderStatus` on every call. Every other enum in the class (`OrderSide`, `TimeInForce`, etc.) is imported once in `__init__` and stored as `self._*`.
- `execution/broker/alpaca.py`: `OrderStatus` added to the `__init__` import block and stored as `self._OrderStatus`. Inline import in `get_fill_by_client_order_id()` replaced with `self._OrderStatus.FILLED`.

**Bug 8 (Low): `get_last_sell_fill_price()` is dead code and lane-blind**
Method was never called anywhere in the pipeline. It queries by ticker alone — if both intraday and long-term lanes closed the same ticker it returns the most recent SELL fill without knowing which lane, making it unsafe to wire in later.
- `execution/broker/alpaca.py`: Method deleted entirely.

**Bug 9 (High): `signal_is_hold` checked `BUY or NEUTRAL` — manually fixed by user**
`NEUTRAL` was already gated at the top of the run block and could never reach the hold check. Fixed by user: changed `in (SignalType.BUY, SignalType.NEUTRAL)` to `== SignalType.BUY`.

---

### Session 27 — Trade Plan Agent Audit: Exit Logic and ATR Mode Differentiation
**Status:** Complete. All 4 gaps implemented and post-implementation bugs corrected.
**Files modified (27A):** `data/storage/decision_store.py`, `agents/orchestrator.py`, `scripts/run_paper.py`
**Files modified (27B):** `agents/orchestrator.py`, `agents/trade_plan.py`, `core/config.py`

#### Audit findings from code review
Five files were read and audited against the documented design: `agents/trade_plan.py`, `core/models.py`, `data/feeds/price_feed.py`, `agents/orchestrator.py`, `agents/execution.py`, `data/storage/decision_store.py`.

One item was confirmed correct and requires no change: ATR timeframe IS already mode-differentiated at line 27 of `trade_plan.py` (`Timeframe.M15` for intraday, `Timeframe.D1` for long-term). The tight/wide stop distinction from the design doc is delivered naturally through ATR magnitude at different timeframes.

Four confirmed gaps were found:

---

#### Issue 1: No SL/TP price monitoring (Condition 1)
**Root cause:** `_reconcile_closed_trades()` in `orchestrator.py` is detection-only — it looks for exit orders that have already been submitted and filled in Alpaca. Nothing in the system ever submits those exit orders based on price breach. Open positions sit indefinitely.

**Confirmed:** `get_filled_trades()` uses `SELECT *` and `stop_loss`/`take_profit` are persisted columns in `save_trade_plan()`, so they are already returned — no schema change needed.

**Design locked:**
- New method `_check_sl_tp_exits()` on `Orchestrator`
- Calls `get_filled_trades(strategy_mode=self.mode.value)` to get all open positions with their SL/TP levels
- For each position, fetches current price via `fetch_snapshot(ticker).price`
- BUY position exits if `current_price <= stop_loss OR current_price >= take_profit`
- SELL position exits if `current_price >= stop_loss OR current_price <= take_profit`
- Before submitting, calls `has_pending_exit(trade["id"])` guard (Issue 3 below) to skip if an exit is already in flight
- If breach and no pending exit: constructs a MARKET order close TradePlan and calls `self.execution.run()`
- **Market hours guard:** `_check_sl_tp_exits()` must call `_is_market_open()` at the very top and return early if market is closed. This is required because for long-term mode the method is called directly from `run_paper.py`, bypassing the `_is_market_open()` gate that lives in `run_all()`. Without this guard the 5-minute long-term price check fires on weekends and pre-market.
- Intraday: called inside `run_all()` every 60-second cycle, before the ticker loop
- Long-term: called from `run_paper.py` every 300 seconds via a separate `last_longterm_price_check` timer

**`run_paper.py` change:**
```
last_longterm_price_check: float = 0.0   # add alongside last_longterm_run

# inside while loop, after intraday block:
now = time.monotonic()
if now - last_longterm_price_check >= 300:
    try:
        longterm_orch.check_sl_tp_exits()
    except Exception:
        logging.exception("Long-term SL/TP check error — continuing")
    last_longterm_price_check = time.monotonic()
```

---

#### Issue 2: No signal-reversal exit (Condition 2)
**Root cause:** `run_ticker()` has no check for an existing open position before Phase 4. A long position plus a SELL signal generates no new plan and leaves the long position open.

**Design locked:**
- New block inserted in `run_ticker()` after the decision signal is produced and after the BEAR_TREND gate (before Phase 4)
- Calls `get_open_filled_entry_trade(ticker, self.mode.value)` — already in `decision_store.py`, just needs importing in `orchestrator.py`
- Checks reversal: open side is `buy` and new signal is SELL/STRONG_SELL, or open side is `sell` and new signal is BUY/STRONG_BUY
- If reversal: constructs a MARKET order close TradePlan, calls `self.execution.run()`, then returns immediately
- Goes flat — no new entry in the same cycle. The decision to enter the opposing direction is left to the next cycle after the exit is reconciled.
- `stop_loss=0.0` and `take_profit=0.0` on the close plan are intentional — these fields are not meaningful for a market close order

---

#### Issue 3: Double-submission risk on SL/TP exits
**Root cause:** After an exit order is submitted, the entry trade's status stays `'filled'` until `_reconcile_closed_trades()` detects the exit fill and calls `close_trade()` (which sets it to `'closed'`). Between submission and reconciliation, multiple price-check cycles would see the same entry as still open and submit duplicate exit orders.

**Design locked:**
- New helper `has_pending_exit(entry_trade_id: int) -> bool` in `decision_store.py`
- Checks for any non-failed SELL row with `client_order_id LIKE 'exit-{entry_trade_id}-%'` and `status != 'failed'`
- Called inside `_check_sl_tp_exits()` before submitting any exit order

```python
def has_pending_exit(entry_trade_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            """SELECT 1 FROM trade_decisions
               WHERE client_order_id LIKE ? AND side='sell' AND status != 'failed'
               LIMIT 1""",
            (f"exit-{entry_trade_id}-%",),
        ).fetchone()
    return row is not None
```

---

#### Issue 4: Mode-specific ATR multipliers missing
**Root cause:** `trade_plan.py` lines 39-40 use `cfg.STOP_LOSS_ATR_MULT` and `cfg.TAKE_PROFIT_ATR_MULT` for both modes. Single global constants make it impossible to tune stop and target widths independently per mode without changing a global value.

**Design locked:**
- Add four constants to `core/config.py` (all overridable via `.env`):
  - `STOP_LOSS_ATR_MULT_INTRADAY = 1.5`
  - `TP_ATR_MULT_INTRADAY = 2.5`
  - `STOP_LOSS_ATR_MULT_LONG_TERM = 2.5`
  - `TP_ATR_MULT_LONG_TERM = 5.0`
- Replace lines 39-40 in `trade_plan.py`:
```python
if self.mode == TradingMode.INTRADAY:
    sl_mult = cfg.STOP_LOSS_ATR_MULT_INTRADAY
    tp_mult = cfg.TP_ATR_MULT_INTRADAY
else:
    sl_mult = cfg.STOP_LOSS_ATR_MULT_LONG_TERM
    tp_mult = cfg.TP_ATR_MULT_LONG_TERM
stop_dist   = atr * sl_mult
target_dist = atr * tp_mult
```
- Starting R:R ratios: intraday 1.67:1, long-term 2.0:1. Both clear the Critic's 1.5 floor. Tune after backtest data exists.

---

#### Trailing stop — deferred
Design discussed but not locked. Long-term positions currently use a fixed TP level only. `TradePlan` dataclass has no `trailing_stop_dist` field. Deferred to a future session after the 4 confirmed gaps above are implemented and verified.

---

### Session 26 — Decision Weight Feedback Loop Implemented, Memory/Risk/Decision Agent Audit
**Status:** Implemented and verified.
**Files created:** `scripts/update_weights.py`
**Files modified:** `agents/decision.py`, `agents/execution.py`, `data/storage/db.py`, `data/storage/decision_store.py`, `agents/fundamental.py`

#### Decision Weight Feedback Loop — Full Implementation
All components designed in Session 24 are now coded and verified in the live files:

**`data/storage/db.py`**
- `agent_weights` table added to `init_schema()` DDL (idempotent `CREATE TABLE IF NOT EXISTS`)
- Idempotent migration `ALTER TABLE trade_decisions ADD COLUMN signal_snapshot TEXT` runs at startup on existing DBs

**`agents/execution.py`**
- Signal snapshot serialized as JSON at insert time: `[{"source", "signal_type", "confidence"} for each signal]`
- Passed as `signal_snapshot` kwarg to `save_trade_plan()` — attribution is recorded on every trade row at creation, not retrospectively

**`data/storage/decision_store.py`**
- `save_trade_plan(plan, signal_snapshot)` — accepts and persists snapshot in the same INSERT
- `get_closed_trades_for_weights(mode)` — returns `(pnl, signal_snapshot)` for all closed BUY rows with non-NULL snapshot, filtered by mode
- `get_agent_weights(mode)` — reads current weights from `agent_weights` table as `{source: weight}` dict
- `save_agent_weights(mode, weights)` — upserts all sources for a mode in one batch with `updated_at` timestamp

**`agents/decision.py`**
- `_load_weights(mode_str)` — module-level function; reads `agent_weights` table via `get_agent_weights()`, converts string source keys to `SignalSource` enum keys, falls back to hardcoded `_INTRADAY_WEIGHTS`/`_LONGTERM_WEIGHTS` if table empty or unreachable
- `_weight_cache` — module-level dict keyed by `mode_str`; cache TTL is 300 seconds (5 minutes) to avoid per-signal DB reads while staying fresh within a cycle window
- `DecisionAgent.run()` calls `_load_weights(self.mode.value)` instead of directly referencing the hardcoded dicts — weights are now runtime-dynamic without a code deploy

**`scripts/update_weights.py`** (new file)
- Re-scores ALL closed trades on every run (idempotent — no strict delta window)
- For each trade: parses `signal_snapshot`; for each source, scores `confidence × vote × trade_outcome` (vote = +1 for BUY-side, -1 for SELL-side; outcome = +1 if pnl > 0, -1 if pnl ≤ 0)
- Aggregates into per-source raw accuracy score; min-shifts all scores to be non-negative before normalizing to sum-to-1
- Blends candidate with current weights: `new = 0.70 × old + 0.30 × candidate`; re-normalizes blended weights
- Falls back to hardcoded defaults as "current" when `agent_weights` table is empty (first run)
- Runs manually: `python scripts/update_weights.py`

**Open items from Session 24 resolved:**
- Lookback window: always re-score all closed history (idempotent, no delta)
- Snapshot write belongs in `execution.py` — `all_signals` is in scope there
- `update_weights.py` is manually invoked (not cron)

---

#### Memory Agent — Session 22 Open Items Closed
Session 22 carried four open items into implementation. All resolved in the current code:

1. **Exact orchestrator call site** — `self.memory.update()` is called in `Orchestrator.run_ticker()` immediately after the decision signal is computed, conditioned on `closed_trade is not None` (i.e., triggered only when the reconciliation loop detected a position closure that cycle).
2. **Timestamp consistency** — `MemoryAgent.update()` uses `datetime.now(timezone.utc)` for both `last_updated` and `closed_at` defaults; `Signal.timestamp` default factory in `core/models.py` also uses `datetime.now(timezone.utc)` via `_utcnow()`. No naive timestamps anywhere.
3. **Single write path** — `save_memory(ticker, mode, mem)` from `memory/stock_memory.py` is the only write path. `stock_memory.update_memory()` (the older function with no timestamp) is not called anywhere in the live pipeline.
4. **`key_levels` and `reasoning` in `_signals_from_memory()`** — intentionally deferred. `_signals_from_memory()` currently emits two signal types only: directional bias → BUY/SELL at `bias_confidence`, and `avoid_until` flag → STRONG_SELL at 0.9. Wiring `key_levels` into signals (e.g., proximity-to-support BUY signal) is left for a future session when the value is clearer from live trading data.

---

#### Risk Manager Agent — Verification
Full audit of `agents/risk_manager.py` confirms Session 23 implementation is intact:
- `run()` surfaces three status dimensions: circuit breakers (nominal vs active), VaR ($X within/exceeding budget), exposure (open_count/MAX trades). All three joined into `summary`; `raw_data` carries `var_ok`, `var_reason`, `open_trades` for dashboard consumption.
- Both signals emitted by `run()` use `SignalSource.RISK` (not `REGIME`) — the Session 23 mislabel fix is in place.
- `validate_plan()` gate order confirmed: circuit breaker → incremental VaR → per-lane exposure → size cap (resize, never reject). `plan.strategy_mode` passed to `check_exposure_limits()` correctly.
- **Note:** `run()` is called from `Orchestrator.run_ticker()` without a `portfolio_value` argument — it defaults to $100,000 for the VaR status display in `run()`. This is intentional: `run()` is for dashboard surfacing only; the actual risk gate that matters is `validate_plan()`, which receives the live-refreshed `self.portfolio_value` every cycle.

---

#### Decision Agent — Verification
`agents/decision.py` confirmed current state:
- Weights: Intraday — Technical 35%, News 25%, Fundamental 20%, Memory 10%, Regime 10%. Long term — Fundamental 40%, Technical 20%, Regime 15%, News 15%, Memory 10%. Both sum to 100%.
- `_weighted_score()` multiplies source weight × signal confidence × signal score (STRONG_BUY=1.0, BUY=0.5, NEUTRAL=0, SELL=−0.5, STRONG_SELL=−1.0); normalizes by total weighted confidence to make confidence matter.
- `_apply_regime_modifier()` confirmed live: BEAR_TREND halves bullish score, BULL_TREND halves bearish score, HIGH_VOLATILITY reduces all by 30%, BREAKOUT amplifies bullish by 15% (capped at 1.0).
- `_score_to_signal()` thresholds: score > 0.5 → STRONG_BUY; > 0.15 → BUY; < −0.5 → STRONG_SELL; < −0.15 → SELL; else NEUTRAL.

---

#### Fundamental Agent — P/B, D/E, Earnings Growth Scoring Added
**File modified:** `agents/fundamental.py`

Three new scoring function pairs added, all following the same `_sigmoid_conf` + `_MIN_FLOOR = 0.50` pattern as P/E and revenue growth:

| Metric | Buy center | Sell center | Steepness (buy/sell) |
|---|---|---|---|
| P/B ratio | 2.0 | 5.0 | 0.60 / 0.40 |
| Debt-to-equity (normalized) | 0.3x | 1.5x | 4.0 / 1.20 |
| Earnings growth | 15% | −5% | 8.0 / 8.0 |

D/E is already normalized at fetch time in `fundamentals_feed.py` (`raw_de / 100.0`). Guard `debt_to_equity >= 0` added since negative D/E (negative equity) is a separate distress signal, not a leverage score.

FCF and market cap remain fetched-but-unscored — scoring FCF meaningfully requires normalization by market cap or revenue (context-dependent), and market cap alone is a size measure, not a valuation signal.

---

### Session 25 — Verification: Orchestrator Fundamental Gating Bug Was Already Resolved
**Status:** Verified, no code change needed beyond what's already in `run_paper.py`.
**Files checked:** `agents/orchestrator.py`, `agents/decision.py`, `agents/news_sentiment.py`, `scripts/run_paper.py`

- The "orchestrator gating bug" (Fundamental Agent gated behind `LONG_TERM` mode, starving the new 20% intraday Fundamental weight) carried on the open-items list since Session 20 does **not** exist in the current code. `self.fundamental.run(ticker)` runs unconditionally in `run_ticker()`. `SignalSource.FUNDAMENTAL` is correctly weighted at 20% intraday / 40% long-term in `decision.py`. News Layer 1 already runs in both modes. This was a stale list entry, not a live bug — likely fixed silently in an earlier session and never removed from tracking.
- The actual blocking bug found during verification: `scripts/run_paper.py` was importing `TradingMode` from `core.constants`, where it isn't defined (it lives in `core.config`) — causing an `ImportError` at startup before either orchestrator could be constructed. Confirmed fixed in the uploaded file (`from core.config import TradingMode`).
- Moved to Outstanding Stubs as struck through (#15); removed from the active Consolidated Open Bug List.
- **Flag carried forward:** `detect_doji` (originally flagged as dead-code import) also appears already resolved per the Session 18/audit review of `timeframe_aggregator.py` — recommend the same direct-file verification before spending a Claude Code session on it.

---

### Session 24 — Decision Weight Feedback Loop (Design Locked)
**Status:** Design locked Session 24. Fully implemented — see Session 26 for implementation details.
**Files created:** `scripts/update_weights.py`
**Files modified:** `agents/decision.py`, `agents/execution.py`, `data/storage/db.py`, `data/storage/decision_store.py`

#### Problem
`_INTRADAY_WEIGHTS` and `_LONGTERM_WEIGHTS` in `agents/decision.py` are hardcoded constants with no mechanism feeding realized P&L back into them. Additionally, nothing today links a closed trade's outcome back to which sources voted on it and how confidently — `signals` rows aren't tied to a `trade_decisions.id`, so there's no attribution to learn from even if a feedback loop existed.

#### Method chosen: confidence-weighted accuracy
For each closed trade, for each contributing source: was that source's directional call (BUY/SELL side) correct given the trade's realized outcome, weighted by the confidence that source expressed at decision time. Aggregated across all trades closed in the lookback week, per source, per mode, into an accuracy score. Chosen over plain win-rate (too coarse — ignores confidence magnitude) and regression (too data-hungry for current trade volume; risks fitting noise).

#### Part A — Attribution (new requirement)
At the moment a trade is created (decision → trade plan → row insert), snapshot the contributing signals (source, signal_type, confidence) and store as JSON on the trade row — new `trade_decisions.signal_snapshot` column — rather than a separate join table. Avoids a schema with joins for a single read-path use case.

- **File to modify:** `data/storage/db.py` — add idempotent migration: `ALTER TABLE trade_decisions ADD COLUMN signal_snapshot TEXT`
- **File to modify:** wherever the trade row is first inserted (`agents/trade_plan.py` or `data/storage/decision_store.py`, depending on where `all_signals` is in scope) — serialize the contributing signals to JSON at insert time

#### Part B — Weekly batch job (`scripts/update_weights.py`)
Runs weekly (manually triggered or cron — implementation session to decide which). For each mode (intraday, long_term):
1. Pull all trades closed since the last run (or all closed trades — open question, see below)
2. For each trade, parse `signal_snapshot`; for each source, score correctness × confidence against the trade's realized P&L sign
3. Aggregate into a per-source accuracy score for the week
4. Normalize accuracy scores into a candidate weight set (sums to 1)
5. Blend with the previous week's weights: **`new_weight = 0.7 × old_weight + 0.3 × candidate_weight`**
6. Write blended weights to `agent_weights` table

**Explicit decisions locked this session:**
- **No minimum trade count** — the job updates every week regardless of sample size, even on a single closed trade. Accepted risk: thin weeks can swing a source's accuracy score on very little evidence. The 70/30 blend dampens but does not eliminate this.
- **No floor/ceiling on weights** — a source's weight can in principle walk toward ~0% or come to dominate the ensemble over many weeks if it's consistently wrong/right. Flagged as worth revisiting once a few months of live weekly updates exist to look at, but left unconstrained for now per explicit decision.
- **Smoothing factor: 70% old / 30% new** — favors stability over responsiveness.

#### Storage: `agent_weights` table (new)
Weights move from hardcoded constants to a DB table the Decision Agent reads at runtime, so the weekly job can write updates without a code deploy.

```sql
CREATE TABLE agent_weights (
    mode       TEXT NOT NULL,   -- 'intraday' / 'long_term'
    source     TEXT NOT NULL,   -- 'technical', 'news', 'fundamental', 'regime', 'memory'
    weight     REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (mode, source)
);
```

`agents/decision.py` reads from this table (cached, refreshed per cycle or on a TTL) and falls back to the existing hardcoded `_INTRADAY_WEIGHTS`/`_LONGTERM_WEIGHTS` dicts as defaults if the table is empty or unreachable — so the system is never without valid weights, including before the first weekly run.

#### Open items carried into implementation session
- Exact lookback window for "trades closed since last run" — strict delta vs. always re-scoring all closed history
- Where the snapshot-write actually belongs (`trade_plan.py` vs `decision_store.py`) — depends on where `all_signals` is already in scope at insert time
- Whether `update_weights.py` is cron-scheduled or manually invoked
- Whether to eventually revisit floor/ceiling bounds once live weekly data exists

---

### Session 23 — Risk Manager: Resize, Kelly Wiring, Incremental VaR, run() Visibility, SignalSource Fix
**Files modified:** `risk/portfolio_exposure.py`, `risk/position_sizing.py`, `agents/trade_plan.py`, `agents/risk_manager.py`, `core/constants.py`
**Status:** Implemented.

#### 1. Sector concentration veto removed — `risk/portfolio_exposure.py`
- The `>= 3` sector blocking check inside `check_exposure_limits()` is deleted entirely.
- `_SECTOR_MAP` (the ticker→sector dict) is retained for dashboard grouping and reporting use.
- `check_exposure_limits()` now only runs two gates: concurrent-trade cap → per-lane duplicate check. No sector-based veto remains.

#### 2. Resize capability — `agents/risk_manager.py`
- `validate_plan()` previously returned `(False, reason)` when `plan.quantity > max_qty * 1.05`.
- Now trims `plan.quantity = round(max_qty)` in-place and continues to approval.
- Trades are never rejected purely for being oversized — they are resized to the risk-adjusted maximum and approved.

#### 3. Kelly sizing wired — `risk/position_sizing.py`, `agents/trade_plan.py`
- Added `get_historical_performance(ticker) → (win_rate, avg_win, avg_loss)` in `position_sizing.py`.
  - Reads up to 100 closed trades from `decision_store.get_trade_history()`.
  - Returns `(0, 0, 0)` when fewer than 5 closed trades exist — not enough history to trust Kelly.
- In `TradePlanAgent.run()`:
  - ATR-based quantity computed as before.
  - If history is sufficient, Kelly quantity is also computed via `kelly_position_size()` (quarter-Kelly, `fraction=0.25`).
  - Final quantity = `min(atr_qty, kelly_qty)` — conservative blend; falls back to ATR-only when history is thin.
  - `reasoning` field now records which sizing method was used and win rate (e.g. `Kelly(wr=58%)`).

#### 4. Incremental VaR — `risk/portfolio_exposure.py`
- Extracted `_fetch_vol_and_price(ticker, fallback_price) → (daily_vol, price)` — shared helper that fetches 60-day realized vol and latest close, falling back to 2% vol and stored price on any data failure. Deduplicates identical logic that previously existed in two places.
- Added `_candidate_var(ticker, quantity, price, confidence)` — 1-day parametric VaR for a single not-yet-open trade using the shared helper.
- `check_var_limit()` signature extended: `candidate_ticker`, `candidate_qty`, `candidate_price` (all optional, default to empty/0).
  - Now computes `existing_var + candidate_var` before comparing to the risk budget.
  - Rejection message distinguishes incremental vs portfolio-only breach: `"Incremental VaR $X (existing $Y + candidate $Z) exceeds budget $W"`.
- `validate_plan()` in `risk_manager.py` now passes `plan.ticker`, `plan.quantity`, `plan.entry_price` to `check_var_limit()` so the candidate trade's own VaR is included in the gate.

#### 5. `run()` standalone visibility — `agents/risk_manager.py`
- Previously `run()` only emitted a circuit-breaker `STRONG_SELL` signal and a one-line summary.
- Now surfaces three status dimensions for the dashboard and orchestrator:
  1. **Circuit breakers** — nominal or active (with reason).
  2. **VaR** — current total VaR vs budget (calls `check_var_limit()` with no candidate). Emits a `STRONG_SELL` signal at 0.9 confidence if VaR is already breached.
  3. **Exposure** — open trade count vs max (`open_count/MAX_CONCURRENT_TRADES trades open`).
- All three are joined into the `summary` string. `raw_data` carries `var_ok`, `var_reason`, `open_trades` for programmatic dashboard use.

#### 6. `SignalSource.REGIME` mislabel fixed — `core/constants.py`, `agents/risk_manager.py`
- Added `RISK = "risk"` to the `SignalSource` enum in `core/constants.py`.
- Both signals emitted by `RiskManagerAgent.run()` (circuit-breaker and VaR-breach) now use `SignalSource.RISK` — they are risk-engine outputs, not market regime classifications.

---

### Session 22 — Memory Agent Write-Back: Implemented
**Files modified:** `agents/orchestrator.py`, `agents/memory_agent.py`, `core/constants.py`, `data/storage/decision_store.py`, `execution/broker/alpaca.py`
**Status:** Implemented.

#### Background
Audit (via uploaded `memory_agent.py` and `memory/stock_memory.py`) confirmed the Memory Agent is currently read-only in the live pipeline: `MemoryAgent.run()` loads the per-`(ticker, mode)` JSON snapshot and emits signals from it every cycle, but `MemoryAgent.update()` is never called anywhere. No file currently writes back into memory after a decision or trade. The SQLite `INSERT OR REPLACE` in `stock_memory_store.py` is unrelated — that's the News Sentiment Agent's `quarterly_thesis` store, not the Memory Agent.

#### Decisions locked
1. **Update trigger: outcome on PnL (trade close), not fill.** Memory is written back only once a `TradePlan` closes and `pnl` is known, not at entry confirmation. This lets `bias_history` record whether the bias was actually right, not just that a trade was attempted.
2. **Ownership: Orchestrator calls `MemoryAgent.update()`.** Consistent with the existing pipeline shape (Decision → Trade Plan → Critic → Risk Manager → Approval Gate → Execution, all coordinated by Orchestrator). Execution Agent reports the closed outcome back to the Orchestrator; Orchestrator is the one that calls into the Memory Agent. Execution itself does not own the memory write.
3. **Two-tier write structure (carried from prior design discussion):**
   - **Overwrite-in-place:** `directional_bias`, `bias_confidence`, `key_levels`, `avoid_until`, `last_updated` — current-state fields, no value in keeping history.
   - **Append-only:** `bias_history` — a list of `{bias, confidence, reasoning, pnl, closed_at}` entries, appended only when `directional_bias` actually changes between cycles, not on every PnL-close event, to avoid bloating the JSON file across 20 tickers × 2 modes running continuously.

#### Confirmed memory schema
```json
{
  "directional_bias": "bullish",
  "bias_confidence": 0.7,
  "key_levels": {"support": 410.2, "resistance": 430.0},
  "avoid_until": null,
  "last_updated": "...",
  "bias_history": [
    {
      "bias": "bearish",
      "confidence": 0.6,
      "reasoning": "...",
      "pnl": -42.50,
      "closed_at": "..."
    },
    {
      "bias": "bullish",
      "confidence": 0.7,
      "reasoning": "...",
      "pnl": 118.30,
      "closed_at": "..."
    }
  ]
}
```
Top-level fields are overwritten in place every cycle. `bias_history` entries are appended only on a `directional_bias` change, keyed by `closed_at` (trade-close timestamp) and carrying that trade's `pnl` — not `changed_at`, since the append happens at trade close, not at the moment belief shifted in isolation.

#### Open items carried into implementation session
- Exact call site in `agents/orchestrator.py` — needs to hook wherever `TradePlan.status` transitions to closed and `pnl` becomes non-`None`
- Whether `key_levels` and `reasoning` (promised in the original Memory Agent design spec but never wired into `_signals_from_memory()`) get added in this same pass or deferred
- Timestamp convention mismatch: `Signal.timestamp` defaults to naive `datetime.utcnow()` (`core/models.py`) vs. `MemoryAgent.update()`'s timezone-aware `datetime.now(timezone.utc)` — needs reconciling before `bias_history` entries mix the two
- `stock_memory.update_memory()` (no timestamp) vs. `MemoryAgent.update()` (adds `last_updated`) are two separate functions doing overlapping jobs — confirm only one write path is used going forward, or consolidate

---

### Session 22 Follow-up — Reconciliation Loop BUY-Only Filter
**Files modified:** `data/storage/decision_store.py`
**Status:** Implemented.

#### Bug
`_reconcile_closed_trades()` in the Orchestrator called `get_filled_trades()`, which returned all `status='filled'` rows — both BUY (entry) and SELL (exit) rows. When the loop hit a SELL row, it built `exit-{trade_id}-{strategy_mode}` using the SELL row's own `id`. But the real exit order's `client_order_id` encodes the **entry** trade's `id` (built in `ExecutionAgent` as `exit-{entry_id}-{strategy_mode}`). So the lookup against Alpaca always returned `None` for SELL rows, they were never marked closed, and they accumulated in the query result every cycle — checked uselessly on every reconciliation pass indefinitely.

#### Fix
Added `AND side='buy'` to both branches of `get_filled_trades()` in `data/storage/decision_store.py`. SELL/exit rows are now permanently excluded from the reconciliation query.

```python
# Before
"SELECT * FROM trade_decisions WHERE status='filled'"

# After
"SELECT * FROM trade_decisions WHERE status='filled' AND side='buy'"
```

No changes needed in `_reconcile_closed_trades()` itself — the correct data now comes from the store.

#### Terminal state of SELL rows
SELL/exit rows stay at `status='filled'` permanently. They represent the exit *action*, not an open position, so `filled` is their correct terminal state. Marking them `closed` would require parsing `client_order_id` to find the parent entry row — extra complexity with no functional benefit. If `closed` is wanted for reporting cleanliness later, that is a separate small change.

---

### Session 21 — New Chart Pattern: Regression-Channel Breakout (Technical Agent)
**Source:** Vansh's existing rule-based breakout notebook (`BreakOut_Strategy_Kotak_15T_Final.ipynb`), detection logic only — visualization and `backtesting.py` strategy/risk-sizing cells excluded by design
**File to modify:** `signals/patterns/chart_patterns.py` (new pattern function), wired into `timeframe_aggregator.py` alongside existing H&S/triangle/pennant/range-breakout detectors
**Status:** Design only. Not yet implemented in code — pending Claude Code session.

#### Why this lives in Technical Agent, not Market Regime
This is a per-ticker price-structure pattern (needs each ticker's own candle history), not a market-wide read. Market Regime Agent classifies regime off SPY/VIX only and shares one label across all 20 tickers per cycle — fundamentally different scope. This pattern is a sibling to the existing H&S/triangle/pennant/range-breakout detectors already in the Technical Agent, not a regime input.

#### Detection logic (carried over from notebook, adapted)

**1. Pivot detection** — for each candle, look `WINDOW` bars left and right (suggested default 5). If the candle's low is the lowest in that window → pivot low. If its high is the highest → pivot high.

**2. Channel fitting** — over the last `BACKCANDLES` bars (suggested default 40), take the most recent 3 pivot highs and 3 pivot lows. Fit a linear regression line through the highs (resistance) and a separate line through the lows (support). Two quality gates must both pass:
- **R² ≥ 0.90** (`R_SQ_MIN`) for both lines — points must sit close to a straight line
- **Parallel constraint** (`PARALLEL` = 0.1) — the two lines' slopes can't diverge beyond this tolerance, ensuring it's a genuine channel and not two unrelated lines
- If fewer than 2 pivots exist on either side, or either gate fails, no valid channel — no breakout signal possible for that candle

**3. Breakout classification, at the current candle, against the fitted channel:**
- **Bullish breakout** → price breaks above resistance (upper channel line) → confirmed when both the current candle's open AND close sit above the upper line at that bar (not just a wick poke-through) → **expectation: price continues up**
- **Bearish breakout** → price breaks below support (lower channel line) → confirmed when both open AND close sit below the lower line at that bar → **expectation: price continues down**

This matches the standard breakout convention you confirmed: a clean break of resistance is bullish (continuation upward), a clean break of support is bearish (continuation downward) — not a reversal/bounce interpretation.

#### What is explicitly excluded
- The `backtesting.py` `Strategy` class (fixed entry-offset %, fixed SL %, fixed TP ratio, order-age expiry) — this is a full separate trade-execution philosophy with its own risk sizing, not a signal generator. Your system already owns this differently via Trade Plan Agent (ATR-based SL/TP) and Risk Manager. Mixing the two would create two conflicting risk-sizing approaches in one pipeline.
- All Plotly visualization/charting cells.

#### Open implementation decisions (for the Claude Code session)
- **Confidence scoring:** sigmoid-scored off R² (better channel fit → higher confidence) to match the Session 18 RSI/StochRSI pattern and Session 20 Fundamental sigmoid pattern, rather than a flat confidence value
- **Parameter defaults:** `WINDOW=5`, `BACKCANDLES=40`, `PARALLEL=0.1`, `R_SQ_MIN=0.90` carried from the notebook as starting points — not yet tuned/validated against the live system's tickers or timeframes
- **Timeframe applicability:** notebook was built on 15-minute forex candles; needs confirmation this generalizes cleanly to the equity timeframes already used in `timeframe_aggregator.py`
- **Naming collision check:** confirm this doesn't conflict with the existing "range breakout" pattern already documented in the Technical Agent (Section on chart pattern detection) — may need to either replace/upgrade that pattern or clearly differentiate the two by name

---

### Session 20 — Fundamental Agent Audit, Decision Weight Rebalance, Layer 1 Intraday Enablement
**Files to modify:** `agents/fundamental.py`, `data/feeds/fundamentals_feed.py`, `agents/decision.py`, `agents/news_sentiment.py`
**Status:** Design and audit complete. Not yet implemented in code — pending Claude Code session.

#### Audit findings — `agents/fundamental.py` and `data/feeds/fundamentals_feed.py`

**Documented vs implemented gap:**
- Design called for SEC EDGAR (earnings, balance sheet, cash flow, revenue trajectory), analyst consensus, insider activity, and 13F filings, plus mode-aware behavior (soft filter intraday, primary driver long term)
- Actual implementation pulls only from yfinance `.info`; EDGAR functions (`_get_cik`, `_EDGAR_FACTS_URL`, `_EDGAR_SEARCH_URL`) exist in the file but are dead code, never called by `fetch_fundamentals`
- No analyst consensus, insider activity, or 13F data anywhere in the agent
- Agent is mode-blind — `TradingMode` is imported but never referenced; identical scoring regardless of intraday vs long term
- Of 8 fields fetched (P/E, P/B, revenue growth, earnings growth, debt-to-equity, free cash flow, market cap, sector), only 2 (P/E, revenue growth) are scored; 6 are fetched and stored but unused

**Eight bugs/gaps identified, fixes specified below:**

1. **Fallback missing `sector` field** — `except` block in `fetch_fundamentals` constructs `FundamentalData` without `sector`, while the `try` block includes it. If `FundamentalData` requires the field with no default, this turns a recoverable fetch failure into an unhandled crash.
   - **Fix:** add `sector=""` to the exception-path constructor call to match the success path.

2. **Negative P/E misread as undervaluation** — current logic only checks `pe_ratio < 15`, so a company with negative P/E (losing money) is read as "cheap" and emits a BUY signal at 0.6 confidence.
   - **Fix:** add an explicit `pe_ratio <= 0` branch before the `< 15` check, emitting SELL at 0.5 confidence with reasoning "Negative/zero P/E indicates unprofitability."

3. **No caching — fundamentals refetched from yfinance every cycle** — unlike the TTL-aware candle cache used elsewhere, this agent hits yfinance fresh every cycle for all 20 tickers, risking rate limits and wasting calls on data that barely moves intraday.
   - **Fix:** add an in-memory TTL cache (dict keyed by ticker, timestamp + `FundamentalData` tuple) inside `fundamentals_feed.py`, TTL configurable (suggested starting point: 6 hours). Wrap the existing fetch logic in a `_fetch_fundamentals_live()` helper; `fetch_fundamentals()` checks cache first.

4. **No distinction between "missing data" and "neutral data"** — when yfinance returns `None` for all fields (delisted ticker, bad symbol, temporary outage), `_score()` silently returns an empty list, indistinguishable from a genuinely neutral read.
   - **Fix:** in `_score()`, if all key fields (`pe_ratio`, `revenue_growth`) are `None`, emit a single explicit NEUTRAL signal with **confidence 0.0** and reasoning "Fundamental data unavailable for this ticker," so the Decision Agent can tell the difference from a real neutral read (0.3 confidence).

5. **Hardcoded cliff thresholds (P/E 15/40, revenue growth 20%/-5%)** — same class of problem fixed for RSI/Stochastic RSI in Session 18; a P/E of 14.9 vs 15.1 produces a discontinuous jump from BUY 0.6 to NEUTRAL 0.3.
   - **Fix:** port the sigmoid pattern from `signals/indicators/scoring.py` into fundamental scoring. Add `pe_buy_confidence(pe, center=15, steepness=0.15)`, `pe_sell_confidence(pe, center=40, steepness=0.1)`, `revenue_growth_confidence(growth, center=0.0, steepness=8.0)` (or equivalent buy/sell pair). Apply the same `_MIN_FLOOR = 0.50` pattern used in `timeframe_aggregator.py` — compute both buy/sell confidence, emit whichever clears the floor.

6. **Inconsistent signal emission** — P/E always emits a signal (BUY/SELL/NEUTRAL); revenue growth only emits when it crosses a threshold, otherwise nothing. Resolved automatically once Bug 5's floor-based pattern is applied uniformly to both fields — no separate code change needed beyond Bug 5.

7. **Untested EDGAR dead code** — `_get_cik()` assumes a specific JSON response shape from SEC's full-text search endpoint that has never been exercised, since nothing calls it.
   - **Fix:** either delete the unused EDGAR functions now (re-add when actually wiring EDGAR in, with proper testing), or test `_get_cik()` in isolation against a real ticker before leaving it in the file unverified. Decision deferred to implementation session.

8. **Unused fetched fields, including a unit trap on `debt_to_equity`** — P/B, debt-to-equity, earnings growth, free cash flow, market cap, and sector are fetched but never scored. `debt_to_equity` from yfinance is returned as a percentage-like figure (e.g. `145.0` meaning 1.45x), not a clean ratio — a unit mismatch trap if wired in later without normalizing.
   - **Fix (if wiring in now):** normalize via `debt_to_equity = info.get("debtToEquity") / 100 if ... else None` at fetch time. Whether to actually score P/B/D-E/earnings growth now or leave them stored-but-unused is an open decision for the implementation session.

#### Design decision: fresh earnings/filing news should matter intraday too
- Prior gap: Layer 1 (earnings-keyword news filter, `news/layer1_quarterly.py`) ran in LONG_TERM mode only, so a fresh earnings beat/miss or guidance change landing during intraday trading would only be caught if Layer 3's price/volume spike gate also triggered — no dedicated earnings-keyword catch intraday.
- **Decision: Layer 1 now runs in both INTRADAY and LONG_TERM mode.** The earnings-keyword filter + FinBERT scoring is cheap; the full LLM quarterly-thesis generation can still be reserved for LONG_TERM if desired (open detail for implementation — confirm whether intraday Layer 1 runs the full LLM thesis or just the keyword+FinBERT filter stage).
- **File to modify:** `agents/news_sentiment.py` — remove the LONG_TERM-only mode gate on Layer 1's call.

#### Design decision: Decision Agent weight rebalance
Fundamental Agent moves from a 0%-intraday / 35%-long-term split to a meaningfully-weighted role in both modes, reflecting that fundamentals (and now fresh earnings/filing news via Layer 1) matter intraday too, not just long term.

**New Intraday profile:**
| Agent | Old | New |
|---|---|---|
| Technical | 45% | 35% |
| News | 30% | 25% |
| Regime | 15% | 10% |
| Memory | 10% | 10% |
| Fundamental | 0% | 20% |

**New Long Term profile:**
| Agent | Old | New |
|---|---|---|
| Fundamental | 35% | 40% |
| Technical | 25% | 20% |
| Regime | 15% | 15% |
| News | 15% | 15% |
| Memory | 10% | 10% |

Both profiles sum to 100%. **File to modify:** `agents/decision.py` — update the hardcoded weight constants for both modes.

#### Open items carried into implementation session
- Whether intraday Layer 1 runs the full LLM quarterly thesis or just keyword+FinBERT filtering (cheaper, faster)
- Whether to score P/B, debt-to-equity, and earnings growth now (Bug 8) or leave them fetched-but-unused
- Whether to delete or test-then-keep the dead EDGAR code (Bug 7)
- TTL duration for the new fundamentals cache (Bug 3) — suggested 6 hours, not yet confirmed

---

### Session 19 — News Agent Full Audit: Layer Correctness, ATR Safety Fix
**Files created:** `data/storage/stock_memory_store.py`, `llm/prompts/quarterly_analysis.py`
**Files modified:** `news/layer1_quarterly.py`, `news/layer2_premarket.py`, `news/layer3_spike.py`, `news/layer4_weekly.py`, `news/layer5_monthly.py`, `agents/news_sentiment.py`, `llm/prompts/spike_analysis.py`, `llm/prompts/weekly_bias.py`, `llm/prompts/monthly_bias.py`, `signals/indicators/volatility.py`, `agents/trade_plan.py`, `backtesting/replay.py`, `signals/timeframe_aggregator.py`

#### Layer 1 — Quarterly (earnings/analyst focus + LLM added)
- **Before:** Fetched any 90-day news and computed a plain FinBERT average. No earnings/analyst filtering, no LLM, no connection to Layer 4 or Layer 5.
- **After:**
  - `_EARNINGS_KEYWORDS` set filters raw articles to earnings/analyst-relevant content before scoring (keywords: earnings, eps, guidance, upgrade, downgrade, price target, beat, miss, institutional, etc.)
  - New LLM call using `build_quarterly_analysis_prompt` — asks for a structured quarterly thesis covering earnings trend, analyst sentiment shift, institutional tone, and net quarterly stance
  - LLM response drives BUY/SELL/NEUTRAL direction; FinBERT avg drives confidence magnitude
  - Quarterly thesis stored to `stock_memory` table via `upsert_memory(ticker, "long_term", "quarterly_thesis", llm_response)` — Layer 4 and Layer 5 now pull this context into their own LLM prompts

#### Layer 2 — Pre-Market Daily (LLM now drives direction)
- **Before:** LLM was called, but its output was embedded only in the `reasoning` string. BUY/SELL/NEUTRAL direction came purely from `avg_score` (FinBERT). LLM was cosmetic.
- **After:**
  - `if not items: return []` moved before FinBERT scoring and LLM call — no wasted API calls when no overnight news
  - LLM response is parsed for directional keywords (bullish/buy/upside → BUY; bearish/sell/downside → SELL; else NEUTRAL). LLM now drives `stype`.
  - FinBERT `avg_score` used only for `confidence` magnitude (`min(abs(avg_score) + 0.1, 0.9)`)
  - Reasoning string leads with the LLM bias, appends FinBERT score as secondary context

#### Layer 3 — Intraday Spike (market-behavior gate added)
- **Before:** Triggered on `abs(FinBERT_score) >= 0.6` — a sentiment threshold, not a market-behavior threshold. Ran unconditionally every cycle on all intraday news. LLM output was ignored for `stype`; only the sign of the FinBERT score was used.
- **After:**
  - `_detect_market_event(ticker)` gate added: fetches 5-min candles, checks current candle price move > `1.5 × ATR` **or** current volume > `2.0 × 20-bar avg volume`. Returns `(False, "")` if neither condition met.
  - If no market event: items are still saved for downstream layers, but FinBERT and LLM are never called. Makes Layer 3 truly event-driven (~5–20 real events/day instead of 390 scheduled calls).
  - `build_spike_prompt` updated to include `market_event_desc` in the prompt and instruct the LLM to start its response with exactly `BUY`, `SELL`, or `HOLD`. First-word parsing drives `stype`.
  - FinBERT threshold (`0.5`) is now a secondary per-article filter *within* an already-triggered event, not the primary trigger.

#### Layer 4 — Weekly (quarterly context wired in)
- Reads `quarterly_thesis` from `stock_memory` (`get_memory(ticker, "long_term", "quarterly_thesis")`)
- Passes it to `build_weekly_bias_prompt` as an optional `quarterly_thesis` section — the LLM now sees the quarterly narrative when writing the weekly outlook
- `build_weekly_bias_prompt` updated to accept `quarterly_thesis: str | None = None`; section is omitted cleanly when absent (INTRADAY mode or first run before Layer 1 has fired)

#### Layer 5 — Monthly (source tag fixed + quarterly context wired in)
- **Bug fixed:** Was tagged `SignalSource.FUNDAMENTAL` — placed the signal in the Decision Agent's Fundamental bucket (35% long-term weight) instead of the News bucket (15% long-term weight)
- Fixed to `SignalSource.NEWS`
- Also reads `quarterly_thesis` from `stock_memory` and passes to `build_monthly_bias_prompt`
- `build_monthly_bias_prompt` updated to accept `quarterly_thesis: str | None = None`

#### `agents/news_sentiment.py` — Cleanup: undocumented 6th step removed
- **Removed `_aggregate_sentiment`**: was an undocumented D1 signal that ran every cycle in both modes, pulled the last 20 stored articles (regardless of layer), averaged all FinBERT scores, and emitted its own D1 signal — overlapping with Layer 2's D1 signal and drawing from the same correlated data
- Removed all now-unused imports: `SignalType`, `SignalSource`, `Timeframe`, `Signal` (was only used by `_aggregate_sentiment`), `NewsItem`, `fetch_premarket_news`, `get_recent_news`, `save_news`, `score_sentiment`
- Agent now imports only what it actually uses

#### New files
- **`data/storage/stock_memory_store.py`** — `upsert_memory(ticker, mode, key, value)` and `get_memory(ticker, mode, key)` over the existing `stock_memory` SQLite table. Uses `INSERT ... ON CONFLICT DO UPDATE` for idempotent upserts.
- **`llm/prompts/quarterly_analysis.py`** — earnings-focused LLM prompt asking for quarterly thesis on earnings trend, analyst sentiment, institutional tone, and net stance

#### ATR safety fix — `calculate_atr` now returns `float | None`
- **Bug:** `calculate_atr()` fell back to `1.0` when the ATR series was empty. For a high-price stock (e.g. NVDA ~$400), `1.5 × 1.0 = 1.5` means almost any candle move falsely triggers the Layer 3 spike gate.
- **Also:** if the series had fewer than `period=14` bars, `atr.iloc[-1]` returned `NaN` silently, causing downstream arithmetic to propagate NaN without error.
- **Fix:** `calculate_atr` now returns `None` for both failure modes (empty series and NaN last value). Comment explicitly documents that callers must handle `None` and must not substitute a fallback magic number.
- **Callers updated:**
  - `news/layer3_spike.py` — `_detect_market_event` aborts with `(False, "")` and logs a warning if ATR is `None`
  - `agents/trade_plan.py` — removed `else 1.0` fallback; returns an explicit no-plan `AgentOutput` with a clear summary message if ATR is `None` or df is empty
  - `backtesting/replay.py` — skips the entry gracefully (continues to next bar, history still appended) if ATR is `None`
  - `signals/timeframe_aggregator.py` — removed dead `calculate_atr` import (was imported but never called)

---

### Session 18 — Continuous / Fuzzy Confidence Scoring for RSI and Stochastic RSI
**Files created:** `signals/indicators/scoring.py`
**Files modified:** `signals/timeframe_aggregator.py`

#### Problem
RSI and Stochastic RSI used hard binary thresholds: RSI < 30 → BUY at fixed 0.7, else no signal. A reading of 32 produced zero signal; a reading of 29 produced the same 0.7 signal as a reading of 15. This discards useful gradient information and feeds coarse inputs into the Decision Agent.

#### `signals/indicators/scoring.py` (new file)
- `sigmoid_conf(x, steepness)` — generic bounded sigmoid: positive x → confidence > 0.5, negative x → confidence < 0.5
- All functions use `steepness=0.18` (tunable), `numpy` only (no scipy)
- `rsi_buy_confidence(rsi_val, center=33)` — buy-side RSI confidence. Center placed at 33 (just past the classic 30 threshold) so RSI=30 reads ~0.65 (genuinely oversold) rather than 0.50 (ambiguous midpoint). Confidence crosses 0.50 at RSI=33
- `rsi_sell_confidence(rsi_val, center=67)` — symmetric sell-side. RSI=70 reads ~0.65
- `stoch_rsi_buy_confidence(k, center=16)` — %K=20 reads ~0.65; crosses 0.50 at k=16
- `stoch_rsi_sell_confidence(k, center=84)` — %K=80 reads ~0.65; crosses 0.50 at k=84

#### `signals/timeframe_aggregator.py`
- Removed `import math`, `_CONF_FLOOR`, and the local `_sigmoid_conf` function
- Added `_MIN_FLOOR = 0.50` — signals must clear this to be emitted (suppresses near-neutral noise)
- RSI block: computes both `buy_conf` and `sell_conf`, emits whichever side wins **and** clears `_MIN_FLOOR`. Near RSI=50 both sides are near zero so no signal is emitted — correct behavior
- Stochastic RSI block: same pattern. %K/%D cross still applies a +0.05 confirmation bump before the floor check
- Reasoning strings now include the exact computed confidence value for full traceability in the dashboard

#### Confidence reference table (RSI buy side)
| RSI | Confidence |
|-----|------------|
| 33  | 0.50 (threshold) |
| 30  | ~0.65 |
| 25  | ~0.78 |
| 20  | ~0.87 |
| 15  | ~0.93 |

Stochastic RSI mirrors this pattern with center=16 (buy) / center=84 (sell).

---

### Session 17 — Doji Wired, Chart Pattern Library Completed
**Files modified:** `signals/patterns/chart_patterns.py`, `signals/timeframe_aggregator.py`

#### Doji — wired from dead import to active signal
- `detect_doji` was imported in the aggregator but never called — dead code
- Now emits `NEUTRAL` at 0.5 confidence: indecision candle (open ≈ close) is meaningful input to the Decision Agent, acting as a confidence dampener when it appears alongside directional signals

#### Head & Shoulders — bearish H&S already existed; inverse H&S added
- `detect_inverse_head_and_shoulders` — bullish reversal: head trough lower than both shoulder troughs, shoulders symmetric within 5% on the low side
- Both wired: H&S → `STRONG_SELL` at 0.75; Inverse H&S → `STRONG_BUY` at 0.75

#### Bear Flag — existed as dead code, now wired
- `detect_bear_flag` was written but never imported or called
- Now wired alongside `detect_bull_flag`: Bear flag → `SELL` at 0.65

#### Pennants (new)
- `detect_bull_pennant` — strong up pole (>5% gain in first 4 bars) followed by converging consolidation (highs declining, lows rising in bars 5–14)
- `detect_bear_pennant` — strong down pole followed by same converging structure
- Both wired: Bull pennant → `BUY` at 0.65; Bear pennant → `SELL` at 0.65

#### Triangles (new)
- `detect_ascending_triangle` — flat resistance ceiling (top 5 highs within 2%) + rising lows (second-half average above first-half average) → bullish breakout bias
- `detect_descending_triangle` — flat support floor (bottom 5 lows within 2%) + falling highs → bearish breakdown bias
- Both wired: Ascending → `BUY` at 0.70; Descending → `SELL` at 0.70

#### Range Breakout / Breakdown (new)
- `detect_range_breakout` — 20-bar consolidation range (< 8% width), current close > range high × 1.005 → bullish
- `detect_range_breakdown` — same range, current close < range low × 0.995 → bearish
- Both wired: Breakout → `BUY` at 0.70; Breakdown → `SELL` at 0.70

#### Complete wired pattern inventory after this session
| Pattern | Direction | Confidence |
|---|---|---|
| Double bottom | BUY | 0.70 |
| Double top | SELL | 0.70 |
| Inverse H&S | STRONG_BUY | 0.75 |
| H&S | STRONG_SELL | 0.75 |
| Bull flag | BUY | 0.65 |
| Bear flag | SELL | 0.65 |
| Bull pennant | BUY | 0.65 |
| Bear pennant | SELL | 0.65 |
| Ascending triangle | BUY | 0.70 |
| Descending triangle | SELL | 0.70 |
| Range breakout | BUY | 0.70 |
| Range breakdown | SELL | 0.70 |
| Doji | NEUTRAL | 0.50 |

---

### Session 16 — Dashboard Import Fix and Duplicate Navigation Removed
**Files modified:** `dashboard/app.py`
**Directory renamed:** `dashboard/pages/` → `dashboard/views/`

#### Import Fix — `dashboard/app.py`
- Added `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` before all project imports so Streamlit can resolve `data`, `core`, and all top-level packages regardless of launch directory
- Removed a stray `DB_PATH = Path(...) / "trading.db"` line that was accidentally placed before the imports

#### Duplicate Navigation Removed
- Streamlit auto-discovers any folder named `pages/` and generates its own nav entries — these ran raw page files without `render()` so they showed nothing
- Renamed `dashboard/pages/` → `dashboard/views/` to suppress auto-discovery
- Updated all `from pages import X` → `from views import X` in `app.py`

---

### Session 15 — Dual Mode Concurrent Execution
**Status:** Implemented.
**Files modified:** `scripts/run_paper.py`, `core/models.py`, `core/config.py`, `data/storage/db.py`, `data/storage/decision_store.py`, `agents/trade_plan.py`, `agents/risk_manager.py`, `risk/portfolio_exposure.py`

#### Problem
The previous system ran a single `TRADING_MODE` flag, so only intraday or long-term could execute at one time. The requirement is to generate and act on both modes concurrently from a single process, each with its own position lane per ticker.

#### `core/models.py` — `strategy_mode` field on `TradePlan`
- Added `strategy_mode: str = "intraday"` field to `TradePlan` dataclass
- Identifies which pipeline lane owns each trade record — `"intraday"` or `"long_term"`

#### `data/storage/db.py` — Schema migration
- Added idempotent `ALTER TABLE trade_decisions ADD COLUMN strategy_mode TEXT NOT NULL DEFAULT 'intraday'` after the `executescript` block
- Uses try/except on `sqlite3.OperationalError` so existing DBs are migrated on first run without crashing

#### `data/storage/decision_store.py` — Persist and filter by strategy_mode
- `save_trade_plan()` now includes `strategy_mode` in the INSERT statement
- `get_open_trades()` accepts optional `strategy_mode` parameter — filters by lane when provided, returns all open trades when omitted (VaR and sector checks still see the full portfolio)

#### `agents/trade_plan.py` — Stamp strategy_mode on every plan
- `TradePlan(... strategy_mode=self.mode.value)` — the agent already knows its mode at construction, so it propagates it onto the plan object automatically

#### `risk/portfolio_exposure.py` — Per-lane duplicate check
- `check_exposure_limits()` now accepts `strategy_mode` parameter
- Concurrent trade count and sector concentration remain aggregated across both modes (same account equity)
- New per-lane check: if an open trade already exists for `(ticker, strategy_mode)`, the new entry is blocked — prevents two intraday positions in the same ticker while still allowing one intraday + one long-term

#### `agents/risk_manager.py` — Pass strategy_mode from plan
- `validate_plan()` now passes `plan.strategy_mode` to `check_exposure_limits()` — no signature change needed since TradePlan already carries the field

#### `core/config.py` — Long-term loop interval
- Added `LONGTERM_LOOP_INTERVAL_SECONDS = int(os.getenv("LONGTERM_LOOP_INTERVAL_SECONDS", "1800"))` — overridable via env, defaults to 30 minutes

#### `scripts/run_paper.py` — Dual-loop rewrite
- Creates two independent `Orchestrator` instances: one for `INTRADAY`, one for `LONG_TERM`
- Single process, no threads: intraday runs every cycle (60s); long-term runs only when `now - last_longterm_run >= LONGTERM_LOOP_INTERVAL_SECONDS`
- Portfolio value fetched once per cycle and synced to both orchestrators before either pipeline runs
- Cache warm-up at startup covers `INTRADAY_TIMEFRAMES + LONGTERM_TIMEFRAMES` combined (deduped)
- `TRADING_MODE` single-flag config is no longer used as a switch — both modes always run. The env var is retained for backward compat but `run_paper.py` ignores it

#### What remains deferred
- `backtesting/replay.py` position tracking still keys by `ticker` alone — needs `(ticker, strategy_mode)` update before backtesting dual-mode behavior is accurate

---

### Session 14 — Dashboard Import Fix and Duplicate Navigation Removed
**Files modified:** `dashboard/app.py`
**Directory renamed:** `dashboard/pages/` → `dashboard/views/`

#### Import Fix — `dashboard/app.py`
- Added `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` before all project imports
- Without this, Streamlit launching `dashboard/app.py` from any directory other than the project root would fail to resolve `data.storage.db` and all other top-level packages
- Removed a stray `DB_PATH = Path(...) / "trading.db"` line that had been accidentally placed before the imports (that variable is already defined in `core/config.py`)

#### Duplicate Navigation Removed — `dashboard/pages/` → `dashboard/views/`
- Streamlit auto-discovers any folder literally named `pages/` adjacent to the entry script and generates its own navigation sidebar entries for every file inside
- This created a second nav list (app / agent reasoning / live trading / performance / spike log) above the custom "Navigate" radio buttons — clicking those auto-generated entries ran the raw page files without going through `render()`, so they displayed nothing
- Fix: renamed `dashboard/pages/` to `dashboard/views/` — Streamlit only auto-discovers `pages/`, so renaming kills the duplicate nav entirely
- Updated all `from pages import X` statements in `app.py` to `from views import X`
- The custom "Navigate" radio buttons are the only navigation now and work correctly

---

### Session 1 — Project Scaffold
**Files created:** All 87 project files across `core/`, `data/`, `agents/`, `signals/`, `news/`, `llm/`, `memory/`, `risk/`, `execution/`, `backtesting/`, `dashboard/`, `scripts/`

**What's fully implemented:**
- `core/` — config, constants, all dataclasses (Signal, TradePlan, AgentOutput, NewsItem, FundamentalData)
- `data/` — SQLite schema, signal/news/decision stores, yfinance price feed, Finnhub company-news feed, EDGAR fundamentals feed, TTL-aware candle cache
- `agents/` — full 11-agent pipeline: Technical, NewsSentiment, Fundamental, MarketRegime, Memory, RiskManager, Decision, TradePlan, Critic, Execution, Orchestrator
- `signals/` — RSI, MACD, Bollinger Bands, ATR, VWAP, OBV, volume spike, EMA trend direction, hammer, shooting star, engulfing, doji, double top/bottom, bull flag, H&S
- `news/` — FinBERT wrapper with graceful degradation, Layer 1 (quarterly), Layer 2 (premarket + LLM), Layer 3 (intraday spike detector)
- `llm/` — Ollama + Claude API dual client, daily/weekly/monthly/spike prompt templates
- `memory/` — JSON file-per-stock persistent memory with load/save/update/clear
- `risk/` — ATR-based + Kelly position sizing, sector exposure limits, daily loss + consecutive loss circuit breakers
- `execution/` — order router, Alpaca broker adapter (market/limit/stop orders, graceful stub fallback)
- `backtesting/` — walk-forward engine, bar-by-bar signal replayer, slippage cost model, regime stress tester
- `dashboard/` — Streamlit app with Live Trading, Agent Reasoning, Performance Analytics, Spike Log pages
- `scripts/` — DB init script, paper trading entry point

**Known stubs / gaps carried forward from Session 1:**
- `risk/portfolio_exposure.py` — sector map hardcoded to 5 tickers only
- `risk/circuit_breakers.py` — portfolio value hardcoded at $100k
- `agents/critic.py` — LLM response parsing is naive keyword match
- `backtesting/replay.py` — exits every trade after 1 bar (unrealistic)

---

### Session 2 — Signal Engine: EMA Stack, ADX/DMI, Stochastic RSI
**Files modified:** `signals/indicators/trend.py`, `signals/indicators/momentum.py`, `signals/timeframe_aggregator.py`, `tests/unit/test_indicators.py`

#### EMA Stack (9 / 20 / 50 / 200) — `signals/indicators/trend.py`
- `EMAStack` dataclass wraps current and previous bar values
- Properties: `fully_bullish`, `fully_bearish`, `bullish_count` (0–4), `bearish_count` (0–4)
- `golden_cross()` / `death_cross()` — detects EMA50/EMA200 crossovers between consecutive bars
- `calculate_ema_stack(df)` — returns `(current, previous)` tuple for crossover detection
- Wired into aggregator: STRONG_BUY on full bullish stack, STRONG_SELL on full bearish stack, BUY/SELL on 3/4 alignment, STRONG_BUY/SELL on golden/death cross

#### ADX / DMI — `signals/indicators/trend.py`
- `ADXResult` dataclass: `adx`, `plus_di`, `minus_di`
- Properties: `is_trending` (ADX > 25), `is_strong_trend` (ADX > 40), `bullish` (+DI > -DI), `bearish` (-DI > +DI)
- Wilder smoothing via EWM — matches TradingView standard
- Wired into aggregator as a **trend strength filter**: ADX < 25 emits a NEUTRAL dampener signal to reduce confidence of trend-following signals in ranging markets

#### Stochastic RSI — `signals/indicators/momentum.py`
- `calculate_stoch_rsi(df)` — applies Stochastic formula to RSI values, returns `(%K series, %D series)` in [0, 100]
- Standard parameters: RSI(14) → Stoch(14) → smooth K(3) → D(3)
- `StochRSIResult` dataclass: `k`, `d`, properties `overbought` (K > 80), `oversold` (K < 20), `bullish_cross`, `bearish_cross`
- `get_stoch_rsi(df)` — convenience wrapper returning current-bar dataclass
- Wired into aggregator: confidence boosted to 0.7 when %K crosses above %D (oversold) or below %D (overbought)

**Tests added:** 13 new test cases covering range validity, length consistency, dataclass integrity, cross detection, trend flags, and mutual-exclusivity invariants

---

### Session 3 — Stub Fixes: Backtesting Replay, Sector Map, Critic Prompt, Config Finalization
**Files modified:** `backtesting/replay.py`, `backtesting/engine.py`, `risk/portfolio_exposure.py`, `llm/prompts/spike_analysis.py`, `core/config.py`, `.env.example`

#### Backtesting Replay — `backtesting/replay.py` (stub fixed)
- Previous stub: exited every trade after exactly 1 bar (unrealistic, no actual hold period)
- New implementation: `_OpenPosition` dataclass tracks `entry_bar`, `entry_price`, `stop_loss`, `take_profit`, `entry_date`
- Realistic multi-bar position lifecycle:
  - **Exits (checked first):** bar low ≤ stop loss → exit at stop price; bar high ≥ take profit → exit at target price; SELL/STRONG_SELL signal fires → exit at bar close; end of test window → exit at last close
  - **Entries (when flat):** BUY/STRONG_BUY with no opposing SELL → enter at bar close; ATR-based stop (entry − ATR × STOP_LOSS_ATR_MULT) and target (entry + ATR × TAKE_PROFIT_ATR_MULT)
- One position at a time per ticker; growing history window prevents lookahead bias
- Trade records include: `entry_date`, `exit_date`, `bars_held`, `entry`, `stop_loss`, `take_profit`, `exit`, `exit_reason`, `raw_pnl`, `pnl`

#### Backtesting Engine — `backtesting/engine.py`
- `summary()` enhanced: added `avg_bars_held` and `exits_by_reason` (dict of stop_loss / take_profit / sell_signal / end_of_window counts)

#### Sector Exposure Map — `risk/portfolio_exposure.py` (stub fixed)
- Expanded from 5 hardcoded tickers to all 20 watchlist tickers across 8 sectors:
  - Technology: NVDA, MSFT, AAPL, PLTR
  - Financials: JPM, GS, V
  - Healthcare: LLY, UNH, ISRG
  - Energy: XOM, NEE
  - Consumer: AMZN, WMT, TSLA
  - Industrials: CAT, BA
  - Communication Services: META, GOOGL
  - Materials: FCX
- Sector concentration limit (max 3 per sector) and max concurrent trade limit now cover the full watchlist correctly

#### Critic Prompt — `llm/prompts/spike_analysis.py`
- `build_critic_prompt()` now demands strict JSON-only output (no prose, no markdown, no code fences)
- Calculates risk/reward ratio inline and embeds it in the prompt
- JSON schema: `approved` (bool), `confidence` (0.0–1.0), `biggest_risk`, `failure_scenario`, `reasoning`
- Explicit block rules: `approved=false` if R/R < 1.5, stop wider than 3× ATR, or signals conflict

#### Config and Watchlist — `core/config.py`, `.env.example`
- Watchlist finalized to 20 tickers: NVDA, MSFT, AAPL, PLTR, JPM, GS, V, LLY, UNH, ISRG, XOM, NEE, AMZN, WMT, TSLA, CAT, BA, META, GOOGL, FCX
- `AUTONOMOUS_MODE` config var removed (system is unconditionally autonomous)

**Stubs still outstanding after Session 3:**
- `risk/circuit_breakers.py` — portfolio value hardcoded at $100k; needs live Alpaca account value
- `agents/critic.py` — `_parse_response()` still uses naive keyword match; prompt now enforces JSON but parser does not consume it
- `llm/prompts/weekly_bias.py` — file exists but is empty
- `llm/prompts/monthly_bias.py` — file exists but is empty

---

### Session 13 — Real VaR from Actual Positions, Wired into validate_plan
**Files modified:** `risk/portfolio_exposure.py`, `agents/risk_manager.py`

#### VaR Engine — `risk/portfolio_exposure.py` (stub fixed)
- Previous stub: `current_var_estimate()` hardcoded `daily_vol = 0.02` for all positions, completely ignoring actual portfolio composition
- New implementation queries `get_open_trades()` and for each position:
  - Fetches last 60 days of daily candles via `get_candles(ticker, Timeframe.D1)`
  - Computes realized daily vol: `std(close.pct_change().tail(60))`
  - Uses last close as current market price (not stale entry price)
  - Position VaR = `quantity × current_price × realized_vol × z_score`
- Portfolio VaR = sum of all position VaRs (conservative, no correlation offset)
- Falls back to 2% vol only for individual positions where data is unavailable
- Returns `0.0` when no positions are open (correct — zero exposure)
- Lookup table for z-scores at 90/95/99% avoids scipy dependency for the common case
- New `check_var_limit(portfolio_value)` function: returns `(bool, str)` gate that blocks new trades when portfolio VaR exceeds `MAX_PORTFOLIO_RISK_PCT × portfolio_value`

#### Risk Manager Agent — `agents/risk_manager.py`
- `validate_plan()` now runs `check_var_limit()` as the second gate (after circuit breakers, before exposure limits)
- Gate order: circuit breaker → VaR budget → sector/concurrent exposure → position size cap

---

### Session 12 — Market Regime Label Integrated End-to-End
**Files modified:** `agents/orchestrator.py`, `agents/decision.py`, `agents/trade_plan.py`

#### Orchestrator — `agents/orchestrator.py`
- Extracts `regime_label` from `results["regime"].raw_data["regime"]` after Phase 1 completes
- Passes `regime=regime_label` kwarg to both `decision.run()` and `trade_plan.run()`
- Added hard entry gate: if regime is `BEAR_TREND` and decision is BUY/STRONG_BUY, the ticker is skipped before a trade plan is generated — no new long entries in a confirmed bear market
- Restored market hours gate (`_is_market_open()`) that was missing from the file: Alpaca clock primary, ET timezone fallback for Mon–Fri 09:30–16:00

#### Decision Agent — `agents/decision.py`
- Accepts `regime` kwarg; applied after weighted score computation, before signal-type conversion
- `_apply_regime_modifier(score, regime)` rules:
  - `BEAR_TREND` + positive score → ×0.5 (halve bullish conviction)
  - `BULL_TREND` + negative score → ×0.5 (halve bearish conviction)
  - `HIGH_VOLATILITY` → ×0.7 (reduce all conviction)
  - `BREAKOUT` + positive score → ×1.15, capped at 1.0 (amplify bullish breakout signals)
- Summary and `raw_data` now include the active regime label

#### Trade Plan Agent — `agents/trade_plan.py`
- Accepts `regime` kwarg
- In `HIGH_VOLATILITY` regime: `size_mult = 0.5` — position quantity halved before submitting
- `reasoning` field includes regime label and size multiplier for full traceability

---

### Session 11 — Polygon.io and Benzinga Feeds Built
**Files modified:** `data/feeds/price_feed.py`, `data/feeds/news_feed.py`

#### Price Feed — `data/feeds/price_feed.py`
- Added `_polygon_fetch_candles(ticker, timeframe)` using Polygon v2 aggregates REST API
- Full timeframe mapping: M1→minute/1, M5→minute/5, M15→minute/15, M30→minute/30, H1→hour/1, H4→hour/4, D1→day/1, W1→week/1, MN1→month/1 — native 4H bars (yfinance could not do this)
- Pagination via `next_url` for responses exceeding 50,000 bars
- Added `_polygon_fetch_snapshot(ticker)` for real-time quote via Polygon snapshot endpoint
- `fetch_candles` and `fetch_snapshot` now use Polygon when `POLYGON_API_KEY` is set, fall back to yfinance on empty response or missing key

#### News Feed — `data/feeds/news_feed.py`
- Added `_fetch_ticker_news_benzinga(ticker, from_dt, to_dt)` using Benzinga v2/news REST API
- Handles Benzinga's ISO 8601 timestamps and multi-ticker `stocks[]` array
- `_fetch_ticker_news` now dispatches: Benzinga (if key set and returns items) → Finnhub → empty list with warning if both missing
- Public API unchanged (`fetch_news`, `fetch_premarket_news`, `fetch_intraday_news`)

---

### Session 10 — All 5 News Layers Wired into NewsSentimentAgent
**Files created:** `news/layer4_weekly.py`, `news/layer5_monthly.py`
**Files modified:** `agents/news_sentiment.py`

#### Layer 4 — Weekly Bias (`news/layer4_weekly.py`)
- `run_weekly_analysis(ticker)` calls `build_weekly_bias_prompt` with:
  - `news_summary`: top 10 headlines from stored news (via `get_recent_news`)
  - `technical_summary`: computed live — price vs 20-day SMA, 20-day vs 50-day SMA cross
- LLM response parsed for keywords (accumulate/bullish/buy vs avoid/bearish/sell) → BUY/SELL/NEUTRAL signal at `W1` timeframe
- Runs in both INTRADAY and LONG_TERM modes (weekly context is useful regardless of horizon)

#### Layer 5 — Monthly Thesis (`news/layer5_monthly.py`)
- `run_monthly_analysis(ticker)` calls `build_monthly_bias_prompt` with:
  - `fundamental_summary`: live P/E, P/B, RevGrowth, D/E via `fetch_fundamentals`
  - `news_summary`: top 15 headlines from stored news
- LLM response parsed for stance keywords (overweight/undervalued vs underweight/overvalued) → BUY/SELL/NEUTRAL signal at `MN1` timeframe
- Runs in LONG_TERM mode only (fundamentals are not meaningful for intraday)

#### News Sentiment Agent — `agents/news_sentiment.py`
- All 5 layers now wired and called in order:
  - Layer 3 (spike): INTRADAY only
  - Layer 2 (pre-market daily): always
  - Layer 4 (weekly): always
  - Layer 1 (quarterly): LONG_TERM only — was defined but never called
  - Layer 5 (monthly): LONG_TERM only
- Summary string now reports active layers by mode

---

### Session 9 — Live Portfolio Value Refreshed Every Cycle
**Files modified:** `agents/orchestrator.py`

- `AlpacaBroker().get_account()["portfolio_value"]` called at the top of every `run_all()` cycle, after the circuit breaker check passes
- `self.portfolio_value` is updated in place — all 20 tickers in that cycle use the fresh value for position sizing (`TradePlanAgent`) and risk validation (`RiskManagerAgent.validate_plan`)
- If Alpaca is unreachable the last known value is retained with a warning log — no crash, no stale-silent drift
- Startup value in `run_paper.py` still used for the very first cycle before `run_all()` fires, which is acceptable

---

### Session 8 — Circuit Breaker Wired into Orchestrator
**Files modified:** `agents/orchestrator.py`

- Imported `check_circuit_breakers` from `risk/circuit_breakers`
- Called at the top of `run_all()` before any ticker cycle begins
- If triggered (daily loss ≥ 3% or consecutive loss streak ≥ 4), the full cycle is aborted and an empty dict is returned — no new signals, no new trades
- Circuit breaker fetches live Alpaca portfolio value on every call (fixed Session 5)
- Gap #1 from the outstanding stubs list is now closed

---

### Session 7 — End-of-Window Force-Close Removed from Backtesting
**Files modified:** `backtesting/replay.py`, `backtesting/engine.py`

#### Backtesting Replay — `backtesting/replay.py`
- Removed forced close at end of test window (`exit_reason: end_of_window`)
- Positions still open at window end are now recorded as `exit_reason: "open"` with `exit: None`, `pnl: None`, and a new `unrealized_pnl` field (last close minus entry price)
- No position is ever force-closed by a time or window boundary — exits only happen on SL, TP, or sell signal

#### Backtest Engine — `backtesting/engine.py`
- `summary()` now splits results into `closed` and `open_positions` before computing metrics
- All P&L metrics (win rate, total pnl, avg pnl, Sharpe, drawdown) computed on closed trades only — open positions do not distort realized stats
- Summary now includes `open_positions` count and `unrealized_pnl` sum as separate fields

---

### Session 6 — No End-of-Day Forced Square-Off
**Files modified:** `execution/broker/alpaca.py`, `progress.md`

#### Design Decision: Positions Carry Overnight — No Forced Close
- There is no hardcoded end-of-day square-off anywhere in the codebase (backtesting `end_of_window` exit fires only at the end of the entire test window, not at each day close)
- Limit and stop entry orders changed from `TimeInForce.DAY` → `TimeInForce.GTC` in `execution/broker/alpaca.py` — previously, unfilled limit/stop entry orders would be cancelled by Alpaca at market close; now they carry forward until filled or the position is closed by signal/SL/TP
- Market orders remain `TimeInForce.DAY` (they fill immediately; TIF is irrelevant)
- Intraday mode description updated: positions in intraday mode exit only on SL hit, TP hit, or SELL/STRONG_SELL signal — regardless of time or day boundary

---

### Session 5 — Critic JSON Parser Fixed, Live Alpaca Portfolio Value
**Files modified:** `agents/critic.py`, `risk/circuit_breakers.py`

#### Critic Parser — `agents/critic.py` (stub fixed)
- `_parse_response()` replaced with full `json.loads()` parser
- Strips markdown code fences (` ```json ``` `) before parsing — handles LLMs that wrap output
- Extracts `approved` (bool), `confidence` (float 0–1), `reasoning` (str) from structured JSON
- Falls back to keyword scan only if JSON is entirely absent or malformed — agent never crashes silently

#### Circuit Breakers — `risk/circuit_breakers.py` (stub fixed)
- Removed hardcoded `PORTFOLIO_VALUE = 100_000.0` constant
- Added `_get_portfolio_value()` — calls `AlpacaBroker().get_account()["portfolio_value"]` on every invocation
- Falls back to $100k with a warning log if Alpaca is unreachable or alpaca-py is not installed
- Daily loss threshold and percentage display now both use the live fetched value

---

### Session 4 — Approval Gate Removed, Circuit Breakers Wired, Critic Parser Fixed
**Files modified:** `agents/execution.py`, `agents/orchestrator.py`, `dashboard/app.py`, `core/config.py`, `.env.example`
**Files deleted:** `execution/approval_gate.py`, `dashboard/pages/approval_queue.py`

#### Design Decision: Approval Gate Permanently Removed
- System is fully autonomous for the entire paper trading phase and beyond unless explicitly re-added
- `execution/approval_gate.py` deleted entirely
- `dashboard/pages/approval_queue.py` deleted entirely
- `AUTONOMOUS_MODE` env var and config constant removed — no config flag needed when there is no gate

#### Execution Agent — `agents/execution.py`
- Removed `ApprovalGate` import and `self.gate` instance
- Trade plans now route directly to broker after Critic and Risk Manager pass
- DB status updated to FILLED on success, FAILED on broker exception

#### Orchestrator — `agents/orchestrator.py`
- Critic logic inverted: gate was previously require explicit `buy/strong_buy` approval; now Critic **blocks** only on `sell/strong_sell` — everything else proceeds
- This correctly models the Critic as a veto agent, not an approval agent

#### Dashboard — `dashboard/app.py`
- Approval Queue removed from navigation
- Caption updated to "Paper trading — autonomous mode"

#### Critic Prompt — `llm/prompts/spike_analysis.py` (continued from Session 3)
- Updated to enforce JSON-only output with explicit schema and block rules
- **Parser in `agents/critic.py` still needs to be updated** — see outstanding stubs below

**Stubs still outstanding after Session 4 (resolved in Session 5 where marked):**
- ~~`agents/critic.py` — `_parse_response()` still uses naive keyword match~~ — **Fixed Session 5**
- ~~`risk/circuit_breakers.py` — portfolio value still hardcoded at $100k~~ — **Fixed Session 5**
- `llm/prompts/weekly_bias.py` — empty
- `llm/prompts/monthly_bias.py` — empty
- Circuit breakers not wired into Orchestrator (function exists, never called)
- Orchestrator has no market hours gate or candle-close scheduling
- Polygon feed not built (`POLYGON_API_KEY` exists in config, zero usage in code)
- Benzinga feed not built (`BENZINGA_API_KEY` exists in config, zero usage in code)

---

## Core Principles

- Every expensive component is a swappable module. Cheap components now, SOTA later, nothing gets rebuilt.
- The system is fully autonomous. Every trade that passes the Critic and Risk Manager is submitted to the broker without human intervention. This is the permanent mode for paper trading.
- Features are normalized and relative across all stocks so the agent generalizes, never stock-specific raw values.
- The system operates in two modes: **intraday** and **long term**. A single mode flag flows through every agent.
- Lower timeframe signals that move against the higher timeframe bias are not automatically rejected. Counter-trend moves on smaller timeframes are valid and tradeable, but are flagged with lower confidence, tighter risk parameters, and reviewed more aggressively by the Critic Agent before execution.
- The LLM is only invoked when justified. Everything that can be done with fast math is done with fast math.
- Every decision, signal, and news item is stored permanently for backtesting.

---

## Signal Modes

### Intraday Mode
- Timeframe stack: 5 min → 15 min → 1 hour → 1 day
- Daily layer sets the intraday bias
- Decision Agent prioritizes sharp, quick gains with tight drawdown
- Trade plan includes: ATR-based stop loss and take profit; position held until SL hit, TP hit, or SELL/STRONG_SELL signal fires — no end-of-day forced close, positions carry overnight freely
- Position sizing: smaller, faster

### Long Term Mode
- Timeframe stack: 5 min → 15 min → 1 hour → 1 day → Weekly → Monthly
- Monthly and weekly layers set the structural and intermediate bias
- Daily layer serves dual role: top of intraday stack and bottom of long term stack
- Decision Agent prioritizes sustained, risk-adjusted returns over weeks and months
- Trade plan includes: wider stop, weeks-to-months target, trailing stop logic
- Position sizing: larger, held through noise

---

## Timeframe Responsibilities

| Timeframe | Role | LLM Involved |
|---|---|---|
| Monthly | Long term structural bias | Yes, once per month per stock |
| Weekly | Intermediate trend direction | Yes, once per week per stock |
| Daily | Short to medium term bias, intraday authority | Yes, once per day per stock |
| 1 hour | Defines trading zone within daily bias | No |
| 15 min | Confirms setup within zone, entry and exit points | No |
| 5 min | Precise entry and exit, spike detection | No |

---

## Five Layer News Architecture

### Layer 1 - Quarterly Narrative (Intraday and Long Term, Session 20)
- Fetches 90-day news, filters to earnings/analyst relevant articles before FinBERT scoring
- LLM produces a quarterly thesis on: earnings trend (beat/miss/guidance), analyst sentiment shift (upgrades/downgrades/PT changes), institutional tone (accumulation/distribution), and net quarterly stance
- Quarterly thesis stored in `stock_memory` — feeds directly into Layer 4 (weekly) and Layer 5 (monthly) LLM prompts
- Now runs in INTRADAY mode as well (previously LONG_TERM only) so fresh earnings/filing headlines are caught the moment they land, not only after a price/volume spike trips Layer 3. Open implementation detail: whether intraday Layer 1 runs the full LLM thesis or just the keyword+FinBERT filter stage

### Layer 2 - Pre-Market Deep Analysis
- Runs once before market open per stock per day
- Full LLM reasoning combining macro context, overnight news, and per-stock memory
- Sets the daily bias for both intraday and long term modes
- All output stored permanently

### Layer 4 - Weekly Directional Bias
- Runs once per cycle for all modes
- Combines stored news headlines + live SMA-based technical summary
- LLM produces a 3–4 sentence weekly stance (accumulate / hold / reduce / avoid)
- Emits a W1 timeframe signal into the Decision Agent

### Layer 5 - Monthly Investment Thesis (Long Term Only)
- Runs in LONG_TERM mode only
- Combines live fundamental snapshot (P/E, P/B, RevGrowth, D/E) + stored news headlines
- LLM produces a 4–5 sentence monthly thesis with valuation stance and catalyst calendar
- Emits a MN1 timeframe signal into the Decision Agent

### Layer 3 - Intraday Spike Detector and Event Driven Lookup
- Gate check on every cycle: fetches 5-min candles, checks price move > 1.5× ATR or volume > 2× 20-bar avg — returns immediately if neither condition met
- On gate trigger: FinBERT scores intraday headlines; articles with `abs(score) >= 0.5` escalate to LLM
- LLM response must start with BUY / SELL / HOLD — first word drives signal direction
- FinBERT score provides confidence magnitude; LLM HOLD + ambiguous FinBERT → article skipped (no signal)
- Event-driven: ~5–20 real triggers/day instead of 390 scheduled FinBERT+LLM calls

### News Storage (All Layers)
Every news item stored permanently with: raw headline, full article text, publication timestamp, agent consumption timestamp, FinBERT sentiment score, stock ticker, whether it triggered a bias revision, stock price at consumption time, layer of origin. Used for backtesting to replay exactly what the agent knew at any point in time — eliminates lookahead bias.

---

## Agent Roster

### 1. Technical Analysis Agent
- RSI, MACD, Bollinger Bands, VWAP (intraday only), ATR, OBV, volume spike
- EMA Stack (9/20/50/200), ADX/DMI, Stochastic RSI
- Chart patterns: H&S, double top/bottom, flag, pennant, triangle, range breakout
- Candlestick patterns: hammer, shooting star, engulfing, doji
- Two modes: intraday indicator set vs long term indicator set

### 2. News and Sentiment Agent
- Runs all five news layers: spike (intraday), pre-market daily, weekly bias, quarterly narrative, monthly thesis
- Layer routing: L3 spike = INTRADAY only; L2 daily + L4 weekly = always; L1 quarterly + L5 monthly = LONG_TERM only
- FinBERT sentiment scoring (local, no API cost)
- LLM escalation for all layers: L1 quarterly (earnings-focused prompt), L2 daily (LLM drives direction), L3 spike (LLM drives direction after market-event gate), L4 weekly, L5 monthly
- L1 quarterly thesis stored in `stock_memory` and fed into L4 weekly and L5 monthly LLM prompts
- L3 spike is event-driven: only activates on 5-min candle price move > 1.5× ATR or volume > 2× avg; no scheduled calls

### 3. Fundamental Agent
- Earnings, balance sheet, cash flow, revenue trajectory via SEC EDGAR
- Analyst consensus, insider activity, 13F filings
- Intraday mode: soft filter only. Long term mode: primary driver

### 4. Market Regime Agent
- Identifies current regime: BULL_TREND, BEAR_TREND, HIGH_VOLATILITY, LOW_VOLATILITY, RANGING, BREAKOUT, UNKNOWN
- Regime label extracted by Orchestrator and threaded into Decision Agent and Trade Plan Agent
- Decision Agent applies regime modifier to composite score (bear trend halves bullish conviction; high vol reduces all conviction by 30%; breakout amplifies bullish signals)
- Trade Plan Agent halves position size in HIGH_VOLATILITY regime
- Orchestrator hard-blocks new long entries when regime is BEAR_TREND

### 5. Memory Agent
- Per-stock JSON memory: `directional_bias`, `bias_confidence`, `key_levels`, `avoid_until`, `last_updated`, `bias_history`
- Read every cycle via `run()` → emits BUY/SELL at stored `bias_confidence`, STRONG_SELL if `avoid_until` is set
- Written via `update()` on trade close only (triggered by Orchestrator after reconciliation detects a filled exit)
- Two-tier write: Tier 1 overwrites current-state fields in place (`directional_bias`, `bias_confidence`, `key_levels`, `avoid_until`, `last_updated`); Tier 2 appends to `bias_history` only when `directional_bias` changes and `pnl` is known — avoids bloating JSON across 20 tickers × 2 modes running continuously
- All timestamps use `datetime.now(timezone.utc)` — no naive UTC timestamps anywhere

### 6. Risk Manager Agent
- ATR-based position sizing with Kelly sizing overlay (quarter-Kelly, activated when ≥5 closed trades exist in history; takes minimum of ATR and Kelly quantities)
- Portfolio-level exposure limits (concurrent-trade cap + per-lane duplicate check); sector concentration veto removed
- Drawdown circuit breakers (daily loss % and consecutive loss streak)
- Incremental VaR gate: candidate trade's own VaR added to existing portfolio VaR before approving
- Oversized positions are resized to risk-adjusted maximum in-place — never rejected for size alone
- `run()` (called without a plan, for dashboard surfacing) — checks circuit breakers, VaR status, open trade count; emits `SignalSource.RISK` signals on active breaches; `raw_data` carries `var_ok`, `var_reason`, `open_trades`
- `validate_plan()` gate order: circuit breaker → incremental VaR → per-lane exposure → size cap (resize-not-reject)
- `SignalSource.RISK` used for all signals emitted by this agent (not `REGIME`)

### 7. Decision Agent
- Weighted scoring across all agent signals; weights are runtime-dynamic (read from `agent_weights` DB table, 5-minute TTL cache) with hardcoded profile as fallback when table is empty
- `_weighted_score()`: normalizes by total weighted confidence so high-confidence signals carry more weight than low-confidence ones at the same source weight
- Intraday profile: Technical 35%, News 25%, Fundamental 20%, Memory 10%, Regime 10% (rebalanced Session 20)
- Long term profile: Fundamental 40%, Technical 20%, Regime 15%, News 15%, Memory 10% (rebalanced Session 20)
- Regime modifiers applied after weighted score: BEAR_TREND×0.5 on positive scores, BULL_TREND×0.5 on negative scores, HIGH_VOLATILITY×0.7 on all, BREAKOUT×1.15 (capped at 1.0) on positive scores
- Score thresholds: >0.5 → STRONG_BUY, >0.15 → BUY, <−0.5 → STRONG_SELL, <−0.15 → SELL, else NEUTRAL
- Weekly `scripts/update_weights.py` batch job updates `agent_weights` table with confidence-weighted accuracy scores from closed trade history (70% old / 30% new blend)

### 8. Trade Plan Agent
- Structures Decision Agent output into: entry price, stop loss, take profit, quantity, reasoning
- Intraday format: tight stop, same-day target
- Long term format: wider stop, multi-week target with trailing stop

### 9. Critic / Contrarian Agent
- Stress-tests the trade plan via LLM
- Returns structured JSON: `approved`, `confidence`, `biggest_risk`, `failure_scenario`, `reasoning`
- Blocks trade on SELL/STRONG_SELL signal output; empty signals list also blocks (fail-closed)
- Prompt enforces block rules: R/R < 1.5, stop > 3× ATR (concrete computed threshold, ATR from `plan.metadata`), or timeframe signal conflict
- Parser uses `json.loads()` with code-fence stripping; keyword fallback (`"approve"` or `"proceed"` only) if JSON absent
- `SignalSource.CRITIC` used for all emitted signals (not TECHNICAL)
- Timeframe label is mode-aware: `Timeframe.H1` for intraday, `Timeframe.D1` for long-term
- LLM call wrapped in try/except; fail-open fallback (approved=true, confidence=0.5) when LLM unavailable
- Confidence defaults: 0.75 for approved, 0.70 for rejected — consistent across JSON and keyword fallback paths
- **Pending:** Critic reasoning (`biggest_risk`, `failure_scenario`) not yet persisted to DB — `critic_log` table design locked, implementation pending
- **Pending:** Supporting summary still flat string — structured per-timeframe signal block design locked, implementation pending

### 10. Execution Agent
- Routes approved plans directly to Alpaca broker (market, limit, stop order types)
- No human gate — fully autonomous
- DB status updated on FILLED or FAILED
- Graceful stub fallback if alpaca-py not installed

### 11. Orchestrator
- Coordinates all agents in correct pipeline order
- Carries the mode flag (intraday / long term) through the full pipeline
- Runs `run_all()` across all 20 watchlist tickers in parallel via `ThreadPoolExecutor(max_workers=10)`
- Per-ticker exception isolation: one ticker failure skips that ticker and logs full traceback; remaining 19 continue
- NEUTRAL signals explicitly gated before Phase 4 alongside None signals
- Critic gate is fail-closed: empty signals list blocks the trade

---

## Decision Pipeline (Per Trade Signal)

```
All agent signals consolidated
  └── Decision Agent (intraday or long term scoring profile)
        └── Trade Plan Agent (entry, stop, target, size)
              └── Critic Agent (LLM stress-test — blocks on SELL/STRONG_SELL)
                    └── Risk Manager (exposure, sizing, circuit breakers)
                          └── Execution Agent (routes to Alpaca broker)
                                └── Outcome logged for performance review
```

---

## Dashboard Pages

| Page | Status |
|---|---|
| Live Trading | Reads Alpaca positions and account value; displays open trades from DB |
| Agent Reasoning | Exists — reads signal store; renders signal trace per agent |
| Performance | Exists — Sharpe, drawdown, win rate from decision store |
| Spike Log | Exists — displays Layer 3 spike events |

**Removed:** Approval Queue page (approval gate deleted permanently)

---

## Watchlist (20 Tickers, Finalized)

| Sector | Tickers |
|---|---|
| Technology | NVDA, MSFT, AAPL, PLTR |
| Financials | JPM, GS, V |
| Healthcare | LLY, UNH, ISRG |
| Energy | XOM, NEE |
| Consumer | AMZN, WMT, TSLA |
| Industrials | CAT, BA |
| Communication Services | META, GOOGL |
| Materials | FCX |

---

## Technology Stack

| Component | Current | Future |
|---|---|---|
| LLM brain | Ollama + Llama 3.1 8B local | Claude Sonnet / Opus API |
| Price data | Polygon.io REST (yfinance fallback) | Polygon WebSocket real-time stream |
| Fundamentals | SEC EDGAR API (free) | Refinitiv or Bloomberg |
| News | Benzinga REST (Finnhub fallback) | Benzinga WebSocket real-time |
| Sentiment NLP | FinBERT local | Fine-tuned financial model |
| Compute hosting | Railway or Render free tier | Dedicated cloud instance |
| Broker API | Alpaca paper trading | Interactive Brokers |
| Dashboard | Streamlit | React |
| Memory store | SQLite | PostgreSQL |

Every swap is a single config change. Architecture stays identical.

---

## API Keys Configured

| API | Key In .env | Code Consuming It |
|---|---|---|
| Alpaca | Yes | `execution/broker/alpaca.py`, `risk/circuit_breakers.py`, `agents/orchestrator.py` |
| Finnhub | Yes | `data/feeds/news_feed.py` (fallback when Benzinga key absent) |
| Polygon.io | Yes | `data/feeds/price_feed.py` (primary candle + snapshot source) |
| Benzinga | Yes | `data/feeds/news_feed.py` (primary news source, Finnhub fallback) |
| Anthropic Claude | Yes (empty) | `llm/client.py` (fallback to Ollama if empty) |
| SEC EDGAR | No key needed | `data/feeds/fundamentals_feed.py` |
| FinBERT | No key needed | `news/finbert.py` (local HuggingFace model) |
| Ollama (local) | No key — local install | `llm/client.py` (active default backend since `ANTHROPIC_API_KEY` is empty). Installed and `llama3` model pulled Session 33; verified end-to-end via `query_llm()`. |

---

## Consolidated Open Bug List — Session 26 Final Verification

**Full audit completed Session 26.** All 13 items verified against live code. Findings below.

1. ~~**Decision weight feedback loop**~~ — **Verified fully implemented.** `agent_weights` table in `db.py`, `signal_snapshot` migration, `execution.py` serializes signals to JSON at insert time, `decision.py` reads DB weights with hardcoded fallback, `scripts/update_weights.py` complete with confidence-weighted accuracy and 70/30 blend. No code change needed.
2. ~~**Dual-mode concurrent execution / position keying**~~ — **Verified fully implemented.** `strategy_mode` column migration in `db.py`, `get_filled_trades()` and `get_open_trades()` filter by lane, per-lane duplicate check in `risk/portfolio_exposure.py`, orchestrator reconciliation uses `exit-{id}-{mode}` client order IDs. No code change needed.
3. **Live end-to-end paper trading test** — not a code bug; not yet run.
4. ~~**Bollinger Band + ADX sigmoid scoring**~~ — **Verified already implemented.** `bb_buy_confidence`, `bb_sell_confidence`, `adx_trend_confidence` in `signals/indicators/scoring.py`; both wired in `timeframe_aggregator.py`. No code change needed.
5. ~~**Regression channel breakout detection**~~ — **Verified already implemented.** `detect_regression_channel_breakout` in `signals/patterns/chart_patterns.py` (full pivot detection, R²-gated OLS, parallel constraint, sigmoid confidence); wired in `timeframe_aggregator.py` lines 192–200. No code change needed.
6. ~~**`LOW_VOLATILITY` / `BREAKOUT` regime states unreachable**~~ — **Verified reachable.** `BREAKOUT` fires when `current > prior_20_high` (checked before `BULL_TREND`); `LOW_VOLATILITY` fires when `vix < 15 AND realized_vol < 0.12`. BREAKOUT amplifier in `decision.py` is live code. No code change needed.
7. ~~**`paper=True` hardcoded**~~ — **Verified already config-driven.** `PAPER_TRADING = os.getenv("PAPER_TRADING", "true") == "true"` in `core/config.py`; used in `alpaca.py` and `orchestrator.py`. No code change needed.
8. ~~**Timezone inconsistency**~~ — **Verified no inconsistency.** `_utcnow()` in `core/models.py` uses `datetime.now(timezone.utc)`; `memory_agent.py` also uses `datetime.now(timezone.utc)`. No code change needed.
9. ~~**News Layer 3 tagging bug**~~ — **Verified already fixed.** Early-return path sets `item.layer = 3` before `save_news()` (line 68). No code change needed.
10. ~~**`detect_doji` dead-code import**~~ — **Verified already wired.** Called in `timeframe_aggregator.py`, emits NEUTRAL at 0.5 confidence. No code change needed.
11. ~~**EDGAR dead code**~~ — **Verified no EDGAR code exists.** `_get_cik()` and related functions were already removed. `data/feeds/fundamentals_feed.py` uses yfinance only with TTL cache. No code change needed.
12. ~~**Unused fundamental fields**~~ — **Fixed Session 26.** Added sigmoid scoring for P/B (`_pb_buy/sell_confidence`, centers 2.0/5.0), debt-to-equity (`_de_buy/sell_confidence`, centers 0.3/1.5), and earnings growth (`_earn_growth_buy/sell_confidence`, centers 15%/−5%) in `agents/fundamental.py`. All three now emit signals using the same floor-based pattern as P/E and revenue growth. FCF and market cap left stored-but-unscored (hard to score in isolation without size normalization).
13. **Production infra deployment and continuous feedback/regime adaptation** — not started at all.
14. ~~**No SL/TP price monitoring**~~ — **Fixed Session 27.** Added `check_sl_tp_exits()` to `Orchestrator`: fetches all filled buy trades for the mode, gets current price from candle cache (M15/D1 by mode), checks against stored `stop_loss`/`take_profit`, calls `_flat_position()` → `ExecutionAgent` on breach. Market hours guard at top. Called every intraday cycle via `run_all()`; called every 300s for long-term via `last_longterm_sl_check` timer in `run_paper.py`.
15. ~~**No signal-reversal exit**~~ — **Fixed Session 27.** Reversal block added in `run_ticker()` between Phase 3 and Phase 4: calls `get_open_filled_entry_trade(ticker, mode)`, detects opposing direction (open-long + SELL signal or open-short + BUY signal), checks `has_pending_exit()` to avoid duplicates, calls `_flat_position()` → `ExecutionAgent`, then returns — no new entry that cycle.
16. ~~**Double-submission risk on SL/TP exits**~~ — **Fixed Session 27.** Added `has_pending_exit(entry_trade_id, strategy_mode)` to `decision_store.py`: queries for a non-terminal SELL row with `client_order_id = f"exit-{entry_trade_id}-{strategy_mode}"`. Called inside `check_sl_tp_exits()` and the reversal block before every `_flat_position()` call.
17. ~~**Mode-specific ATR multipliers missing**~~ — **Fixed Session 27.** Four new env-configurable constants in `core/config.py`: `STOP_LOSS_ATR_MULT_INTRADAY=1.5`, `TAKE_PROFIT_ATR_MULT_INTRADAY=2.5`, `STOP_LOSS_ATR_MULT_LONGTERM=2.5`, `TAKE_PROFIT_ATR_MULT_LONGTERM=5.0`. `TradePlanAgent.run()` picks the correct pair by `self.mode`. Old global constants (`STOP_LOSS_ATR_MULT`, `TAKE_PROFIT_ATR_MULT`) retained — still used by `backtesting/replay.py`.

**Net: 10 of 13 original items verified already resolved in code; 1 fixed Session 26 (item 12); 2 are not code bugs (items 3 and 13). 4 new gaps from Session 27 audit (items 14-17) — all fixed Session 27.**

---

### Consolidated Open Bug List — Session 28 Critic Agent Audit

**Full code-level audit of `agents/critic.py`, `agents/orchestrator.py`, `llm/prompts/spike_analysis.py`. 9 bugs fixed and verified. 3 design gaps locked pending Claude Code.**

18. ~~**Type mismatch on `plan` — Critic or Execution always broken**~~ — **Fixed Session 28.** `trade_plan.py` now stores TradePlan object in `raw_data["plan"]`. `TradePlan(**plan)` dict-unpacking removed from `orchestrator.py`. Both Critic prompt builder and Execution path now receive a proper TradePlan object.
19. ~~**No exception handling — one crash kills all 20 tickers**~~ — **Fixed Session 28.** try/except added around `query_llm()` in `critic.py` (fail-open fallback: approved=true, confidence=0.5). Sequential for loop in `run_all()` replaced with `ThreadPoolExecutor(max_workers=10)` — per-ticker try/except via `future.result()` isolates failures. One ticker crash no longer halts remaining 19.
20. ~~**Sequential 20-ticker loop — cycles stack past 60s window**~~ — **Fixed Session 28.** `run_all()` now runs all 20 tickers in parallel via `ThreadPoolExecutor(max_workers=10)`. SQLite safe: WAL mode + per-call connections confirmed. `self.portfolio_value` is read-only during parallel section — no race condition.
21. ~~**ATR block rule in prompt but ATR never provided**~~ — **Fixed Session 28.** `trade_plan.py` stores `metadata={"atr": round(atr, 4)}` on every TradePlan. `build_critic_prompt` reads `plan.metadata.get("atr")`, adds `ATR (14)` as dedicated prompt field, replaces vague "3x ATR" rule with concrete computed threshold. Graceful fallback when ATR is None.
22. ~~**`SignalSource.TECHNICAL` on Critic signal corrupts weight feedback loop**~~ — **Fixed Session 28.** `CRITIC = "critic"` added to `SignalSource` enum in `core/constants.py`. `agents/critic.py` now uses `SignalSource.CRITIC`.
23. ~~**Fail-open when Critic returns empty signals list**~~ — **Fixed Session 28.** `critic_blocked` now defaults `True` when `critic_signal is None`. `logger.warning` fires so the bypass is always visible.
24. ~~**Default confidence inverted — rejections defaulted higher than approvals**~~ — **Fixed Session 28.** Both JSON parse path and keyword fallback path in `_parse_response` now use `0.75 if approved else 0.70`.
25. ~~**"valid" keyword in fallback scan causes false approvals**~~ — **Fixed Session 28.** `"valid" in lower` removed from keyword scan. Only `"approve"` and `"proceed"` remain.
26. ~~**R/R formula inconsistent fallbacks for same field**~~ — **Fixed Session 28.** Both sides of R/R formula in `build_critic_prompt` now use `entry_price or 0`.
27. ~~**NEUTRAL signal not gated before Phase 4**~~ — **Fixed Session 28.** Gate in `run_ticker()` now reads `if decision_signal is None or decision_signal.signal_type == SignalType.NEUTRAL` — both exit before Phase 4 with an auditable log entry.
28. ~~**`Timeframe.D1` hardcoded regardless of mode**~~ — **Fixed Session 28 (manual one-line fix).** Critic now emits `Timeframe.H1` for intraday, `Timeframe.D1` for long-term. Note: H1 is a representative label, not per-signal precision — per-timeframe precision deferred to Gap 3 implementation.
29. **Critic reasoning never stored — blocked trades invisible** — **Design locked Session 28. Pending Claude Code.** Option A selected: new `critic_log` table. Captures every verdict (approved and blocked) with `biggest_risk`, `failure_scenario`, `reasoning`, plan details, and `trade_id` FK when executed. See Session 28 design block above.
30. **Conflict rule unevaluable + no regime in Critic prompt** — **Design locked Session 28. Pending Claude Code.** Option A selected: structured per-timeframe signal block replaces flat 500-char summary. Regime label included as dedicated field — closes both gaps simultaneously. See Session 28 design block above.
31. **No HOLD as a first-class decision** — **Partially locked Session 28. HOLD logging pending Claude Code. Add (20%) and Trim (30%) deferred.** Add deferred: multiple open BUY rows break reconciliation. Trim deferred: `client_order_id` collision with `has_pending_exit()` + stale quantity after trim. HOLD logging design locked: new `hold_decisions` table, position review block in `run_ticker()` before Phase 4. See Session 28 design block above.

**Net: 11 bugs fixed Session 28 (items 18-28). 3 design gaps locked pending implementation (items 29-31). Add and Trim explicitly deferred pending reconciliation and partial-order infrastructure rework.**

---

### Consolidated Open Bug List: Session 30 Orchestrator Audit

**Full code-level audit of `agents/orchestrator.py`. 6 bugs fixed and verified against returned file.**

32. ~~**`check_sl_tp_exits()` used stale candle price**~~ **Fixed Session 30.** Replaced `get_candles()` candle close with `fetch_snapshot(ticker).price` for live price. try/except wraps the fetch; failure logs warning and skips the trade without crashing the loop.
33. ~~**Falsy check on `stop_loss` silently skipped trades with `stop_loss == 0.0`**~~ **Fixed Session 30.** Guard changed to `if stop_loss is None or take_profit is None or not qty:`.
34. ~~**`_is_market_open()` fallback had no NYSE holiday awareness**~~ **Fixed Session 30.** `_nyse_holidays(year)` added using `USFederalHolidayCalendar` plus Good Friday via O'Beirne algorithm. Fallback returns `False` on NYSE holidays.
35. ~~**No timeout on `future.result()` meant a hanging thread blocked all of `run_all()`**~~ **Fixed Session 30.** `future.result(timeout=45)` with dedicated `TimeoutError` handler per ticker.
36. ~~**Phase 1 agents sequential inside `run_ticker()` creating a 10-15s bottleneck**~~ **Fixed Session 30.** Inner `ThreadPoolExecutor(max_workers=5)` runs all 5 Phase 1 agents in parallel. 30s per-agent timeout with empty `AgentOutput` fallback on failure.
37. ~~**`close_trade()` unguarded inside reconciliation loop meant partial failure was silent**~~ **Fixed Session 30.** Wrapped in try/except with `logger.exception` and `continue`. One DB failure no longer aborts reconciliation of remaining trades.

---

### Consolidated Open Bug List: Session 31 Short Selling Feature

**Short selling enabled. All bugs fixed Session 31. Thread safety audit completed Session 31.**

38. ~~**`_estimate_pnl()` wrong sign for short positions**~~ **Fixed Session 31.** Negates result when `trade["side"] == "sell"`.
39. ~~**`check_sl_tp_exits()` breach direction wrong for short positions**~~ **Fixed Session 31.** Gates `sl_breach` and `tp_breach` on `trade.get("side", "buy")`. Short: SL fires on `current_price >= stop_loss`, TP fires on `current_price <= take_profit`.
40. ~~**`signal_is_hold` falls through to Phase 4 when False with open position**~~ **Fixed Session 31.** Hold evaluates direction relative to open position side. `logger.error` plus early return added as safety net for any unmatched fall-through.
41. ~~**`_flat_position()` hardcoded `SELL` side breaks short position close**~~ **Fixed Session 31.** Closing side opposes open side: `BUY` to close a short, `SELL` to close a long.
42. ~~**TradePlanAgent SL/TP direction and order side wrong for short entries**~~ **Verified Session 31 — already correct.** `trade_plan.py` already calculated `stop_loss = entry + dist` and `take_profit = entry - dist` for SELL signals. No code change needed.
43. ~~**No ETB pre-flight check before short entry**~~ **Fixed Session 31.** `is_etb(ticker) -> bool` added to `AlpacaBroker` using Assets endpoint. Short entry blocked if `shortable == False` or `easy_to_borrow == False`. ETB gate placed in orchestrator before Phase 4. Fails closed on any API error.
44. ~~**`get_open_filled_entry_trade()` side-agnostic query unverified**~~ **Fixed Session 31.** Removed `AND side='buy'` from `get_open_filled_entry_trade()`, `get_filled_trades()`, `has_pending_exit()`, and `get_closed_trades_for_weights()`. Now filter on `client_order_id LIKE 'entry-%'` to distinguish entry vs exit orders regardless of side. `execution.py` updated to use presence of open position (not order side) to choose entry vs exit COID.
45. ~~**Thread safety of shared agent instances unverified**~~ **Fixed Session 31 (Stub 26).** All 10 agent `run()` methods audited. 9 agents fully stateless. `DecisionAgent`: module-level `_weight_cache` write was unguarded — fixed by adding `threading.Lock` around the write in `_load_weights()`.

---

### Consolidated Open Bug List: Session 32 Pre-Flight Health Check

~~46. **No pre-flight health check before paper trading** **Design locked Session 32. Pending Claude Code.** `scripts/health_check.py` to be created. Three tiers: environment, connectivity, agent instantiation. Zero side effects enforced. Collect-all-then-report. Exits with code 1 on any FAIL. WARN-only exits with code 0. Standalone script, not integrated into `run_paper.py`.~~ — **Fixed Session 32.** `scripts/health_check.py` created with all three tiers. Expanded Tier 2 with Polygon, Benzinga, paper trading flag, and LLM backend checks. `AlpacaBroker.__init__()` hardened with `except Exception` so Tier 3 never crashes on missing keys. `.env` created from `.env.example`.

---

### Consolidated Open Bug List: Session 33 First Live Paper Trading Run

**Found via live execution of `run_paper.py`, not static audit. 3 real bugs fixed. 1 cosmetic item suppressed per explicit user decision, not fixed.**

47. ~~**yfinance `401 Invalid Crumb` errors, permanent once triggered**~~ **Fixed Session 33.** Root cause was two-fold: (a) `fundamentals_feed.py` called yfinance directly, unlocked, racing candle fetches from the same `ThreadPoolExecutor` pool; (b) yfinance caches its crumb in-process with no recovery once Yahoo invalidates it under load. Fixed with a shared `_yf_lock` in `price_feed.py` serializing all yfinance calls (including routing `fundamentals_feed.py` through it), plus a `_reset_yf_crumb()` retry-once-on-empty helper. Upgrading yfinance alone (`pip install -U yfinance` → 1.5.1) did not fix this.
48. ~~**Benzinga news requests failing with `JSONDecodeError`**~~ **Fixed Session 33.** Benzinga defaults to an XML response body; `resp.json()` was called unconditionally. Fixed by sending `Accept: application/json` (content negotiation via header, not a query param — `format=json` param does nothing).
49. ~~**Ollama connection refused — local LLM backend not installed**~~ **Fixed Session 33 (environment, not code).** Ollama installed, `llama3` pulled (resumed cleanly after a stalled download partway through), verified end-to-end via `llm.client.query_llm()` returning a real response.
50. **Pandas `FutureWarning` spam across ~20 call sites** (`float(single_element_series)` deprecated) in `signals/patterns/candlestick.py`, `signals/timeframe_aggregator.py`, `agents/market_regime.py` — **explicitly deferred, not fixed, per user decision Session 33.** Cosmetic only; suppressed via scoped `warnings.filterwarnings()` in `run_paper.py` rather than rewriting call sites to `.iloc[0]`.

**Net: 3 of 4 items from the first live run fixed Session 33 (items 47-49); item 50 is a known, accepted, suppressed cosmetic warning — not a bug to revisit unless pandas actually removes the fallback.**

---

### Consolidated Open Bug List: Session 34 Third Live Run Log Analysis + Implementation

**5 bugs identified from log analysis, 4 design decisions locked. Pre-implementation gate for the WebSocket layer (Decision 3, Layer 2) failed — Finnhub plan does not support `subscribe-news`. Implemented the design's own documented REST+cache fallback instead, plus everything else as locked. One new bug found during live verification, not in original scope.**

51. ~~**`OLLAMA_MODEL` config default still `"llama3"` — causes 60s ReadTimeout on every LLM call (Critical).**~~ **Verified already fixed (Session 33).** `core/config.py` already read `"llama3.2:3b"` — this had already been corrected in Session 33's Ollama setup work before Session 34 started. No change needed.

52. ~~**News agent timeout for all tickers — parallel Finnhub hammering causes connection hang (Critical).**~~ **Fixed Session 34.** `scripts/test_finnhub_ws.py` (created this session) confirmed the WebSocket fix (Decision 3, Layer 2) is not available on the current Finnhub plan — `subscribe-news` rejected with `"You don't have access to this resource"`. Implemented the design's documented interim fix instead: `threading.Semaphore(2)` + 5-minute TTL cache added to `data/feeds/news_feed.py`, keyed by `(ticker, hours_back)` at the `_fetch_ticker_news()` dispatcher level so both Benzinga and Finnhub paths benefit.

53. ~~**FinBERT loading weights from non-merged PR branch `refs/pr/29` — fragile (Moderate).**~~ **Verified Session 34 — no change needed.** `transformers` is already at 5.13.0 (past the 4.47.0 floor). FinBERT was already confirmed in Session 33 to return real non-zero sentiment scores (0.78 on clearly bullish text). Still loads from `refs/pr/29` (confirmed again via live logs this session) — fragile but functional; left as-is since forcing a different loading path risks breaking a working setup.

54. ~~**Benzinga returning nothing for most tickers — `BENZINGA_API_KEY` likely absent or not activated (Low).**~~ **Verified Session 34 — not a bug.** Key is present and working (Session 33's `Accept: application/json` fix). Live logs this session showed genuine empty-result "falling back to Finnhub" warnings for specific tickers/windows, not a broken key. No change needed.

55. **Alpaca broker initialized 4 times per startup (Cosmetic).** Four separate agent constructors each create their own `AlpacaBroker` instance at startup. No functional impact. **Still deferred** — a broker singleton is not worth the refactor until needed for another reason.

56. ~~**Remove `Timeframe.M1` from TechnicalAgent (Decision 1).**~~ **Fixed Session 34.** `core/constants.py` — `INTRADAY_TIMEFRAMES` changed from `[M1, M5, M15, H1]` to `[M5, M15, H1]`.

57. ~~**Split orchestrator cycle frequency: 60s maintenance vs 5-min full pipeline (Decision 2).**~~ **Fixed Session 34.** `agents/orchestrator.py` — new `run_maintenance()` method (SL/TP + reconciliation only) extracted alongside `run_all()` via shared `_refresh_portfolio_and_reconcile()` helper. `core/config.py` — new `INTRADAY_CYCLE_SECS=60`/`PIPELINE_CYCLE_SECS=300` constants. `scripts/run_paper.py` — main loop calls `run_maintenance()` every cycle and `run_all()` every `PIPELINE_CYCLE_SECS` via a `last_pipeline_run` timer.

58. **Three-layer event-driven news architecture (Decision 3).** **Partially implemented Session 34 — Layer 2 (WebSocket) blocked by Finnhub plan, substituted with REST+cache (see item 52). Layer 1 (pre-market batch) implemented: new `news/news_ingest.py::run_premarket_news_fetch()`, wired into `run_paper.py` startup, gated on ET time < 9:20am matching the design's skip-if-late logic. Layer 3 (SQLite-only reads) not implemented as a hard requirement — see Session 34 Implementation Log for the reasoning (the WebSocket feed was the only thing that would have made SQLite-only reads an improvement over the now-cached REST path).**

59. ~~**Persist news across sessions via `news_events` SQLite table (Decision 4).**~~ **Fixed Session 34.** `data/storage/db.py` — `news_events` table added with the exact columns specified, plus `UNIQUE(ticker, headline, published_at)` for idempotent re-fetches. `data/storage/news_store.py` — `save_news_event()`/`get_recent_news_events()` added. `news/layer2_premarket.py` and `news/layer3_spike.py` now persist every FinBERT-scored article to `news_events` at zero extra cost (reusing scores they already compute).

60. **Ollama concurrency — new bug found during Session 34 live verification, not in original scope.** Once Bug 52 (Finnhub hammering) was fixed, live reruns still showed Phase-1 `news`/`regime` timeouts — now caused by Ollama itself. `news/layer2_premarket.py::run_premarket_analysis()` calls `query_llm()` unconditionally for every ticker every cycle; up to 10 concurrent calls queue behind Ollama's single serialized model instance and tail requests exceed both Ollama's 60s timeout and the orchestrator's 30s Phase-1 timeout. **Mitigated, not fully resolved Session 34:** `llm/client.py` — `threading.Semaphore(2)` added around the Ollama POST call, same pattern as item 52's fix. Queuing is now orderly but total Ollama compute time is unchanged — a local 3B CPU model serving 10 tickers within a 30s window is a genuine throughput ceiling. Timeouts were still observed in the final verification run. **Open for a future session:** raise the Phase-1 timeout, reduce how many tickers call the LLM per cycle, or switch to a faster backend (Claude API via `ANTHROPIC_API_KEY`, or GPU-accelerated Ollama).

**Net: 5 of 6 original Session 34 bugs resolved or verified-not-applicable (items 51, 53, 54 were already fine; item 52 fixed via the documented fallback; item 55 still deferred as designed). All 4 design decisions addressed (items 56-59) — 3 fully implemented, 1 (Decision 3) partially implemented with the WebSocket sub-piece substituted per user decision. 1 new bug found during verification (item 60), mitigated but not fully resolved — genuine hardware throughput ceiling, not a code defect.**

---

## Outstanding Stubs (Prioritized)

| Priority | File | Issue |
|---|---|---|
| ~~**1**~~ | ~~`agents/critic.py`~~ | ~~`_parse_response()` uses naive keyword match~~ — **Fixed Session 5** |
| ~~**2**~~ | ~~`risk/circuit_breakers.py`~~ | ~~`PORTFOLIO_VALUE` hardcoded at $100k~~ — **Fixed Session 5** |
| ~~**1**~~ | ~~`agents/orchestrator.py`~~ | ~~Circuit breaker check never called~~ — **Fixed Session 8** |
| ~~**4**~~ | ~~`llm/prompts/weekly_bias.py`~~ | ~~File exists but never called~~ — **Fixed Session 10** |
| ~~**5**~~ | ~~`llm/prompts/monthly_bias.py`~~ | ~~File exists but never called~~ — **Fixed Session 10** |
| ~~**6**~~ | ~~`agents/orchestrator.py`~~ | ~~No market hours gate~~ — **Fixed Session 12** |
| ~~**7**~~ | ~~`data/feeds/price_feed.py`~~ | ~~Polygon.io feed not built~~ — **Fixed Session 11** |
| ~~**8**~~ | ~~`data/feeds/news_feed.py`~~ | ~~Benzinga feed not built~~ — **Fixed Session 11** |
| ~~**9**~~ | ~~`risk/portfolio_exposure.py`~~ | ~~VaR uses flat 2% vol assumption~~ — **Fixed Session 13** |
| ~~**10**~~ | ~~`agents/orchestrator.py`~~ | ~~Regime label never read back by downstream agents~~ — **Fixed Session 12** |
| ~~**11**~~ | ~~`agents/decision.py`, `scripts/update_weights.py`, `data/storage/db.py`~~ | ~~Decision weight tuning~~ — **Implemented Session 26.** `agent_weights` table, `signal_snapshot` attribution, DB-backed `_load_weights()` with 5-min TTL, `update_weights.py` weekly batch. |
| **12** | `data/feeds/price_feed.py`, `data/feeds/fundamentals_feed.py`, `data/feeds/news_feed.py`, `scripts/run_paper.py` | ~~Live end-to-end paper trading test not yet run.~~ — **First live run completed Session 33.** Found and fixed 3 real infra bugs (yfinance crumb, Benzinga XML/JSON, Ollama not installed) that static audit had not caught. Multi-cycle stability under live market data not yet observed — one clean cycle confirmed, longer unattended runs still pending. See Consolidated Open Bug List Session 33 block. |
| **13** | `run_paper.py`, `agents/orchestrator.py`, `data/storage/db.py`, `agents/risk_manager.py`, `backtesting/replay.py` | Dual mode concurrent execution — design locked (Session 15), not yet coded. Position keying still ticker-only, not `(ticker, strategy_mode)`. See Consolidated Open Bug List item #2. |
| ~~**16 (27A)**~~ | ~~`data/storage/decision_store.py`, `agents/orchestrator.py`, `scripts/run_paper.py`~~ | ~~Exit infrastructure~~ — **Fixed Session 27.** `has_pending_exit()` added to `decision_store.py`. `check_sl_tp_exits()` added to `Orchestrator` (market hours guard, candle-cache price check, delegates to `ExecutionAgent` via `_flat_position()`). Called every intraday cycle in `run_all()`; 300s timer for long-term in `run_paper.py`. Post-fix: `_flat_position()` corrected to call `self.execution.run()` instead of bypassing `route_order()`. |
| ~~**17 (27B)**~~ | ~~`agents/orchestrator.py`, `agents/trade_plan.py`, `core/config.py`~~ | ~~Signal-reversal exit + ATR mode differentiation~~ — **Fixed Session 27.** Reversal block in `run_ticker()` before Phase 4. Four mode-specific ATR constants in `core/config.py`; `trade_plan.py` picks by mode. Post-fix: `TAKE_PROFIT_ATR_MULT_INTRADAY` corrected from 3.0 to design-locked 2.5. |
| ~~**15**~~ | ~~`agents/orchestrator.py`~~ | ~~Orchestrator fundamental gating bug~~ — **verified already resolved, Session 25.** `fundamental.run()` is unconditional; real blocking bug was a `TradingMode` import error in `run_paper.py`, also fixed. |
| ~~**14**~~ | ~~`agents/orchestrator.py`, `agents/memory.py`~~ | ~~Memory Agent write-back~~ — **Fixed Session 22** |
| **18 (28A)** | `data/storage/db.py`, `data/storage/decision_store.py`, `agents/critic.py`, `agents/orchestrator.py` | Critic reasoning not stored — `critic_log` table and `save_critic_verdict()` — **Design locked Session 28. Pending Claude Code.** |
| **19 (28B)** | `agents/orchestrator.py`, `llm/prompts/spike_analysis.py` | Structured per-timeframe signal block for Critic prompt + regime label — **Design locked Session 28. Pending Claude Code.** |
| **20 (28C)** | `data/storage/db.py`, `data/storage/decision_store.py`, `agents/orchestrator.py` | HOLD decision logging to DB — **Design locked Session 28. Pending Claude Code.** Add (20%) and Trim (30%) explicitly deferred. |
| **21 (30)** | `agents/orchestrator.py` | Orchestrator 6-bug audit — **Fixed Session 30.** Live snapshot price in SL/TP check, None guard on stop_loss, NYSE holiday calendar, future.result timeout, Phase 1 parallelism, close_trade exception handling. |
| ~~**22 (31A)**~~ | ~~`agents/orchestrator.py`~~ | ~~Short selling: L1 PnL sign flip, L2 SL/TP breach direction, L3 signal_is_hold fall-through, `_flat_position()` closing side.~~ — **Fixed Session 31.** |
| ~~**23 (31B)**~~ | ~~`agents/trade_plan.py`~~ | ~~Short selling: SL/TP direction and order side for short entries.~~ — **Verified Session 31 — already correct. No change needed.** |
| ~~**24 (31C)**~~ | ~~`execution/broker/alpaca.py`~~ | ~~Short selling: `is_etb(ticker)` ETB pre-flight check.~~ — **Fixed Session 31.** |
| ~~**25 (31D)**~~ | ~~`data/storage/decision_store.py`~~ | ~~Verify `get_open_filled_entry_trade()` is side-agnostic.~~ — **Fixed Session 31.** Side filter replaced with `client_order_id LIKE 'entry-%'` across all four affected queries. `execution.py` COID logic also corrected. |
| ~~**26**~~ | ~~`agents/decision.py`~~ | ~~Thread safety verification: confirm all `run()` methods are stateless.~~ — **Fixed Session 31.** 9 agents verified safe. `DecisionAgent` `_weight_cache` write protected with `threading.Lock`. |
| ~~**27 (32)**~~ | ~~`scripts/health_check.py`~~ | ~~Pre-flight health check script. **Design locked Session 32. Pending Claude Code.** Three-tier check: environment, connectivity, agent instantiation. Zero side effects. Collect-all-then-report. FAIL exits code 1.~~ — **Implemented Session 32.** Script created with all three tiers. Expanded Tier 2 (Polygon, Benzinga, paper flag, LLM backend). `alpaca.py` broker init hardened. `.env` created. |
| ~~**28 (34A)**~~ | ~~`core/config.py`, `.env`~~ | ~~`OLLAMA_MODEL` default still `"llama3"` — causes 60s timeout on every LLM call.~~ — **Verified Session 34: already fixed in Session 33.** No change needed. |
| ~~**29 (34B)**~~ | ~~`core/constants.py`~~ | ~~Remove `Timeframe.M1` from TechnicalAgent timeframe list.~~ — **Fixed Session 34.** `INTRADAY_TIMEFRAMES` changed to `[M5, M15, H1]`. |
| ~~**30 (34C)**~~ | ~~`run_paper.py`, `agents/orchestrator.py`, `core/config.py`~~ | ~~Split cycle frequency: 60s maintenance loop + 5-min full pipeline loop.~~ — **Fixed Session 34.** `run_maintenance()` added to `Orchestrator`; `INTRADAY_CYCLE_SECS`/`PIPELINE_CYCLE_SECS` added to `core/config.py`; `run_paper.py` main loop split accordingly. |
| **31 (34D)** | `data/feeds/news_feed.py`, `data/storage/db.py`, `data/storage/news_store.py`, `news/news_ingest.py`, `news/layer2_premarket.py`, `news/layer3_spike.py` | Three-layer event-driven news architecture — **Partially implemented Session 34.** Pre-implementation gate (`scripts/test_finnhub_ws.py`) failed: `subscribe-news` not available on the current Finnhub plan. Layer 2 (WebSocket) substituted with the design's own documented REST + `threading.Semaphore(2)` + 5-min TTL cache fallback (implemented). Layer 1 (pre-market batch → `news_events`) implemented. Layer 4 (Decision 4, cross-session persistence via `news_events`) implemented. Layer 3 (hard SQLite-only reads in `NewsSentimentAgent`) not implemented as a literal requirement — its rationale depended on the now-unavailable WebSocket feed; the shared cache achieves the same practical outcome (≤1 REST hit per ticker per 5 min) without the rewrite risk. Revisit if the Finnhub plan is upgraded. |
| ~~**32 (34E)**~~ | ~~`transformers`, `torch`~~ | ~~FinBERT loading from PR branch `refs/pr/29` — fragile.~~ — **Verified Session 34: already resolved.** `transformers` already at 5.13.0 (past the 4.47.0 floor); FinBERT already confirmed returning non-zero scores in Session 33. Still loads from the PR branch (fragile but functional) — left as-is. |
| **33 (34F)** | `llm/client.py`, `news/layer2_premarket.py` | Ollama concurrency — new bug found during Session 34 live verification, not in original scope. Up to 10 tickers call `query_llm()` concurrently every cycle; Ollama serializes on one local model instance, causing Phase-1 timeouts. **Mitigated Session 34** with `threading.Semaphore(2)` in `llm/client.py` (same pattern as the Finnhub fix), but a genuine CPU throughput ceiling remains — timeouts still observed in the final verification run. Open for a future session: raise Phase-1 timeout, reduce per-cycle LLM calls, or switch to a faster backend. |

---

## Latency Profile

| Layer | Latency | Acceptable |
|---|---|---|
| Price and volume feed | Milliseconds | Yes |
| Technical signal calculation | Instant | Yes |
| Risk calculations | Microseconds | Yes |
| Decision Agent scoring | Milliseconds | Yes |
| FinBERT sentiment scoring | 1–2 seconds | Yes |
| LLM reasoning (local Llama) | 3–15 seconds | Yes — only runs at daily layer |
| LLM reasoning (Claude API) | 1–3 seconds | Yes |
| Finnhub news | Real-time, seconds | Yes |

System is designed for swing and positional trading. Not optimized for HFT. Edge exists in analysis quality, not execution speed.

---

## Build Steps Status

| Step | Status |
|---|---|
| 1. Define scope: instruments, markets, stock list | Done — 20 tickers, stocks only, long-only to start |
| 2. Data infrastructure: price, fundamentals, macro | Done (yfinance + EDGAR + Finnhub) |
| 3. Signal engine: indicators, chart patterns, candlesticks | Done |
| 4. Five-layer news and sentiment architecture | Done (all 5 layers wired; daily/weekly/monthly/quarterly/spike) |
| 5. Persistent memory layer per stock | Done |
| 6. Multi-agent architecture with orchestrator | Done (regime label flows end-to-end; market hours gate active) |
| 7. Risk management layer | Done (incremental VaR; circuit breakers; Kelly sizing from trade history; resize-not-reject; run() visibility; sector veto removed) |
| 8. Decision Agent: weighted rule-based scoring | Done |
| 9. Decision feedback loop and outcome tracking | Done — `signal_snapshot` attribution at trade insert; `agent_weights` DB table; `scripts/update_weights.py` weekly batch; `_load_weights()` with DB read + hardcoded fallback |
| 10. Backtesting framework | Done (replay realistic, engine walk-forward, cost model) |
| 11. Execution layer with broker API | Done (Alpaca, fully autonomous) |
| 12. Dashboard and observability | Done (4 pages live; depth of each page not fully verified) |
| 13. Paper trade with live data end to end | In progress — first live run executed Session 33, 3 infra bugs found and fixed (yfinance crumb, Benzinga XML/JSON, Ollama setup); sustained multi-cycle/multi-day stability not yet observed |
| 14. Production infrastructure deployment | Not started |
| 15. Continuous feedback and regime adaptation | Not started |

---

## Open Decisions

- ~~Long only confirmed for paper trading phase. Short selling deferred.~~ **Short selling enabled, Session 31.**
- Stocks only confirmed. Options deferred.
- Alpaca confirmed as paper trading broker.
- Add-to-position (20% on STRONG_BUY) permanently deferred: reconciliation rework required.
- Trim (30% on SELL) permanently deferred: `client_order_id` collision and `current_quantity` column gap.
- ~~ETB check placement: block at TradePlan stage vs Execution stage.~~ **Resolved Session 31: placed in `orchestrator.py` before Phase 4, before `run_ticker()` enters TradePlan.**
- Whether and when to introduce a reinforcement learning policy in place of the rule-based Decision Agent.
- Cloud provider for production deployment.
- Dual mode (intraday + long term running concurrently per ticker) — design confirmed Session 15, coding pending via Claude Code.
