import os
from enum import Enum
from dotenv import load_dotenv

load_dotenv()


class TradingMode(str, Enum):
    INTRADAY = "intraday"
    LONG_TERM = "long_term"


# ── Mode ──────────────────────────────────────────────────────────────────────
TRADING_MODE: TradingMode = TradingMode(os.getenv("TRADING_MODE", "intraday"))

# ── Watchlist ─────────────────────────────────────────────────────────────────
WATCHLIST: list[str] = os.getenv(
    "WATCHLIST",
    "NVDA,MSFT,AAPL,PLTR,JPM,GS,V,LLY,UNH,ISRG,XOM,NEE,AMZN,WMT,TSLA,CAT,BA,META,GOOGL,FCX",
).split(",")

# ── API Keys ──────────────────────────────────────────────────────────────────
ALPACA_API_KEY     = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY  = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL    = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

FINNHUB_API_KEY    = os.getenv("FINNHUB_API_KEY", "")
POLYGON_API_KEY    = os.getenv("POLYGON_API_KEY", "")
BENZINGA_API_KEY   = os.getenv("BENZINGA_API_KEY", "")

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
OLLAMA_BASE_URL    = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "trading_agent.db")

# ── Risk Parameters ───────────────────────────────────────────────────────────
MAX_POSITION_SIZE_PCT   = float(os.getenv("MAX_POSITION_SIZE_PCT", "0.05"))   # 5% of portfolio
MAX_PORTFOLIO_RISK_PCT  = float(os.getenv("MAX_PORTFOLIO_RISK_PCT", "0.02"))  # 2% daily drawdown limit
MAX_CONCURRENT_TRADES   = int(os.getenv("MAX_CONCURRENT_TRADES", "20"))
STOP_LOSS_ATR_MULT      = float(os.getenv("STOP_LOSS_ATR_MULT", "2.0"))
TAKE_PROFIT_ATR_MULT    = float(os.getenv("TAKE_PROFIT_ATR_MULT", "4.0"))

# Mode-differentiated ATR multipliers — intraday uses tighter stops to exit quickly;
# long-term uses wider stops to avoid being shaken out by daily noise.
STOP_LOSS_ATR_MULT_INTRADAY   = float(os.getenv("STOP_LOSS_ATR_MULT_INTRADAY",   "1.5"))
TAKE_PROFIT_ATR_MULT_INTRADAY = float(os.getenv("TAKE_PROFIT_ATR_MULT_INTRADAY", "2.5"))
STOP_LOSS_ATR_MULT_LONGTERM   = float(os.getenv("STOP_LOSS_ATR_MULT_LONGTERM",   "2.5"))
TAKE_PROFIT_ATR_MULT_LONGTERM = float(os.getenv("TAKE_PROFIT_ATR_MULT_LONGTERM", "5.0"))

# ── Execution ─────────────────────────────────────────────────────────────────
PAPER_TRADING                  = os.getenv("PAPER_TRADING", "true").lower() == "true"
LONGTERM_LOOP_INTERVAL_SECONDS = int(os.getenv("LONGTERM_LOOP_INTERVAL_SECONDS", "1800"))

# Session 34: cycle frequency split. The 60s loop only checks SL/TP breaches and
# reconciles closed trades (both depend on live price, not candle data). The full
# 11-agent pipeline (signals, LLM, execution) runs every 5 minutes instead of every
# 60s, since 1-minute candles were removed as noise and 5-minute is the new floor.
INTRADAY_CYCLE_SECS = int(os.getenv("INTRADAY_CYCLE_SECS", "60"))
PIPELINE_CYCLE_SECS = int(os.getenv("PIPELINE_CYCLE_SECS", "300"))

# Session 39: Phase 1 (per-agent) and outer (per-ticker) timeouts inside
# run_ticker()/run_all(). Originally 30s/45s, sized around the old assumption
# that a long run_all() would also delay the next SL/TP check. Now that SL/TP
# monitoring runs on its own thread (item 68) independent of run_all()'s
# duration, these can absorb more Ollama queueing latency (Critic + Layer 3/4
# bursts) before giving up on a ticker, instead of discarding partial work
# under load that would likely have finished given more headroom.
PHASE1_AGENT_TIMEOUT_SECONDS = int(os.getenv("PHASE1_AGENT_TIMEOUT_SECONDS", "100"))
TICKER_TIMEOUT_SECONDS = int(os.getenv("TICKER_TIMEOUT_SECONDS", "150"))

# Session 51 item (51B): premarket-only override of the two constants above.
# Session 51 removed llm/client.py's non-blocking Ollama queue limiter, so
# every LLM call now genuinely waits its turn on the single Ollama slot
# instead of being rejected past 3-deep — measured worst case in a real
# premarket run was ~125s (102.6s wait + 22.8s call), comfortably under this
# 240s premarket-only Phase 1 budget (~2x headroom over the observed worst
# case). Live's PHASE1_AGENT_TIMEOUT_SECONDS (100s) is intentionally left
# unchanged pending its own live-log verification — premarket has no hard
# time budget (30-minute ceiling, current runs ~10-14 minutes) so absorbing
# more queue wait there is a fine tradeoff; live's per-cycle budget is not
# yet confirmed to have the same slack. PREMARKET_TICKER_TIMEOUT_SECONDS is
# raised in lockstep with the same +50s margin already used for phases 2-4
# (risk/decision/memory) in the live pair above — otherwise the outer bound
# would abandon a premarket ticker's future before its own Phase 1 timeout
# ever got a chance to fire.
PREMARKET_PHASE1_TIMEOUT_SECONDS = int(os.getenv("PREMARKET_PHASE1_TIMEOUT_SECONDS", "240"))
PREMARKET_TICKER_TIMEOUT_SECONDS = int(os.getenv("PREMARKET_TICKER_TIMEOUT_SECONDS", "290"))

# Session 39 (item 67 mitigation), unified Session 39 addendum: single shared
# timeout for every query_llm() call in the app — live-cycle (Critic, Layer 3,
# Layer 4) and the pre-market batch (Layers 1/2/5) alike. Originally two
# separate constants (a "live" one and a tighter 25s pre-market one) plus a
# scattered `timeout: int = 60` default hardcoded independently in
# llm/client.py, all three run_*_analysis() functions, and news_ingest.py's
# _classify_horizon()/ingest_news_item() — six-plus places that all had to
# independently agree on the same number. Collapsed to one constant:
# - future.result(timeout=...) does not kill the underlying thread on
#   timeout, so a thread can keep holding llm/client.py's _ollama_semaphore
#   slot well past when its caller gave up on it. An explicit request timeout
#   bounds how long that orphaned hold can last — not a true cancellation,
#   but the practical mitigation until one is built.
# - Raised from the original 20s: a trivial prompt was already observed
#   taking ~19s end-to-end on this hardware, meaning the old value was timing
#   out essentially every real call before Ollama finished — real prompts
#   (Critic's JSON reasoning, Layer 3/4's bias prompts, the pre-market
#   batch's Layer 1/2/5 prompts) run both a prompt_eval and an eval pass and
#   will consistently run longer than a trivial one. The pre-market batch's
#   old 25s cap was never re-examined against this same finding and was
#   almost certainly equally undersized. 90s gives real headroom instead of a
#   marginal bump, at the cost of PHASE1_AGENT_TIMEOUT_SECONDS (100s) having
#   very little margin left if a Phase 1 agent needs more than one Ollama
#   call in the same cycle, and of the pre-market batch's worst-case per-
#   ticker time budget (previously estimated at ~80s/ticker assuming a 25s
#   cap) needing to be re-derived against the new 90s cap.
LLM_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT_SECONDS", "90"))

# ── Session 45: Technical Agent hybrid LLM escalation ─────────────────────────
# Deterministic zone boundaries for RSI / Stoch RSI. Outside these bands the
# reading is unambiguous and stays fully rule-based; inside them an LLM call
# replaces what used to be silence (RSI/Stoch RSI) or a flat NEUTRAL default
# (ADX) with a genuine buy/sell/hold verdict for that one indicator.
RSI_LLM_ZONE_LOW        = float(os.getenv("RSI_LLM_ZONE_LOW", "30.0"))
RSI_LLM_ZONE_HIGH       = float(os.getenv("RSI_LLM_ZONE_HIGH", "70.0"))
STOCH_RSI_LLM_ZONE_LOW  = float(os.getenv("STOCH_RSI_LLM_ZONE_LOW", "25.0"))
STOCH_RSI_LLM_ZONE_HIGH = float(os.getenv("STOCH_RSI_LLM_ZONE_HIGH", "75.0"))

# Bollinger %B (percent bandwidth: 0 = at lower band, 1 = at upper band) zone
# split. The locked design specifies "lower zone / middle / upper zone" but
# no numeric split — these two values are an implementation choice, not a
# locked number, and should be revisited if that turns out to be wrong.
BB_LLM_ZONE_LOW_PCT_B   = float(os.getenv("BB_LLM_ZONE_LOW_PCT_B", "0.2"))
BB_LLM_ZONE_HIGH_PCT_B  = float(os.getenv("BB_LLM_ZONE_HIGH_PCT_B", "0.8"))

# Delta-based cache: an LLM call only fires if the indicator's raw value has
# moved more than this many points since its last evaluation for that
# ticker/timeframe/indicator; otherwise the cached prior verdict is reused
# (see data/storage/llm_indicator_cache_store.py). RSI/Stoch RSI = literal
# 5-point threshold, locked Session 45 (both true 0-100 scales). ADX and
# Bollinger %B thresholds are NOT locked — Session 45 explicitly left their
# normalized equivalents undecided. Provisional values below exist so caching
# isn't skipped entirely for those two; revisit once a real number is chosen.
RSI_LLM_DELTA_THRESHOLD       = float(os.getenv("RSI_LLM_DELTA_THRESHOLD", "5.0"))
STOCH_RSI_LLM_DELTA_THRESHOLD = float(os.getenv("STOCH_RSI_LLM_DELTA_THRESHOLD", "5.0"))
ADX_LLM_DELTA_THRESHOLD       = float(os.getenv("ADX_LLM_DELTA_THRESHOLD", "3.0"))
BB_LLM_DELTA_THRESHOLD_PCT_B  = float(os.getenv("BB_LLM_DELTA_THRESHOLD_PCT_B", "0.05"))

# Fixed confidence assigned to a signal produced from an LLM indicator
# verdict — same pattern as MACD's fixed 0.65, since the LLM returns a
# direction, not a magnitude, for these ambiguous-zone reads.
LLM_INDICATOR_VOTE_CONFIDENCE = float(os.getenv("LLM_INDICATOR_VOTE_CONFIDENCE", "0.55"))

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
