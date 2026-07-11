import streamlit as st
import pandas as pd
from data.storage.news_store import get_recent_news
from core.config import WATCHLIST


def render():
    st.title("Spike Log")
    st.caption("Intraday news spikes and event-driven signals (Layer 3).")

    ticker = st.selectbox("Filter by ticker", ["ALL"] + WATCHLIST)

    search_ticker = None if ticker == "ALL" else ticker
    rows = get_recent_news(search_ticker or "SPY", layer=3, limit=100) if search_ticker else []

    if ticker == "ALL":
        # Fetch for all tickers and merge
        all_rows = []
        for t in WATCHLIST:
            all_rows.extend(get_recent_news(t, layer=3, limit=20))
        rows = sorted(all_rows, key=lambda r: r.get("published_at", ""), reverse=True)[:100]

    if not rows:
        st.info("No spike events recorded yet.")
        return

    df = pd.DataFrame(rows)[["published_at","source","headline","sentiment","summary"]]
    df["sentiment"] = df["sentiment"].round(3)

    positive = df[df["sentiment"] > 0.3]
    negative = df[df["sentiment"] < -0.3]
    st.metric("Bullish spikes", len(positive), delta=None)
    st.metric("Bearish spikes", len(negative), delta=None)

    st.dataframe(df, use_container_width=True)
