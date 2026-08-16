import logging
from datetime import datetime, timedelta, timezone
from core.models import FundamentalData
from data.storage.db import get_connection

logger = logging.getLogger(__name__)


def save_fundamentals_cache(data: FundamentalData) -> None:
    """Persist a FundamentalData snapshot, replacing any existing row for the ticker."""
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO fundamentals_cache
               (ticker, pe_ratio, pb_ratio, revenue_growth, earnings_growth,
                debt_to_equity, free_cash_flow, market_cap, sector, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                data.ticker,
                data.pe_ratio,
                data.pb_ratio,
                data.revenue_growth,
                data.earnings_growth,
                data.debt_to_equity,
                data.free_cash_flow,
                data.market_cap,
                data.sector,
                now,
            ),
        )


def get_fundamentals_cache(ticker: str, max_age_hours: int = 24) -> FundamentalData | None:
    """Returns the cached FundamentalData for ticker if fetched_at is within max_age_hours, else None."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM fundamentals_cache WHERE ticker = ? AND fetched_at >= ?",
            (ticker, cutoff),
        ).fetchone()
    if row is None:
        return None
    return FundamentalData(
        ticker=row["ticker"],
        pe_ratio=row["pe_ratio"],
        pb_ratio=row["pb_ratio"],
        revenue_growth=row["revenue_growth"],
        earnings_growth=row["earnings_growth"],
        debt_to_equity=row["debt_to_equity"],
        free_cash_flow=row["free_cash_flow"],
        market_cap=row["market_cap"],
        sector=row["sector"] or "",
    )
