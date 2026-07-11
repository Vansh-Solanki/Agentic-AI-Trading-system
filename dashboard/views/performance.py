import streamlit as st
import pandas as pd
import numpy as np
from data.storage.decision_store import get_trade_history


def render():
    st.title("Performance Analytics")

    trades = get_trade_history(limit=500)
    filled = [t for t in trades if t["status"] == "filled" and t["pnl"] is not None]

    if not filled:
        st.info("No completed trades yet.")
        return

    df = pd.DataFrame(filled)
    df["pnl"] = df["pnl"].astype(float)
    df["created_at"] = pd.to_datetime(df["created_at"])

    total_pnl  = df["pnl"].sum()
    win_rate   = (df["pnl"] > 0).mean()
    avg_win    = df.loc[df["pnl"] > 0, "pnl"].mean() if (df["pnl"] > 0).any() else 0
    avg_loss   = df.loc[df["pnl"] < 0, "pnl"].mean() if (df["pnl"] < 0).any() else 0
    cum_pnl    = df["pnl"].cumsum()
    max_dd     = (cum_pnl - cum_pnl.cummax()).min()
    sharpe     = (df["pnl"].mean() * 252) / (df["pnl"].std() * np.sqrt(252)) if df["pnl"].std() else 0

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total P&L",   f"${total_pnl:,.2f}")
    col2.metric("Win Rate",    f"{win_rate:.1%}")
    col3.metric("Max Drawdown",f"${max_dd:,.2f}")
    col4.metric("Sharpe",      f"{sharpe:.2f}")

    st.subheader("Cumulative P&L")
    st.line_chart(cum_pnl.values)

    st.subheader("Trade History")
    st.dataframe(df[["created_at","ticker","side","quantity","entry_price","pnl"]].sort_values("created_at", ascending=False),
                 use_container_width=True)
