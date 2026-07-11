import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st
from data.storage.db import init_schema

init_schema()

st.set_page_config(
    page_title="Trading Agent",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.sidebar.title("Trading Agent")
st.sidebar.markdown("---")

pages = {
    "Live Trading":    "dashboard/views/live_trading.py",
    "Agent Reasoning": "dashboard/views/agent_reasoning.py",
    "Performance":     "dashboard/views/performance.py",
    "Spike Log":       "dashboard/views/spike_log.py",
}

page = st.sidebar.radio("Navigate", list(pages.keys()))
st.sidebar.markdown("---")
st.sidebar.caption("Paper trading — autonomous mode")

if page == "Live Trading":
    from views import live_trading
    live_trading.render()
elif page == "Agent Reasoning":
    from views import agent_reasoning
    agent_reasoning.render()
elif page == "Performance":
    from views import performance
    performance.render()
elif page == "Spike Log":
    from views import spike_log
    spike_log.render()
