"""Streamlit chat over the EDGAR gold layer.

Run:  streamlit run app.py

The "How I got this" expander under each answer is not decoration -- it shows
every tool call and its arguments, which is what separates an auditable data
agent from a chatbot that sounds confident. If an answer looks wrong, the trace
tells you whether the model asked the wrong question or the data is wrong.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent / "src"))

from chatbot.agent import Agent  # noqa: E402
from chatbot.tools import GoldStore  # noqa: E402

st.set_page_config(page_title="EDGAR lakehouse assistant", page_icon="📊", layout="wide")

SUGGESTIONS = [
    "What companies do you have data on?",
    "Which company had the biggest material restatement?",
    "Compare revenue across all companies",
    "Tell me about Apple's profile and financials",
    "How many restatements are there, and which concepts get restated most?",
    "Show JPMorgan's net income by year",
]


@st.cache_resource(show_spinner=False)
def _load() -> tuple[GoldStore, Agent]:
    store = GoldStore()
    return store, Agent(store)


def _fmt_args(args: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in args.items()) or "no arguments"


try:
    store, agent = _load()
except FileNotFoundError as exc:
    st.error(f"{exc}")
    st.stop()

# ---------------------------------------------------------------- sidebar ---
with st.sidebar:
    st.subheader("Dataset")
    counts = store.manifest.get("row_counts", {})
    for label, key in [
        ("Companies", "company_profile"),
        ("Financial facts", "financials_current"),
        ("Restatements", "restatement_event"),
        ("Activity days", "filing_activity_daily"),
    ]:
        st.metric(label, f"{counts.get(key, 0):,}")

    exported = store.manifest.get("generated_at", "unknown")
    st.caption(f"Exported {exported[:19].replace('T', ' ')} UTC")
    st.caption(f"Model: `{agent.model_id.split(chr(46))[-1]}`")

    st.divider()
    st.caption(
        "**How this works.** The model never calculates. It chooses a tool; the "
        "tool runs fixed SQL over the exported gold tables and returns rows. "
        "Every figure you see came from a query, not from the model."
    )
    st.caption(
        "Reads local Parquet exported from Databricks — no live warehouse "
        "dependency, so quota limits cannot take it down."
    )
    st.divider()
    st.caption(
        "Portfolio project on Databricks Free Edition. Not investment advice. "
        "`materiality_band` is a product heuristic, not an accounting standard."
    )
    if st.button("Clear conversation"):
        st.session_state.history = []
        st.session_state.messages = []
        st.rerun()

# ------------------------------------------------------------------- main ---
st.title("EDGAR lakehouse assistant")
st.caption(
    "Ask about company profiles, reported financials, or restatements — figures "
    "a company published and later corrected."
)

if "history" not in st.session_state:
    st.session_state.history = []   # [(role, text, trace)]
    st.session_state.messages = []  # Bedrock conversation

if not st.session_state.history:
    st.write("**Try one of these:**")
    cols = st.columns(3)
    for i, s in enumerate(SUGGESTIONS):
        if cols[i % 3].button(s, key=f"sug{i}", use_container_width=True):
            st.session_state.pending = s
            st.rerun()

for role, text, trace in st.session_state.history:
    with st.chat_message(role):
        st.markdown(text)
        if trace:
            with st.expander(f"How I got this — {len(trace)} tool call(s)"):
                for t in trace:
                    if t.error:
                        st.error(f"`{t.tool}`({_fmt_args(t.args)}) → {t.error}")
                    else:
                        st.markdown(f"`{t.tool}`({_fmt_args(t.args)}) → **{t.row_count}** row(s)")
                        for c in t.caveats:
                            st.caption(f"⚠️ {c}")

prompt = st.chat_input("Ask about the data…") or st.session_state.pop("pending", None)

if prompt:
    st.session_state.history.append(("user", prompt, []))
    with st.chat_message("user"):
        st.markdown(prompt)

    st.session_state.messages.append({"role": "user", "content": [{"text": prompt}]})
    with st.chat_message("assistant"), st.spinner("Querying the lakehouse…"):
        try:
            answer, trace, updated = agent.chat(st.session_state.messages)
            st.session_state.messages = updated
        except Exception as exc:  # show the real error; a silent failure is worse
            answer, trace = f"**Request failed.**\n\n```\n{type(exc).__name__}: {exc}\n```", []
        st.markdown(answer)
        if trace:
            with st.expander(f"How I got this — {len(trace)} tool call(s)"):
                for t in trace:
                    if t.error:
                        st.error(f"`{t.tool}`({_fmt_args(t.args)}) → {t.error}")
                    else:
                        st.markdown(f"`{t.tool}`({_fmt_args(t.args)}) → **{t.row_count}** row(s)")
                        for c in t.caveats:
                            st.caption(f"⚠️ {c}")

    st.session_state.history.append(("assistant", answer, trace))
