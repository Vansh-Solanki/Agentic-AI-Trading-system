import streamlit as st
import pandas as pd
from data.storage.signal_store import get_signals
from core.config import WATCHLIST


def render():
    st.title("Agent Reasoning")

    ticker = st.selectbox("Select ticker", WATCHLIST)

    if ticker:
        signals = get_signals(ticker, limit=50)
        if signals:
            df = pd.DataFrame(signals)
            st.subheader(f"Last {len(df)} signals for {ticker}")

            for source in df["source"].unique():
                with st.expander(f"Source: {source}", expanded=True):
                    sub = df[df["source"] == source][["created_at", "signal_type", "timeframe", "confidence", "reasoning"]]
                    st.dataframe(sub, use_container_width=True)
        else:
            st.info(f"No signals recorded for {ticker} yet. Run the pipeline first.")
