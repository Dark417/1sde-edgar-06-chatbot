"""G1-G4, V5, V9, V10 through the real LangGraph runner with a scripted model."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from finchat.agent.base import TurnResult
from finchat.agent.lg_runner import LangGraphRunner
from finchat.prompts import ERROR_TEXT, REFUSAL_TEXT, TOO_BROAD_TEXT

pytestmark = pytest.mark.loop


class ScriptedModel(BaseChatModel):
    """Emits a predetermined sequence of AIMessages, ignoring input."""

    script: list[AIMessage] = []
    i: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedModel:
        return self

    def _generate(
        self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        msg = self.script[min(self.i, len(self.script) - 1)]
        object.__setattr__(self, "i", self.i + 1)
        return ChatResult(generations=[ChatGeneration(message=msg)])


def call(name: str, args: dict, cid: str) -> dict:
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


def make_runner(store, script: list[AIMessage], gate: list[AIMessage] | None = None):
    model = ScriptedModel(script=script)
    gate_model = ScriptedModel(script=gate) if gate else None
    return LangGraphRunner(store, model, gate_model=gate_model, topic_gate=gate is not None)


def test_happy_path_two_tools_trace_matches(store) -> None:
    script = [
        AIMessage(content="", tool_calls=[call("search_companies", {"q": "alpha"}, "1")]),
        AIMessage(
            content="", tool_calls=[call("get_company_financials", {"cik": "0000000001"}, "2")]
        ),
        AIMessage(
            content="Alpha Corp revenue was 1,000,000 USD for FY2024 "
            "(accession 0000000001-24-000000)."
        ),
    ]
    r = make_runner(store, script).run_turn("s", "what was alpha's revenue?")
    assert r.outcome == "answered"
    assert [t.tool for t in r.tools_called] == ["search_companies", "get_company_financials"]
    assert r.tools_called[0].args_display["q"].startswith("sha256:")  # not verbatim
    assert "1,000,000" in r.text


def test_out_of_scope_token_maps_to_refusal(store) -> None:
    r = make_runner(store, [AIMessage(content="OUT_OF_SCOPE")]).run_turn(
        "s", "who wins the election?"
    )
    assert r.outcome == "refused"
    assert r.text == REFUSAL_TEXT


def test_fourth_round_halts_too_broad(store) -> None:
    def burst(i: int) -> AIMessage:  # distinct objects: add_messages dedupes by id
        return AIMessage(content="", tool_calls=[call("list_companies", {}, f"x{i}")])

    r = make_runner(
        store, [burst(0), burst(1), burst(2), burst(3), AIMessage(content="never")]
    ).run_turn("s", "everything")
    assert r.outcome == "too_broad"
    assert r.text == TOO_BROAD_TEXT
    assert len(r.tools_called) == 3  # the 4th round never ran


def test_char_cap_halts_too_broad(store, monkeypatch) -> None:
    import finchat.agent.lg_runner as lg

    monkeypatch.setattr(lg, "TOOL_OUTPUT_CHAR_CAP", 50)
    burst = AIMessage(content="", tool_calls=[call("list_companies", {}, "x0")])
    r = make_runner(store, [burst, AIMessage(content="never")]).run_turn("s", "everything")
    assert r.outcome == "too_broad"
    assert r.text == TOO_BROAD_TEXT


def test_tool_exception_becomes_error_taxonomy_not_traceback(store, monkeypatch) -> None:
    from finchat.tools import registry

    def boom(store_, params_):
        raise RuntimeError("secret path C:\\Users\\dev\\x arn:aws:iam::123456789012:role/r")

    monkeypatch.setitem(
        registry.BY_NAME,
        "list_companies",
        registry.ToolSpec("list_companies", boom, registry.BY_NAME["list_companies"].params),
    )
    script = [
        AIMessage(content="", tool_calls=[call("list_companies", {}, "1")]),
        AIMessage(content="The data tool reported an internal problem."),
    ]
    r = make_runner(store, script).run_turn("s", "list companies")
    assert r.tools_called[0].error_kind == "denied"
    assert "arn:aws" not in r.text and "123456789012" not in r.text


def test_runner_exception_is_error_text(store, monkeypatch) -> None:
    runner = make_runner(store, [AIMessage(content="x")])
    monkeypatch.setattr(runner, "_graph", None)  # invoke will explode
    r = runner.run_turn("s", "hi")
    assert r.outcome == "error"
    assert r.text == ERROR_TEXT


def test_gate_refuses_before_main_model(store) -> None:
    r = make_runner(
        store, [AIMessage(content="SHOULD NEVER RUN")], gate=[AIMessage(content="OUT")]
    ).run_turn("s", "lottery numbers?")
    assert r.outcome == "refused"
    assert r.text == REFUSAL_TEXT
    assert r.tokens_in == 0 and r.tokens_out == 0  # zero main-model spend (G3)


def test_gate_error_fails_open_and_is_flagged(store) -> None:
    r = make_runner(
        store,
        [AIMessage(content="fine answer with no numbers")],
        gate=[AIMessage(content="MAYBE?")],
    ).run_turn("s", "hello")
    assert r.outcome == "answered"
    assert r.gate_error is True


def test_advice_refusal_via_scope_token_no_tools(store) -> None:
    r = make_runner(store, [AIMessage(content="OUT_OF_SCOPE")]).run_turn("s", "should I buy Alpha?")
    assert r.outcome == "refused" and r.tools_called == []


def test_sessions_do_not_cross_contaminate(store) -> None:
    runner = make_runner(store, [AIMessage(content="a1"), AIMessage(content="b1")])
    runner.run_turn("session-A", "q1")
    runner.run_turn("session-B", "q2")
    assert len(runner._sessions) == 2
    texts_a = [m.content for m in runner._sessions["session-A"]]
    assert "q2" not in texts_a


def test_ui_trace_renderer_consumes_only_turnresult(store) -> None:
    from finchat.ui.chat import render_trace_lines

    script = [
        AIMessage(content="", tool_calls=[call("list_companies", {}, "1")]),
        AIMessage(content="done"),
    ]
    r = make_runner(store, script).run_turn("s", "companies?")
    lines = render_trace_lines(r)
    assert any("list_companies" in line for line in lines)
    assert isinstance(r, TurnResult)
