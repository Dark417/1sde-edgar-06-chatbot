"""G2/G9 through the real ADK runner with a scripted BaseLlm.

Skips with a stated reason when google-adk is absent — locally only. In CI
the adk-seam job installs the extra and promotes skips to failures, so the
seam cannot go green unexecuted (architect finding #2).
"""

from __future__ import annotations

import pytest

adk = pytest.importorskip("google.adk", reason="google-adk not installed (pip install .[adk])")

from google.genai import types as genai_types  # noqa: E402

from finchat.agent.adk_runner import AdkRunner, ScriptableLlm  # noqa: E402
from finchat.prompts import REFUSAL_TEXT, TOO_BROAD_TEXT  # noqa: E402

pytestmark = pytest.mark.loop


def _text_response(text: str):
    from google.adk.models.llm_response import LlmResponse

    return LlmResponse(
        content=genai_types.Content(role="model", parts=[genai_types.Part(text=text)])
    )


def _tool_call_response(name: str, args: dict):
    from google.adk.models.llm_response import LlmResponse

    part = genai_types.Part(function_call=genai_types.FunctionCall(name=name, args=args))
    return LlmResponse(content=genai_types.Content(role="model", parts=[part]))


class Script:
    """Returns queued responses; after exhaustion, repeats the last one."""

    def __init__(self, responses: list) -> None:
        self.responses = responses
        self.i = 0

    def __call__(self, llm_request) -> object:
        r = self.responses[min(self.i, len(self.responses) - 1)]
        self.i += 1
        return r


def make_runner(store, responses: list) -> AdkRunner:
    return AdkRunner(store, ScriptableLlm(Script(responses)), topic_gate=False)


def test_happy_path_tool_then_answer(store) -> None:
    runner = make_runner(
        store,
        [
            _tool_call_response("list_companies", {}),
            _text_response("There are 3 companies in the dataset."),
        ],
    )
    r = runner.run_turn("s1", "what companies do you have?")
    assert r.outcome == "answered"
    assert [t.tool for t in r.tools_called] == ["list_companies"]
    assert "3 companies" in r.text


def test_out_of_scope_maps_to_identical_refusal_text(store) -> None:
    r = make_runner(store, [_text_response("OUT_OF_SCOPE")]).run_turn("s1", "lottery?")
    assert r.outcome == "refused"
    assert r.text == REFUSAL_TEXT  # byte-identical across impls (G9)


def test_round_cap_halts_too_broad(store) -> None:
    burst = [_tool_call_response("list_companies", {}) for _ in range(6)]
    r = make_runner(store, burst + [_text_response("never")]).run_turn("s1", "everything")
    assert r.outcome == "too_broad"
    assert r.text == TOO_BROAD_TEXT


def test_sequential_turns_share_a_session(store) -> None:
    runner = make_runner(store, [_text_response("first answer"), _text_response("second answer")])
    r1 = runner.run_turn("same-session", "q1")
    r2 = runner.run_turn("same-session", "q2")
    assert r1.outcome == r2.outcome == "answered"
    assert r1.text != r2.text


def test_two_sessions_do_not_cross_contaminate(store) -> None:
    runner = make_runner(store, [_text_response("a"), _text_response("b")])
    runner.run_turn("session-A", "alpha question")
    runner.run_turn("session-B", "beta question")
    svc = runner.session_service
    import asyncio

    async def others() -> list:
        a = await svc.get_session(app_name="finchat", user_id="ui", session_id="session-A")
        b = await svc.get_session(app_name="finchat", user_id="ui", session_id="session-B")
        return [a, b]

    sa, sb = asyncio.run(others())
    texts_a = " ".join(
        p.text or "" for e in sa.events if e.content for p in (e.content.parts or [])
    )
    assert "beta question" not in texts_a
    assert sa.id != sb.id
