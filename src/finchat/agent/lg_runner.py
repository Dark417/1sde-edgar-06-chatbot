"""LangGraph implementation of AgentRunner.

A StateGraph with two nodes (agent, tools) and conditional edges enforcing the
shared loop policy: <=MAX_TOOL_ROUNDS rounds, TOOL_OUTPUT_CHAR_CAP characters
of cumulative tool output. The model is injected (BaseChatModel), so loop
tests script it and never touch the network.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from finchat.agent.base import (
    MAX_TOOL_ROUNDS,
    TOOL_OUTPUT_CHAR_CAP,
    TurnResult,
    errored,
    finalize,
    too_broad,
)
from finchat.data.store import GoldStore
from finchat.prompts import (
    GATE_PROMPT,
    REFUSAL_TEXT,
    SYSTEM_PROMPT,
    new_nonce,
    wrap_tool_data,
)
from finchat.tools.registry import BY_NAME, ToolCallRecord, run_tool


class _State(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    rounds: int
    tool_chars: int
    records: list[ToolCallRecord]
    halt: str  # '', 'too_broad'


def _lc_tools() -> list[dict[str, Any]]:
    """Tool schemas for bind_tools, derived from the registry's Pydantic
    models — one source of truth (sec #12)."""
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.json_schema(),
        }
        for spec in BY_NAME.values()
    ]


class LangGraphRunner:
    def __init__(
        self,
        store: GoldStore,
        model: BaseChatModel,
        gate_model: BaseChatModel | None = None,
        topic_gate: bool = True,
    ) -> None:
        self.store = store
        self.model = model.bind_tools(_lc_tools())
        self.gate_model = gate_model
        self.topic_gate = topic_gate
        self.tokens = {"in": 0, "out": 0}
        self._sessions: dict[str, list[BaseMessage]] = {}
        self._graph = self._build()

    # -- graph -----------------------------------------------------------------

    def _agent_node(self, state: _State) -> dict[str, Any]:
        response = self.model.invoke(state["messages"])
        usage = getattr(response, "usage_metadata", None) or {}
        self.tokens["in"] += int(usage.get("input_tokens", 0))
        self.tokens["out"] += int(usage.get("output_tokens", 0))
        return {"messages": [response]}

    def _tools_node(self, state: _State) -> dict[str, Any]:
        last = state["messages"][-1]
        calls = getattr(last, "tool_calls", None) or []
        out_messages: list[BaseMessage] = []
        records = list(state["records"])
        chars = state["tool_chars"]
        nonce = new_nonce()
        for call in calls:
            payload, record = run_tool(self.store, call["name"], call.get("args") or {})
            records.append(record)
            body = wrap_tool_data(payload, nonce)
            chars += len(body)
            out_messages.append(ToolMessage(content=body, tool_call_id=call["id"]))
        halt = ""
        rounds = state["rounds"] + 1
        if chars > TOOL_OUTPUT_CHAR_CAP:
            halt = "too_broad"
        return {
            "messages": out_messages,
            "rounds": rounds,
            "tool_chars": chars,
            "records": records,
            "halt": halt,
        }

    def _route_after_agent(self, state: _State) -> str:
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None):
            # A 4th round would start -> halt with the fixed text instead.
            if state["rounds"] >= MAX_TOOL_ROUNDS:
                return "halt"
            return "tools"
        return END

    def _route_after_tools(self, state: _State) -> str:
        return "halt" if state["halt"] else "agent"

    def _halt_node(self, state: _State) -> dict[str, Any]:
        return {"halt": "too_broad"}

    def _build(self) -> Any:
        g: StateGraph = StateGraph(_State)
        g.add_node("agent", self._agent_node)
        g.add_node("tools", self._tools_node)
        g.add_node("halt", self._halt_node)
        g.set_entry_point("agent")
        g.add_conditional_edges(
            "agent", self._route_after_agent, {"tools": "tools", "halt": "halt", END: END}
        )
        g.add_conditional_edges(
            "tools", self._route_after_tools, {"agent": "agent", "halt": "halt"}
        )
        g.add_edge("halt", END)
        return g.compile()

    # -- gate --------------------------------------------------------------------

    def _gate(self, text: str) -> tuple[bool, bool]:
        """(in_scope, gate_error). Fails OPEN with the error recorded — a
        broken gate must not refuse legitimate questions (arch #5)."""
        if not self.topic_gate or self.gate_model is None:
            return True, False
        try:
            reply = self.gate_model.invoke(
                [HumanMessage(content=GATE_PROMPT.format(message=text[:500]))]
            )
            verdict = str(getattr(reply, "content", "")).strip().upper()
            if verdict.startswith("OUT"):
                return False, False
            if verdict.startswith("IN"):
                return True, False
            return True, True  # unparseable -> open + flagged
        except Exception:
            return True, True

    # -- the protocol --------------------------------------------------------------

    def run_turn(self, session_id: str, text: str) -> TurnResult:
        result = TurnResult(text="")
        in_scope, gate_error = self._gate(text)
        result.gate_error = gate_error
        if not in_scope:
            result.text = REFUSAL_TEXT
            result.outcome = "refused"
            return finalize(result)

        history = self._sessions.setdefault(session_id, [SystemMessage(content=SYSTEM_PROMPT)])
        history.append(HumanMessage(content=text))
        t0 = dict(self.tokens)
        try:
            state: _State = {
                "messages": list(history),
                "rounds": 0,
                "tool_chars": 0,
                "records": [],
                "halt": "",
            }
            final = self._graph.invoke(state)
        except Exception:
            return errored(result)

        result.tools_called = final["records"]
        result.tokens_in = self.tokens["in"] - t0["in"]
        result.tokens_out = self.tokens["out"] - t0["out"]

        if final["halt"] == "too_broad":
            # Do not keep the truncated exchange in history.
            history.pop()
            return too_broad(result)

        last = final["messages"][-1]
        answer = last.content if isinstance(last.content, str) else json.dumps(last.content)
        result.text = str(answer)
        history.extend(final["messages"][len(history) :])
        return finalize(result)
