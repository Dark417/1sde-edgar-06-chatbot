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

# Quality-ordered; first invocable wins. The Anthropic use-case form was
# submitted and approved on 2026-08-02, so the Claude entries are live in
# this account (us-east-2); the Nova entries remain as a working fallback for a
# fresh account that has not been through that form yet.
#
# Sonnet leads rather than Haiku: the model's job here is to pick tools and
# write careful prose around numbers it must not alter, and Sonnet is markedly
# better at both the multi-step tool plans (resolve -> query -> compare) and at
# honouring the "never state a figure without unit and period" rule. The SQL
# does the arithmetic either way, so the extra cost buys judgement, not maths.
MODEL_CANDIDATES = (
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
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
    # Sidebar links. Config, not code, so a slide deck or write-up can be
    # attached after the fact without a deploy: set LINK_* env vars, or the
    # matching SSM parameter under /edgar-lakehouse/chat/link/. A link left
    # empty is not rendered at all, so an unfinished deck never ships a dead
    # URL -- which is worse than no link.
    links: dict[str, str] = field(default_factory=dict)

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
        s.links = {
            label: url
            for label, env, ssm in (
                ("Source code", "LINK_REPO", f"{SSM_PREFIX}/link/repo"),
                ("How it works", "LINK_ARCHITECTURE", f"{SSM_PREFIX}/link/architecture"),
                ("Demo / slides", "LINK_DEMO", f"{SSM_PREFIX}/link/demo"),
                ("Write-up", "LINK_WRITEUP", f"{SSM_PREFIX}/link/writeup"),
            )
            if (url := resolve(env, ssm, "").strip())
        }
        return s


# botocore raises these when the caller cannot be authenticated at all. They
# say nothing about which models exist, so they must not be reported as model
# access. Matched by class name to avoid importing botocore just for this.
_CREDENTIAL_ERRORS = frozenset(
    {
        "TokenRetrievalError",
        "UnauthorizedSSOTokenError",
        "SSOTokenLoadError",
        "NoCredentialsError",
        "CredentialRetrievalError",
        "ExpiredTokenException",
        "InvalidClientTokenId",
        "UnrecognizedClientException",
    }
)


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
            name = type(exc).__name__
            # A credential failure is NOT a model-access failure, and reporting
            # it as one sends you to the Bedrock console to fix something that
            # is not broken. Observed for real: an expired SSO token produced
            # "no invocable Bedrock model in this account/region", listing four
            # models that were all perfectly available.
            #
            # Credential errors are identical for every candidate, so the first
            # one is the whole story -- fail immediately rather than probing
            # three more times to collect the same message.
            if name in _CREDENTIAL_ERRORS:
                raise RuntimeError(
                    f"AWS credentials are not usable ({name}): {exc}. "
                    "This is a sign-in problem, not model access - the models "
                    "may be fine. Run `aws sso login --profile edgar-sso` "
                    "(or double-click D:\\aws-login.bat) and restart. On a "
                    "server, check the instance role."
                ) from exc
            errors.append(f"{candidate.rsplit('.', 1)[-1]}: {name}")
    raise RuntimeError(
        "no invocable Bedrock model in this account/region. Tried: "
        + "; ".join(errors)
        + ". Credentials worked, so this is model access: see "
        "docs/SETUP-CREDENTIALS.md section 2."
    )
