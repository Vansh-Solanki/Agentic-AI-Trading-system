from enum import Enum


class Timeframe(str, Enum):
    M1   = "1m"
    M5   = "5m"
    M15  = "15m"
    M30  = "30m"
    H1   = "1h"
    H4   = "4h"
    D1   = "1d"
    W1   = "1wk"
    MN1  = "1mo"


class RegimeType(str, Enum):
    BULL_TREND     = "bull_trend"
    BEAR_TREND     = "bear_trend"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY  = "low_volatility"
    RANGING         = "ranging"
    BREAKOUT        = "breakout"
    UNKNOWN         = "unknown"


class SignalType(str, Enum):
    STRONG_BUY   = "strong_buy"
    BUY          = "buy"
    NEUTRAL      = "neutral"
    SELL         = "sell"
    STRONG_SELL  = "strong_sell"


class SignalSource(str, Enum):
    TECHNICAL   = "technical"
    NEWS        = "news"
    FUNDAMENTAL = "fundamental"
    REGIME      = "regime"
    MEMORY      = "memory"
    RISK        = "risk"
    CRITIC      = "critic"


class OrderSide(str, Enum):
    BUY  = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT  = "limit"
    STOP   = "stop"
    STOP_LIMIT = "stop_limit"


class TradeStatus(str, Enum):
    PENDING   = "pending"
    APPROVED  = "approved"
    REJECTED  = "rejected"
    SUBMITTED = "submitted"
    FILLED    = "filled"
    CLOSED    = "closed"
    CANCELLED = "cancelled"
    FAILED    = "failed"


# Intraday active timeframes.
# Session 34: M1 removed — 1-minute candles at a 60s cycle produced identical
# signals on consecutive runs and only amplified noise. 5-minute is the new floor.
INTRADAY_TIMEFRAMES  = [Timeframe.M5, Timeframe.M15, Timeframe.H1]

# Long-term active timeframes
LONGTERM_TIMEFRAMES  = [Timeframe.H4, Timeframe.D1, Timeframe.W1, Timeframe.MN1]

# Minimum candles needed before indicators are reliable
MIN_CANDLES_REQUIRED = 50
