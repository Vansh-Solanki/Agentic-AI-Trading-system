COMMISSION_PER_SHARE = 0.005   # $0.005 per share (Alpaca is $0 but model conservatively)
SLIPPAGE_BPS        = 5        # 5 basis points slippage


def apply_costs(raw_pnl: float, entry_price: float, quantity: float = 1.0) -> float:
    """Deduct slippage and commission from a raw P&L figure."""
    slippage   = entry_price * (SLIPPAGE_BPS / 10_000) * 2  # entry + exit
    commission = COMMISSION_PER_SHARE * quantity * 2
    return raw_pnl - slippage - commission


def estimate_market_impact(order_size: float, avg_volume: float) -> float:
    """
    Simplified square-root market impact model.
    Returns the estimated slippage in price units.
    """
    if avg_volume == 0:
        return 0.0
    participation_rate = order_size / avg_volume
    return 0.1 * (participation_rate ** 0.5)
