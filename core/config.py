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

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
