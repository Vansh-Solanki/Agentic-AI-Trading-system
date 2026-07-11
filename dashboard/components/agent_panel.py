import streamlit as st
from core.models import AgentOutput


def render_agent_panel(output: AgentOutput) -> None:
    """Renders a collapsible panel for a single agent's output."""
    signal_count = len(output.signals)
    label = f"**{output.agent_name}** — {signal_count} signal(s) | {output.summary[:80]}"

    with st.expander(label, expanded=False):
        if output.signals:
            for s in output.signals:
                color = "green" if "buy" in s.signal_type.value else ("red" if "sell" in s.signal_type.value else "gray")
                st.markdown(
                    f":{color}[**{s.signal_type.value.upper()}**] "
                    f"`{s.timeframe.value}` conf={s.confidence:.2f} — {s.reasoning}"
                )
        else:
            st.caption("No signals this cycle.")

        if output.raw_data:
            st.json(output.raw_data)
