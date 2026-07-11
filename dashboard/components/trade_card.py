import streamlit as st
from core.models import TradePlan


def render_trade_card(plan: TradePlan) -> None:
    """Displays a trade plan as a structured card."""
    side_color = "green" if plan.side.value == "buy" else "red"
    st.markdown(f"### :{side_color}[{plan.side.value.upper()}] {plan.ticker}")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Entry",       f"${plan.entry_price:.2f}" if plan.entry_price else "Market")
    col2.metric("Stop Loss",   f"${plan.stop_loss:.2f}")
    col3.metric("Take Profit", f"${plan.take_profit:.2f}")
    col4.metric("Quantity",    str(plan.quantity))

    rr = abs(plan.take_profit - (plan.entry_price or 0)) / max(abs((plan.entry_price or 0) - plan.stop_loss), 0.01)
    st.caption(f"R:R ratio {rr:.1f}:1 | Confidence {plan.confidence:.1%} | Status: {plan.status.value}")
    st.caption(f"Reasoning: {plan.reasoning}")
