from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from core.constants import SignalType, SignalSource, Timeframe, TradeStatus, OrderSide, OrderType


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Signal:
    ticker: str
    source: SignalSource
    signal_type: SignalType
    timeframe: Timeframe
    confidence: float               # 0.0 – 1.0
    value: float | None = None      # e.g. RSI value, sentiment score
    reasoning: str = ""
    timestamp: datetime = field(default_factory=_utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TradePlan:
    ticker: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    entry_price: float | None       # None for market orders
    stop_loss: float
    take_profit: float
    confidence: float
    reasoning: str
    strategy_mode: str = "intraday"  # "intraday" or "long_term" — identifies which lane owns this position
    client_order_id: str | None = None  # set by ExecutionAgent after DB row is created
    status: TradeStatus = TradeStatus.PENDING
    created_at: datetime = field(default_factory=_utcnow)
    filled_at: datetime | None = None
    filled_price: float | None = None
    pnl: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentOutput:
    agent_name: str
    ticker: str
    signals: list[Signal]
    summary: str
    raw_data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=_utcnow)


@dataclass
class MarketSnapshot:
    ticker: str
    price: float
    volume: float
    bid: float | None
    ask: float | None
    timestamp: datetime = field(default_factory=_utcnow)


@dataclass
class NewsItem:
    headline: str
    source: str
    url: str
    published_at: datetime
    tickers: list[str]
    sentiment_score: float | None = None   # -1.0 to 1.0 from FinBERT
    layer: int = 2                         # 1=quarterly, 2=premarket, 3=intraday
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FundamentalData:
    ticker: str
    pe_ratio: float | None
    pb_ratio: float | None
    revenue_growth: float | None
    earnings_growth: float | None
    debt_to_equity: float | None
    free_cash_flow: float | None
    market_cap: float | None
    sector: str = ""
    as_of: datetime = field(default_factory=_utcnow)
