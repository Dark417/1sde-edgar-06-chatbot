"""Google ADK implementation of AgentRunner (optional extra: pip install .[adk]).

Bridge per design §4.1: a custom BaseLlm subclass adapts any model — the
scripted fake in loop tests, LiteLLM->Bedrock live — into ADK's llm interface;
one asyncio.run per turn (fresh loop each time: Streamlit reruns make a
long-lived loop a Windows trap); one InMemorySessionService per process.

Telemetry is disabled before litellm can phone home (sec review #8):
LITELLM_LOCAL_MODEL_COST_MAP stops the model-price fetch at import;
litellm.telemetry = False stops the rest.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from typing import Any

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("ADK_DISABLE_TELEMETRY", "true")

from google.adk.agents import LlmAgent  # noqa: E402
from google.adk.models.base_llm import BaseLlm  # noqa: E402
from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types as genai_types  # noqa: E402

from finchat.agent.base import (  # noqa: E402
    MAX_TOOL_ROUNDS,
    TOOL_OUTPUT_CHAR_CAP,
    TurnResult,
    errored,
    finalize,
    too_broad,
)
from finchat.data.store import GoldStore  # noqa: E402
from finchat.prompts import GATE_PROMPT, REFUSAL_TEXT, SYSTEM_PROMPT  # noqa: E402
from finchat.tools.registry import BY_NAME, ToolCallRecord, run_tool  # noqa: E402

APP_NAME = "finchat"


class TooBroadHalt(Exception):
    pass


class ScriptableLlm(BaseLlm):
    """Adapts a simple callable interface into ADK's BaseLlm.

    The callable receives the running conversation as ADK's LlmRequest and
    returns an LlmResponse. Production wraps LiteLLM; loop tests wrap a script.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "allow"}

    def __init__(self, respond: Any, **kwargs: Any) -> None:
        super().__init__(model="scriptable", **kwargs)
        object.__setattr__(self, "_respond", respond)

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        yield self._respond(llm_request)


def litellm_bedrock(model_id: str) -> Any:
    """Production model: LiteLLM against Bedrock, telemetry off."""
    import litellm

    litellm.telemetry = False
    from google.adk.models.lite_llm import LiteLlm

    return LiteLlm(model=f"bedrock/{model_id}")


class AdkRunner:
    def __init__(
        self,
        store: GoldStore,
        model: Any,  # BaseLlm (ScriptableLlm in tests, LiteLlm live)
        gate_model: Any | None = None,
        topic_gate: bool = True,
    ) -> None:
        self.store = store
        self.gate_model = gate_model
        self.topic_gate = topic_gate
        self._records: list[ToolCallRecord] = []
        self._chars = 0
        self._rounds = 0

        def make_tool(spec: Any) -> Any:
            def _tool(**kwargs: Any) -> dict[str, Any]:
                payload, record = run_tool(self.store, spec.name, kwargs)
                self._records.append(record)
                import json as _json

                self._chars += len(_json.dumps(payload, default=str))
                self._rounds += 1
                if self._rounds > MAX_TOOL_ROUNDS or self._chars > TOOL_OUTPUT_CHAR_CAP:
                    raise TooBroadHalt()
                return payload

            _tool.__name__ = spec.name
            _tool.__doc__ = spec.description
            return _tool

        self.agent = LlmAgent(
            name="finchat",
            model=model,
            instruction=SYSTEM_PROMPT,
            tools=[make_tool(s) for s in BY_NAME.values()],
        )
        self.session_service = InMemorySessionService()
        self.runner = Runner(
            agent=self.agent, app_name=APP_NAME, session_service=self.session_service
        )

    def _gate(self, text: str) -> tuple[bool, bool]:
        if not self.topic_gate or self.gate_model is None:
            return True, False
        try:
            reply = self.gate_model(GATE_PROMPT.format(message=text[:500]))
            verdict = str(reply).strip().upper()
            if verdict.startswith("OUT"):
                return False, False
            if verdict.startswith("IN"):
                return True, False
            return True, True
        except Exception:
            return True, True

    async def _turn(self, session_id: str, text: str) -> str:
        session = await self.session_service.get_session(
            app_name=APP_NAME, user_id="ui", session_id=session_id
        )
        if session is None:
            await self.session_service.create_session(
                app_name=APP_NAME, user_id="ui", session_id=session_id
            )
        content = genai_types.Content(role="user", parts=[genai_types.Part(text=text)])
        final = ""
        async for event in self.runner.run_async(
            user_id="ui", session_id=session_id, new_message=content
        ):
            if event.is_final_response() and event.content and event.content.parts:
                final = "".join(p.text or "" for p in event.content.parts)
        return final

    def run_turn(self, session_id: str, text: str) -> TurnResult:
        result = TurnResult(text="")
        in_scope, gate_error = self._gate(text)
        result.gate_error = gate_error
        if not in_scope:
            result.text = REFUSAL_TEXT
            result.outcome = "refused"
            return finalize(result)

        self._records = []
        self._chars = 0
        self._rounds = 0
        try:
            answer = asyncio.run(self._turn(session_id, text))
        except TooBroadHalt:
            result.tools_called = self._records
            return too_broad(result)
        except Exception as exc:
            if isinstance(exc.__cause__, TooBroadHalt) or "TooBroadHalt" in str(type(exc)):
                result.tools_called = self._records
                return too_broad(result)
            return errored(result)

        result.tools_called = self._records
        result.text = answer
        return finalize(result)
