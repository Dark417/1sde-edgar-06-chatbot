"""The seam: AgentRunner protocol and TurnResult.

The UI and every test consume TurnResult only; neither knows which
implementation produced it. Shared loop policy (rounds, char cap, outcome
mapping, hygiene) lives here so the two runners cannot diverge on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from finchat.prompts import (
    ERROR_TEXT,
    OUT_OF_SCOPE_TOKEN,
    REFUSAL_TEXT,
    TOO_BROAD_TEXT,
    redact,
    sanitize_markdown,
)
from finchat.tools.registry import ToolCallRecord

MAX_TOOL_ROUNDS = 3
TOOL_OUTPUT_CHAR_CAP = 32_000


@dataclass
class TurnResult:
    text: str
    tools_called: list[ToolCallRecord] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    outcome: str = "answered"  # answered|refused|too_broad|budget|killed|error
    gate_error: bool = False


class AgentRunner(Protocol):
    def run_turn(self, session_id: str, text: str) -> TurnResult: ...


def finalize(result: TurnResult) -> TurnResult:
    """The one exit door: outcome mapping + output hygiene, shared by both
    runners so fixed texts stay byte-identical (G9) and nothing unredacted
    leaves (sec #2, #5)."""
    stripped = result.text.strip()
    if stripped == OUT_OF_SCOPE_TOKEN or stripped.endswith(OUT_OF_SCOPE_TOKEN):
        result.text = REFUSAL_TEXT
        result.outcome = "refused"
    result.text = sanitize_markdown(redact(result.text))
    return result


def too_broad(result: TurnResult) -> TurnResult:
    result.text = TOO_BROAD_TEXT
    result.outcome = "too_broad"
    return finalize(result)


def errored(result: TurnResult) -> TurnResult:
    result.text = ERROR_TEXT
    result.outcome = "error"
    return finalize(result)
