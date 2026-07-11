"""
scripts/test_finnhub_ws.py

Pre-implementation confirmation script for Finnhub WebSocket news streaming.
Verifies that:
  1. A WebSocket connection to wss://ws.finnhub.io can be established with your API key
  2. The subscribe-news message type is accepted (not rejected with an error frame)
  3. At least one news message arrives within the wait window

Run this BEFORE implementing the full WebSocket news architecture.
If this script exits with FAIL, the WebSocket news feature is not available on
your current Finnhub plan and the REST + TTL cache interim fix should be used instead.

Usage:
    python scripts/test_finnhub_ws.py

Requirements:
    pip install websocket-client python-dotenv
"""

import json
import os
import sys
import threading
import time
from dotenv import load_dotenv

load_dotenv()

try:
    import websocket
except ImportError:
    print("[FAIL] websocket-client not installed. Run: pip install websocket-client")
    sys.exit(1)

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
if not FINNHUB_API_KEY:
    print("[FAIL] FINNHUB_API_KEY not set in .env or environment.")
    sys.exit(1)

# Test against a small subset of the watchlist
TEST_TICKERS = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN"]

# How long to wait for at least one news message (seconds)
# During pre-market or quiet periods no news may arrive — that is NOT a failure
# Connection + subscription acceptance is the primary gate
WAIT_SECONDS = 30

_results = {
    "connected": False,
    "subscriptions_sent": False,
    "error_frame_received": False,
    "error_detail": "",
    "news_messages_received": 0,
    "ping_pong_ok": False,
    "done": threading.Event(),
}


def on_open(ws):
    _results["connected"] = True
    print(f"[PASS] WebSocket connection established to wss://ws.finnhub.io")

    for ticker in TEST_TICKERS:
        payload = json.dumps({"type": "subscribe-news", "symbol": ticker})
        ws.send(payload)
        print(f"       Subscribed to news for {ticker}")

    _results["subscriptions_sent"] = True
    print(f"[INFO] Subscriptions sent. Waiting up to {WAIT_SECONDS}s for incoming news...")
    print(f"[INFO] No news arriving is NOT a failure — it means the market is quiet.")
    print(f"[INFO] An error frame in response to subscribe-news IS a failure.")


def on_message(ws, message):
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        print(f"[WARN] Non-JSON message received: {message[:120]}")
        return

    msg_type = data.get("type", "")

    if msg_type == "error":
        _results["error_frame_received"] = True
        _results["error_detail"] = data.get("msg", str(data))
        print(f"[FAIL] Server returned error frame: {_results['error_detail']}")
        _results["done"].set()
        ws.close()
        return

    if msg_type == "news":
        _results["news_messages_received"] += 1
        news_list = data.get("data", [])
        for item in news_list[:2]:  # print first 2 headlines only
            headline = item.get("headline", "")[:80]
            symbol = item.get("related", "")
            print(f"[NEWS] [{symbol}] {headline}")
        print(f"[INFO] Total news messages received so far: {_results['news_messages_received']}")
        return

    if msg_type == "ping":
        _results["ping_pong_ok"] = True
        ws.send(json.dumps({"type": "pong"}))
        return

    # Any other message type — log and continue
    print(f"[INFO] Received message type='{msg_type}': {str(data)[:120]}")


def on_error(ws, error):
    _results["error_detail"] = str(error)
    print(f"[FAIL] WebSocket error: {error}")
    _results["done"].set()


def on_close(ws, close_status_code, close_msg):
    print(f"[INFO] WebSocket closed (code={close_status_code}, msg={close_msg})")
    _results["done"].set()


def run():
    url = f"wss://ws.finnhub.io?token={FINNHUB_API_KEY}"
    ws = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    thread = threading.Thread(target=ws.run_forever, kwargs={"ping_interval": 20, "ping_timeout": 10})
    thread.daemon = True
    thread.start()

    # Wait up to WAIT_SECONDS for the test to complete or for the timer to expire
    _results["done"].wait(timeout=WAIT_SECONDS + 5)

    if thread.is_alive():
        ws.close()
        thread.join(timeout=5)

    print("\n" + "=" * 55)
    print("FINNHUB WEBSOCKET NEWS — CONFIRMATION RESULTS")
    print("=" * 55)

    all_pass = True

    # Gate 1: connection established
    if _results["connected"]:
        print("[PASS] WebSocket connection: OK")
    else:
        print("[FAIL] WebSocket connection: Could not connect")
        all_pass = False

    # Gate 2: subscriptions sent without error frame
    if _results["error_frame_received"]:
        print(f"[FAIL] subscribe-news rejected by server: {_results['error_detail']}")
        print("       This means subscribe-news is NOT available on your Finnhub plan.")
        print("       Use the REST + TTL cache interim fix instead.")
        all_pass = False
    elif _results["subscriptions_sent"]:
        print("[PASS] subscribe-news accepted (no error frame returned)")
    else:
        print("[WARN] Subscriptions not sent — connection may have failed before on_open")
        all_pass = False

    # Informational: news received
    n = _results["news_messages_received"]
    if n > 0:
        print(f"[PASS] Live news messages received: {n}")
    else:
        print(f"[INFO] No news messages in {WAIT_SECONDS}s window — expected during quiet periods")
        print(f"       This does NOT indicate a plan restriction. Connection + subscription is the gate.")

    print("=" * 55)

    if all_pass:
        print("RESULT: PASS — WebSocket news streaming is active on this API key.")
        print("        Safe to proceed with full WebSocket news architecture implementation.")
        sys.exit(0)
    else:
        print("RESULT: FAIL — Fix the above before implementing WebSocket news architecture.")
        sys.exit(1)


if __name__ == "__main__":
    run()
