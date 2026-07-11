"""
Pre-flight health check — run manually before starting paper trading.

Exits with code 0 if all checks pass (warnings allowed).
Exits with code 1 if any FAIL is recorded.

Zero side effects: no DB writes, no memory writes, no signal storage.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ANTHROPIC_API_KEY, FINNHUB_API_KEY,
    DB_PATH, PAPER_TRADING, TradingMode, OLLAMA_BASE_URL,
)
from core.constants import Timeframe

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

results: list[tuple[str, str, str]] = []  # (level, tier_label, message)


def record(level: str, tier: str, msg: str) -> None:
    results.append((level, tier, msg))


# ── Tier 1: Environment ────────────────────────────────────────────────────────

def check_packages() -> None:
    packages = ["alpaca", "yfinance", "anthropic", "transformers", "pandas", "numpy", "finnhub"]
    for pkg in packages:
        try:
            __import__(pkg)
            record(PASS, "Tier 1", f"{pkg} importable")
        except ImportError as exc:
            record(FAIL, "Tier 1", f"{pkg} import failed: {exc}")


def check_env_vars() -> None:
    required = {
        "ALPACA_API_KEY":    ALPACA_API_KEY,
        "ALPACA_SECRET_KEY": ALPACA_SECRET_KEY,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        record(FAIL, "Tier 1", f"missing env vars: {', '.join(missing)}")
    else:
        record(PASS, "Tier 1", "Alpaca credentials present")


def check_db_tables() -> None:
    required_tables = {
        "trade_decisions", "agent_weights", "critic_log",
        "hold_decisions", "stock_memory",
    }
    try:
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        conn.close()
        existing = {r[0] for r in rows}
        missing = required_tables - existing
        if missing:
            record(FAIL, "Tier 1", f"DB tables missing: {', '.join(sorted(missing))}")
        else:
            record(PASS, "Tier 1", f"all {len(required_tables)} DB tables exist")
    except Exception as exc:
        record(FAIL, "Tier 1", f"DB open failed: {exc}")


def check_agent_imports() -> None:
    agent_modules = [
        ("agents.technical",      "TechnicalAgent"),
        ("agents.news_sentiment", "NewsSentimentAgent"),
        ("agents.fundamental",    "FundamentalAgent"),
        ("agents.market_regime",  "MarketRegimeAgent"),
        ("agents.memory_agent",   "MemoryAgent"),
        ("agents.risk_manager",   "RiskManagerAgent"),
        ("agents.decision",       "DecisionAgent"),
        ("agents.trade_plan",     "TradePlanAgent"),
        ("agents.critic",         "CriticAgent"),
        ("agents.execution",      "ExecutionAgent"),
    ]
    failed = []
    for module_path, class_name in agent_modules:
        try:
            mod = __import__(module_path, fromlist=[class_name])
            getattr(mod, class_name)
        except Exception as exc:
            failed.append(f"{class_name}: {exc}")
    if failed:
        for msg in failed:
            record(FAIL, "Tier 1", f"agent import failed — {msg}")
    else:
        record(PASS, "Tier 1", "all 10 agent classes importable")


# ── Tier 2: Connectivity ───────────────────────────────────────────────────────

def check_alpaca_account() -> None:
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_TRADING)
        acct = client.get_account()
        pv = float(acct.portfolio_value)
        record(PASS, "Tier 2", f"Alpaca connected — portfolio ${pv:,.2f}")
    except Exception as exc:
        record(FAIL, "Tier 2", f"Alpaca get_account() failed: {exc}")


def check_alpaca_clock() -> None:
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_TRADING)
        clock = client.get_clock()
        status = "OPEN" if clock.is_open else "CLOSED"
        record(PASS, "Tier 2", f"market {status} (next open: {clock.next_open})")
    except Exception as exc:
        record(FAIL, "Tier 2", f"Alpaca get_clock() failed: {exc}")


def check_price_feed() -> None:
    try:
        from data.cache.market_data_cache import get_candles
        df = get_candles("AAPL", Timeframe.D1)
        if df is None or df.empty:
            record(FAIL, "Tier 2", "AAPL D1 price feed returned empty DataFrame")
        else:
            record(PASS, "Tier 2", f"AAPL D1 price feed OK ({len(df)} bars)")
    except Exception as exc:
        record(FAIL, "Tier 2", f"price feed get_candles() failed: {exc}")


def check_llm_key() -> None:
    if ANTHROPIC_API_KEY:
        record(PASS, "Tier 2", "LLM: ANTHROPIC_API_KEY present — will use Claude API")
    else:
        record(PASS, "Tier 2", f"LLM: ANTHROPIC_API_KEY not set — will use Ollama at {OLLAMA_BASE_URL}")

def check_finnhub_key() -> None:
    if FINNHUB_API_KEY:
        record(PASS, "Tier 2", "FINNHUB_API_KEY present")
    else:
        record(WARN, "Tier 2", "FINNHUB_API_KEY missing — news feed will fall back to yfinance")

def check_polygon_key() -> None:
    from core.config import POLYGON_API_KEY
    if POLYGON_API_KEY:
        record(PASS, "Tier 2", "POLYGON_API_KEY present")
    else:
        record(WARN, "Tier 2", "POLYGON_API_KEY not set — price feed will use yfinance")

def check_benzinga_key() -> None:
    from core.config import BENZINGA_API_KEY
    if BENZINGA_API_KEY:
        record(PASS, "Tier 2", "BENZINGA_API_KEY present")
    else:
        record(WARN, "Tier 2", "BENZINGA_API_KEY not set — news feed will fall back to Finnhub")
# ── Tier 3: Agent instantiation ────────────────────────────────────────────────

def check_agent_instantiation() -> None:
    agent_classes = [
        ("agents.technical",      "TechnicalAgent"),
        ("agents.news_sentiment", "NewsSentimentAgent"),
        ("agents.fundamental",    "FundamentalAgent"),
        ("agents.market_regime",  "MarketRegimeAgent"),
        ("agents.memory_agent",   "MemoryAgent"),
        ("agents.risk_manager",   "RiskManagerAgent"),
        ("agents.decision",       "DecisionAgent"),
        ("agents.trade_plan",     "TradePlanAgent"),
        ("agents.critic",         "CriticAgent"),
        ("agents.execution",      "ExecutionAgent"),
    ]
    failed = []
    for module_path, class_name in agent_classes:
        for mode in (TradingMode.INTRADAY, TradingMode.LONG_TERM):
            try:
                mod = __import__(module_path, fromlist=[class_name])
                cls = getattr(mod, class_name)
                cls(mode)
            except Exception as exc:
                failed.append(f"{class_name}({mode.value}): {exc}")
    if failed:
        for msg in failed:
            record(FAIL, "Tier 3", f"instantiation failed — {msg}")
    else:
        record(PASS, "Tier 3", "all 10 agents instantiated (INTRADAY + LONG_TERM)")

def check_paper_trading_flag() -> None:
    if PAPER_TRADING:
        record(PASS, "Tier 2", "PAPER_TRADING is True — safe to run")
    else:
        record(WARN, "Tier 2", "PAPER_TRADING is False — system will place LIVE orders with real money")
# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Running pre-flight health check...\n")

    check_packages()
    check_env_vars()
    check_db_tables()
    check_agent_imports()

    check_alpaca_account()
    check_alpaca_clock()
    check_price_feed()
    check_llm_key()
    check_finnhub_key()

    check_agent_instantiation()

    print()
    for level, tier, msg in results:
        print(f"[{level}] {tier}: {msg}")

    warns  = sum(1 for r in results if r[0] == WARN)
    fails  = sum(1 for r in results if r[0] == FAIL)

    print(f"\nResult: {warns} WARNING{'S' if warns != 1 else ''}, {fails} FAILURE{'S' if fails != 1 else ''}")
    if fails:
        print("Fix the above failures before starting paper trading.")
        sys.exit(1)
    else:
        print("System is ready. You may start run_paper.py.")
        sys.exit(0)


if __name__ == "__main__":
    main()
