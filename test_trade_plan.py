"""
Isolated test of the NaN-close guard proposed for trade_plan.py.

This does NOT import the real trade_plan.py (it depends on core.config,
data.cache.market_data_cache, risk.position_sizing, etc. that aren't
available standalone). Instead it copies the exact patched snippet so we
can verify the *logic* is correct before touching the live file.
"""
import pandas as pd
import numpy as np


def resolve_current_price(df: pd.DataFrame):
    """
    This is the EXACT patched block proposed for trade_plan.py, extracted
    so it can be unit tested on its own. Returns (price, error_summary).
    error_summary is None on success.
    """
    current_price = float(df["Close"].iloc[-1])
    if current_price != current_price:  # NaN check (today's bar not finalized yet)
        if len(df) < 2:
            return None, "No valid close price — cannot generate trade plan"
        current_price = float(df["Close"].iloc[-2])
        if current_price != current_price:  # t-1 also NaN — genuine data problem
            return None, "No valid close price (t and t-1 both NaN) — cannot generate trade plan"
    return current_price, None


def make_df(closes):
    """Minimal OHLC dataframe with only Close populated (enough for this test)."""
    n = len(closes)
    return pd.DataFrame({
        "Open":  [100.0] * n,
        "High":  [101.0] * n,
        "Low":   [99.0] * n,
        "Close": closes,
    })


test_cases = [
    # (description, closes, expected_price, expect_error)
    ("Normal case — today's close is real",
     [170.0, 171.5, 172.85],
     172.85, False),

    ("Today's close is NaN, yesterday is real — should fall back to t-1",
     [170.0, 171.5, np.nan],
     171.5, False),

    ("Both today and yesterday NaN — should error out, NOT reach further back",
     [170.0, np.nan, np.nan],
     None, True),

    ("Only 1 row total and it's NaN — should error, no t-1 to fall back to",
     [np.nan],
     None, True),

    ("Only 1 row total and it's valid — should just use it",
     [172.85],
     172.85, False),
]

print(f"{'Test':60s} {'Result':10s} {'Price':>10s}  {'Error'}")
print("-" * 110)
all_passed = True
for desc, closes, expected_price, expect_error in test_cases:
    df = make_df(closes)
    price, error = resolve_current_price(df)

    if expect_error:
        passed = (error is not None) and (price is None)
    else:
        passed = (error is None) and (price is not None) and abs(price - expected_price) < 1e-9

    all_passed &= passed
    status = "PASS" if passed else "FAIL"
    price_str = f"{price:.2f}" if price is not None else "None"
    print(f"{desc:60s} {status:10s} {price_str:>10s}  {error or ''}")

print("-" * 110)
print("ALL TESTS PASSED" if all_passed else "SOME TESTS FAILED — do not ship the patch yet")