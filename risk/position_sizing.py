import core.config as cfg


def get_historical_performance(ticker: str | None = None) -> tuple[float, float, float]:
    """
    Returns (win_rate, avg_win_dollars, avg_loss_dollars) from closed trade history.
    Returns (0, 0, 0) when fewer than 5 closed trades exist — not enough to trust Kelly.
    """
    from data.storage.decision_store import get_trade_history
    trades = get_trade_history(ticker=ticker, limit=100)
    closed = [t for t in trades if t.get("status") == "closed" and t.get("pnl") is not None]
    if len(closed) < 5:
        return 0.0, 0.0, 0.0
    wins   = [t["pnl"] for t in closed if t["pnl"] > 0]
    losses = [abs(t["pnl"]) for t in closed if t["pnl"] <= 0]
    win_rate = len(wins) / len(closed)
    avg_win  = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    return win_rate, avg_win, avg_loss


def calculate_position_size(
    portfolio_value: float,
    entry_price: float,
    stop_loss: float,
    risk_pct: float | None = None,
) -> float:
    """
    Fixed-fractional position sizing.
    Returns the number of shares to buy/sell.
    """
    if entry_price <= 0 or stop_loss <= 0:
        return 0.0

    risk_pct      = risk_pct or cfg.MAX_PORTFOLIO_RISK_PCT
    risk_amount   = portfolio_value * risk_pct
    risk_per_share = abs(entry_price - stop_loss)

    if risk_per_share == 0:
        return 0.0

    shares = risk_amount / risk_per_share

    # Cap at max position size
    max_shares = (portfolio_value * cfg.MAX_POSITION_SIZE_PCT) / entry_price
    return min(shares, max_shares)


def kelly_position_size(
    portfolio_value: float,
    entry_price: float,
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    fraction: float = 0.25,
) -> float:
    """Half-Kelly (default fraction=0.25) position size."""
    if avg_loss == 0:
        return 0.0
    b = avg_win / avg_loss
    kelly = (b * win_rate - (1 - win_rate)) / b
    kelly = max(kelly, 0.0) * fraction
    dollars = portfolio_value * kelly
    return dollars / entry_price if entry_price > 0 else 0.0
