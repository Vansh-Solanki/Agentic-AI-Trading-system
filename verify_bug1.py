"""
verify_bug1.py

Run from the project root (the folder that contains agents/, risk/, core/):

    python verify_bug1.py

Checks that RiskManagerAgent.validate_plan() resizes BEFORE the VaR gate, using
the nine intraday plans from the 2026-10-04 premarket log. It does not place
orders or write to the database. It does read real prices (for volatility) and
the real open-trades table, so a rejection caused by an already-used VaR budget
will show up in the reason column.
"""
import sys

import core.config as cfg
from core.config import TradingMode
from core.constants import OrderSide, OrderType
from core.models import TradePlan
from agents.risk_manager import RiskManagerAgent

PORTFOLIO_VALUE = 100_628.91

# (ticker, side, quantity, entry, stop) as produced by TradePlanAgent in the log
INTRADAY_PLANS = [
    ("AAPL",  "buy",  1603, 333.79, 332.53),
    ("NVDA",  "buy",  1672, 233.99, 232.79),
    ("V",     "sell", 2076, 360.72, 361.69),
    ("XOM",   "buy",  2854, 164.12, 163.41),
    ("NEE",   "buy",  6247,  76.82,  76.50),
    ("CAT",   "buy",   377, 845.53, 840.20),
    ("BA",    "buy",  1029, 193.59, 191.63),
    ("FCX",   "buy",  4374,  72.04,  71.58),
    ("GOOGL", "buy",  1406, 343.59, 342.16),
]


def make_plan(ticker, side, qty, entry, stop, mode="intraday"):
    return TradePlan(
        ticker=ticker,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=qty,
        entry_price=entry,
        stop_loss=stop,
        take_profit=entry + 2 * (entry - stop),
        confidence=0.5,
        reasoning="bug 1 verification",
        strategy_mode=mode,
        metadata={},
    )


def main() -> int:
    risk = RiskManagerAgent(TradingMode.INTRADAY)
    failures = 0

    print(f"Position cap: {cfg.MAX_POSITION_SIZE_PCT:.0%} of ${PORTFOLIO_VALUE:,.2f}\n")
    print(f"{'ticker':6} {'before':>7} {'after':>6} {'notional':>9}  result")
    for ticker, side, qty, entry, stop in INTRADAY_PLANS:
        plan = make_plan(ticker, side, qty, entry, stop)
        approved, reason = risk.validate_plan(plan, PORTFOLIO_VALUE)
        cap_qty = PORTFOLIO_VALUE * cfg.MAX_POSITION_SIZE_PCT / entry
        notional = plan.quantity * entry

        problems = []
        if not approved:
            problems.append("REJECTED: " + reason)
        # validate_plan() only resizes when quantity > cap * 1.05, and the original
        # code uses round(), so allow the same 5% tolerance plus half a share.
        if plan.quantity > cap_qty * 1.05 + 0.5:
            problems.append(f"quantity {plan.quantity} exceeds cap {cap_qty:.1f}")
        if approved and plan.quantity <= 0:
            problems.append("approved with zero quantity")

        status = "ok  " + reason if not problems else "FAIL " + "; ".join(problems)
        failures += bool(problems)
        print(f"{ticker:6} {qty:>7} {int(plan.quantity):>6} {notional:>9,.0f}  {status}")

    # Edge case: an invalid stop makes the sizer return 0 shares.
    # The plan must NOT come back approved with quantity 0.
    print("\nEdge case: stop_loss = 0 (sizer returns 0 shares)")
    try:
        plan = make_plan("AAPL", "buy", 100, 333.79, 0.0)
        approved, reason = risk.validate_plan(plan, PORTFOLIO_VALUE)
        if approved and plan.quantity <= 0:
            print(f"FAIL approved with quantity {plan.quantity}")
            failures += 1
        else:
            print(f"ok  approved={approved}, quantity={plan.quantity}, reason={reason}")
    except Exception as exc:  # the model may refuse a zero stop at construction
        print(f"ok  plan construction or validation raised {type(exc).__name__}: {exc}")

    print("\nRESULT:", "PASS" if failures == 0 else f"FAIL ({failures} problem(s))")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
