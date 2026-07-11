import streamlit as st
import pandas as pd
from execution.broker.alpaca import AlpacaBroker
from data.storage.decision_store import get_open_trades


def render():
    st.title("Live Trading")

    broker = AlpacaBroker()
    account = broker.get_account()

    col1, col2, col3 = st.columns(3)
    col1.metric("Portfolio Value", f"${account['portfolio_value']:,.2f}")
    col2.metric("Cash Available",  f"${account['cash']:,.2f}")
    col3.metric("Open Positions",  len(broker.get_positions()))

    st.subheader("Open Positions")
    positions = broker.get_positions()
    if positions:
        st.dataframe(pd.DataFrame(positions), use_container_width=True)
    else:
        st.info("No open positions")

    st.subheader("Pending Orders")
    open_trades = get_open_trades()
    if open_trades:
        st.dataframe(pd.DataFrame(open_trades), use_container_width=True)
    else:
        st.info("No pending orders")
