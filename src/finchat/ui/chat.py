"""Streamlit page. Wiring and rendering only — consumes TurnResult, never a
runner's internals (rule 12). Logic lives in testable helpers below."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from finchat.agent.base import TurnResult
from finchat.prompts import (
    BUDGET_TEXT,
    ERROR_TEXT,
    KILLED_TEXT,
    LIMIT_TEXT,
    TOO_BROAD_TEXT,
    redact,
)

log = logging.getLogger("finchat")

# Every one of these is answerable from the loaded export. That is the whole
# point: a suggested question the dataset cannot answer produces a correct
# refusal that a first-time visitor reads as a broken product. Between them they
# cover the three things the agent can do -- list what exists, rank across
# companies, and compare an original figure against its restatement.
SAMPLE_QUESTIONS = (
    "What companies do you have data on?",
    "Which company had the biggest material restatement?",
    "What was Apple's most recently reported revenue?",
    "Which restatements were filed more than a year after the original?",
)

GUARD_TEXTS = {
    "limit": LIMIT_TEXT,
    "daily": LIMIT_TEXT,
    "budget": BUDGET_TEXT,
    "killed": KILLED_TEXT,
    "conversation": TOO_BROAD_TEXT,
}


def render_trace_lines(result: TurnResult) -> list[str]:
    """Pure helper (tested): trace lines from a TurnResult only."""
    lines = []
    for r in result.tools_called:
        args = ", ".join(f"{k}={v}" for k, v in r.args_display.items()) or "no args"
        if r.error_kind:
            lines.append(f"`{r.tool}`({args}) -> error:{r.error_kind}")
        else:
            lines.append(f"`{r.tool}`({args}) -> {r.row_count} row(s)")
        for c in r.caveats:
            lines.append(f"  caveat: {c}")
    if result.gate_error:
        lines.append("gate_error: topic gate failed open")
    return [redact(line) for line in lines]


def log_turn(session_id: str, impl: str, model_id: str, result: TurnResult) -> None:
    line = (
        f"turn session={session_id[:8]} impl={impl} model={model_id} "
        f"tools={[r.tool for r in result.tools_called]} "
        f"in={result.tokens_in} out={result.tokens_out} outcome={result.outcome}"
    )
    log.info(redact(line))


def main() -> None:  # pragma: no cover - streamlit script body
    import streamlit as st

    from finchat.config import Settings, pick_model
    from finchat.data.store import GoldStore
    from finchat.guard.limits import Guard

    st.set_page_config(page_title="EDGAR lakehouse assistant", page_icon="📊", layout="wide")

    @st.cache_resource(show_spinner="Loading the gold layer…")
    def boot() -> dict[str, Any]:
        settings = Settings.load()
        store = GoldStore(settings.data_dir, settings.serving_prefix)
        guard = Guard(settings.data_dir, settings.token_budget_day)
        import boto3

        brt = boto3.client("bedrock-runtime", region_name=settings.region)
        model_id = pick_model(brt, settings.model_main)
        return {"settings": settings, "store": store, "guard": guard, "model_id": model_id}

    try:
        ctx = boot()
    except Exception as exc:
        st.error(redact(f"startup failed: {type(exc).__name__}: {exc}"))
        st.stop()

    settings, store, guard = ctx["settings"], ctx["store"], ctx["guard"]

    with st.sidebar:
        impl = st.radio(
            "Agent implementation",
            ["langgraph", "adk"],
            index=0 if settings.agent_impl == "langgraph" else 1,
        )
        st.caption(f"Model: `{ctx['model_id'].rsplit('.', 1)[-1]}`")
        counts = store.row_counts
        for label, key in [
            ("Companies", "company_profile"),
            ("Facts", "financials_current"),
            ("Restatements", "restatement_event"),
        ]:
            st.metric(label, f"{counts.get(key, 0):,}")
        st.caption(f"Exported {str(store.manifest.get('generated_at', 'unknown'))[:19]}")
        if settings.links:
            st.divider()
            for label, url in settings.links.items():
                st.link_button(label, url, use_container_width=True)

        st.divider()
        st.caption(
            "The model never calculates: every figure comes from a fixed SQL tool, "
            "and each answer shows its tool trace. materiality_band is a product "
            "heuristic, not an accounting standard. Not investment advice."
        )

    @st.cache_resource
    def get_runner(which: str, model_id: str) -> Any:
        from langchain_aws import ChatBedrockConverse

        main = ChatBedrockConverse(
            model=model_id, region_name=settings.region, temperature=0.0, max_tokens=1024
        )
        if which == "adk":
            from finchat.agent.adk_runner import AdkRunner, litellm_bedrock

            return AdkRunner(store, litellm_bedrock(model_id), topic_gate=False)
        from finchat.agent.lg_runner import LangGraphRunner

        return LangGraphRunner(
            store,
            main,
            gate_model=main if settings.topic_gate else None,
            topic_gate=settings.topic_gate,
        )

    if "sid" not in st.session_state:
        st.session_state.sid = uuid.uuid4().hex
        st.session_state.history = []

    for role, text, trace in st.session_state.history:
        with st.chat_message(role):
            st.markdown(text)
            if trace:
                with st.expander(f"How I got this — {len(trace)} step(s)"):
                    for line in trace:
                        st.markdown(line)

    # Sample questions, first turn only. An empty chat box is the hardest moment
    # for a visitor who does not know what this dataset contains -- and asking
    # for something that is not in it ("What was Tesla's revenue?") produces a
    # correct refusal that reads like a broken demo. These four are all
    # answerable from the eight companies loaded, and between them exercise the
    # lookup, the ranking and the restatement comparison.
    #
    # They disappear once a conversation exists: they are a starting point, not
    # a permanent toolbar competing with the thing the visitor just typed.
    if not st.session_state.history:
        st.caption("Try one of these:")
        cols = st.columns(2)
        for i, question in enumerate(SAMPLE_QUESTIONS):
            if cols[i % 2].button(question, key=f"sample-{i}", use_container_width=True):
                st.session_state.pending = question
                st.rerun()

    # chat_input is evaluated unconditionally, not short-circuited behind the
    # pending question: `pending or st.chat_input(...)` would skip rendering the
    # widget on the run that consumes a sample, and the input box would vanish
    # for exactly the turn the visitor is watching.
    typed = st.chat_input("Ask about the data…")
    if prompt := st.session_state.pop("pending", None) or typed:
        st.session_state.history.append(("user", prompt, []))
        with st.chat_message("user"):
            st.markdown(prompt)

        decision = guard.check(st.session_state.sid)
        if not decision.allowed:
            text = GUARD_TEXTS.get(decision.reason, ERROR_TEXT)
            with st.chat_message("assistant"):
                st.markdown(text)
            st.session_state.history.append(("assistant", text, []))
        else:
            with st.chat_message("assistant"), st.spinner("Querying…"):
                try:
                    runner = get_runner(impl, ctx["model_id"])
                    result = runner.run_turn(st.session_state.sid, prompt)
                except Exception:
                    result = TurnResult(text=ERROR_TEXT, outcome="error")
                guard.settle(result.tokens_in + result.tokens_out)
                st.markdown(result.text)
                trace = render_trace_lines(result)
                if trace:
                    with st.expander(f"How I got this — {len(trace)} step(s)"):
                        for line in trace:
                            st.markdown(line)
            log_turn(st.session_state.sid, impl, ctx["model_id"], result)
            st.session_state.history.append(("assistant", result.text, trace))
