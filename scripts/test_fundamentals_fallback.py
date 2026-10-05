"""
Standalone check for the fundamentals_feed.py fallback chain.
Run this directly from your project root:

    python test_fundamentals_fallback.py

It bypasses the orchestrator, FinBERT, Ollama, and Alpaca entirely —
only touches yfinance (+ Finnhub if configured) through your
fundamentals_feed module.
"""

import logging
import os
import sys

# Add the project root (one level above this scripts/ folder) to sys.path
# so `data`, `core`, `agents` etc. resolve no matter where this is run from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.feeds.fundamentals_feed import _fetch_fundamentals_live, _cache

# Show the fallback logging (e.g. "D/E filled via Finnhub fallback") on screen
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# Known problem tickers from your live run + one control ticker that
# already worked fine (MSFT), so you can compare behavior.
TICKERS_TO_CHECK = ["JPM", "ISRG", "BA", "MSFT"]

print("=" * 70)
for ticker in TICKERS_TO_CHECK:
    # Call the LIVE fetch directly, skipping the in-memory/SQLite caches,
    # so you always see a fresh result rather than an old cached None.
    data = _fetch_fundamentals_live(ticker)

    print(f"\n{ticker}")
    print(f"  P/E:            {data.pe_ratio}")
    print(f"  P/B:            {data.pb_ratio}")
    print(f"  Revenue Growth: {data.revenue_growth}")
    print(f"  Earnings Growth:{' MISSING' if data.earnings_growth is None else f' {data.earnings_growth}'}")
    print(f"  Debt/Equity:    {' MISSING' if data.debt_to_equity is None else f' {data.debt_to_equity}'}")
    print(f"  Sector:         {data.sector}")

print("\n" + "=" * 70)
print("Done. Any WARNING lines above show a field that stayed empty even")
print("after all three fallback layers (info -> statements -> Finnhub).")