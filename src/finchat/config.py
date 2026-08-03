"""Settings: env -> SSM -> default-or-fail naming the key (project rule 3).

DEPLOY_ENV drives the fail direction everywhere (arch review #8): the string
"local" is the ONLY value that fails open; unset or anything else is treated
as cloud and fails closed. run.bat sets DEPLOY_ENV=local explicitly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SSM_PREFIX = "/edgar-lakehouse/chat"
PROBE_SENTINEL = "probe"  # SSM forbids empty values; this means "probe candidates"

# Quality-ordered; first invocable wins. Claude entries activate themselves the
# day the Bedrock Anthropic use-case form is approved (see SETUP-CREDENTIALS.md).
MODEL_CANDIDATES = (
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.amazon.nova-pro-v1:0",
    "us.amazon.nova-lite-v1:0",
)


def is_local() -> bool:
    return os.environ.get("DEPLOY_ENV", "") == "local"


def _ssm_get(name: str) -> str | None:
    """One SSM read; None when unreachable. Callers decide the fail direction."""
    try:
        import boto3

        client = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-2"))
        return str(client.get_parameter(Name=name)["Parameter"]["Value"])
    except Exception:
        return None


def resolve(key_env: str, key_ssm: str | None, default: str | None) -> str:
    """env -> SSM -> default; no default -> fail naming both keys."""
    value = os.environ.get(key_env)
    if value:
        return value
    if key_ssm:
        value = _ssm_get(key_ssm)
        if value is not None:
            return value
    if default is not None:
        return default
    raise RuntimeError(
        f"missing configuration: set env {key_env}" + (f" or SSM {key_ssm}" if key_ssm else "")
    )


@dataclass
class Settings:
    agent_impl: str = "langgraph"
    data_dir: Path = field(default_factory=lambda: Path("data"))
    serving_prefix: str = ""  # s3://... once repo 4 ships; untested until then
    model_main: str = PROBE_SENTINEL
    model_cheap: str = PROBE_SENTINEL
    token_budget_day: int = 200_000
    topic_gate: bool = True
    region: str = "us-east-2"

    @classmethod
    def load(cls) -> Settings:
        s = cls()
        s.agent_impl = resolve("AGENT_IMPL", None, "langgraph")
        s.data_dir = Path(resolve("DATA_DIR", None, "data"))
        s.serving_prefix = resolve("SERVING_PREFIX", None, "")
        s.model_main = resolve("BEDROCK_MODEL_ID", f"{SSM_PREFIX}/model/main", PROBE_SENTINEL)
        s.model_cheap = resolve("BEDROCK_MODEL_CHEAP", f"{SSM_PREFIX}/model/cheap", PROBE_SENTINEL)
        s.token_budget_day = int(
            resolve("TOKEN_BUDGET_DAY", f"{SSM_PREFIX}/token_budget_day", "200000")
        )
        s.topic_gate = resolve("TOPIC_GATE", None, "on") == "on"
        s.region = resolve("AWS_REGION", None, "us-east-2")
        return s


def pick_model(client: Any, pinned: str) -> str:
    """First invocable candidate; a 5-token probe per attempt.

    Turns an opaque mid-conversation ResourceNotFoundException (Anthropic
    gating appeared mid-session once) into a startup fact the UI displays.
    """
    if pinned and pinned != PROBE_SENTINEL:
        return pinned
    errors: list[str] = []
    for candidate in MODEL_CANDIDATES:
        try:
            client.converse(
                modelId=candidate,
                messages=[{"role": "user", "content": [{"text": "ok"}]}],
                inferenceConfig={"maxTokens": 5},
            )
            return candidate
        except Exception as exc:
            errors.append(f"{candidate.rsplit('.', 1)[-1]}: {type(exc).__name__}")
    raise RuntimeError(
        "no invocable Bedrock model in this account/region. Tried: "
        + "; ".join(errors)
        + ". See docs/SETUP-CREDENTIALS.md."
    )
