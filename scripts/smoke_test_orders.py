"""
Manual smoke test for the Bug 6 broker-layer changes (execution/broker/alpaca.py:
get_order_state, cancel_order, submit_limit_order). NOT part of pytest — run this
by hand against a real paper account:

    python scripts/smoke_test_orders.py

What it does (touches no database, works with the market open or closed):
  1. Refuses to run unless PAPER_TRADING=true.
  2. Submits a 1-share LIMIT BUY on a liquid ticker at ~50% of the last price
     (client_order_id "smoke-<timestamp>") — far enough off-market that it
     cannot fill, so it's safe to run any time.
  3. Reads it back with get_order_state() and prints the raw status string.
  4. Cancels it with cancel_order(), polls up to 30s, prints the final status.
  5. Calls get_order_state() on a client_order_id that doesn't exist (expect
     None), then repeats the same lookup one level down (the raw
     TradingClient call get_order_state() wraps) to print the actual
     exception type and .status_code Alpaca raises, confirming
     `exc.status_code == 404` is the right check against the installed
     alpaca-py version.
  6. Prints every real status string seen, to compare by eye against the
     strings execution/order_sync.py's _sync_one() switches on (canceled,
     expired, rejected, filled, partially_filled, pending_cancel).

This script places one real (unfillable) order on your paper account and
cancels it. It does not touch trading_agent.db or any other local file.
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import PAPER_TRADING
from core.constants import OrderSide, OrderType
from core.models import TradePlan
from data.feeds.price_feed import fetch_snapshot
from execution.broker.alpaca import AlpacaBroker

TICKER = "AAPL"
POLL_SECONDS = 30
POLL_INTERVAL = 2

# Statuses execution/order_sync.py explicitly recognizes — printed at the end
# next to what this script actually observed, for a by-eye comparison.
ORDER_SYNC_KNOWN_STATUSES = {
    "filled", "partially_filled", "canceled", "expired", "rejected", "pending_cancel",
}


def main() -> int:
    if not PAPER_TRADING:
        print("REFUSING TO RUN: PAPER_TRADING is not true. This script places a real "
              "order and will not run against a live account.")
        return 1

    observed_statuses: list[str] = []

    print(f"Fetching last price for {TICKER}...")
    snap = fetch_snapshot(TICKER)
    last_price = snap.price
    if not last_price:
        print(f"Could not get a last price for {TICKER} — aborting.")
        return 1
    print(f"  last price: {last_price:.2f}")

    unfillable_price = round(last_price * 0.5, 2)
    coid = f"smoke-{int(time.time())}"
    print(f"\nSubmitting 1-share LIMIT BUY {TICKER} @ ~{unfillable_price:.2f} "
          f"(~50% of last price, should never fill) | client_order_id={coid}")

    plan = TradePlan(
        ticker=TICKER,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=1,
        entry_price=unfillable_price,
        stop_loss=unfillable_price * 0.9,
        take_profit=unfillable_price * 1.1,
        confidence=0.5,
        reasoning="smoke_test_orders.py manual broker check",
    )

    broker = AlpacaBroker()
    fill = broker.submit_limit_order(plan, client_order_id=coid)
    print(f"  submit response: {fill}")
    observed_statuses.append(str(fill.get("status")))

    print("\nReading it back with get_order_state()...")
    state = broker.get_order_state(coid)
    print(f"  get_order_state(): {state}")
    if state is not None:
        observed_statuses.append(str(state["status"]))

    print("\nCancelling it with cancel_order()...")
    broker.cancel_order(coid)

    print(f"Polling up to {POLL_SECONDS}s for the cancel to finalize...")
    deadline = time.monotonic() + POLL_SECONDS
    final_state = None
    while time.monotonic() < deadline:
        final_state = broker.get_order_state(coid)
        status = final_state["status"] if final_state else None
        print(f"  status: {status}")
        if status in ("canceled", "expired", "rejected", "filled"):
            break
        time.sleep(POLL_INTERVAL)
    if final_state is not None:
        observed_statuses.append(str(final_state["status"]))
    print(f"  FINAL STATUS: {final_state}")

    print("\nChecking get_order_state() on a client_order_id that does not exist...")
    missing_coid = f"smoke-does-not-exist-{int(time.time())}"
    missing_result = broker.get_order_state(missing_coid)
    print(f"  get_order_state('{missing_coid}') -> {missing_result!r}  (expect None)")

    print("\nRepeating that lookup one level down (the raw call get_order_state() wraps), "
          "to show the real exception type/status_code:")
    try:
        broker.client.get_order_by_client_id(missing_coid)
        print("  (no exception raised — unexpected; Alpaca normally 404s here)")
    except Exception as exc:
        status_code = getattr(exc, "status_code", "N/A")
        print(f"  exception type: {type(exc).__module__}.{type(exc).__name__}")
        print(f"  exc.status_code: {status_code}")
        print(f"  exc.status_code == 404: {status_code == 404}")

    print("\n" + "=" * 70)
    print("Observed real status strings this run:", sorted(set(observed_statuses)))
    print("execution/order_sync.py explicitly recognizes:", sorted(ORDER_SYNC_KNOWN_STATUSES))
    unrecognized = sorted(set(observed_statuses) - ORDER_SYNC_KNOWN_STATUSES - {"new", "accepted", "stub_filled"})
    if unrecognized:
        print("Observed but NOT in order_sync's known set (would hit the "
              "'unrecognized status' warning path):", unrecognized)
    else:
        print("All observed statuses are accounted for by order_sync.py.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
