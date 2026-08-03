"""Single source of truth for the tool surface.

Both runners derive their tool schemas from the same Pydantic models that
validate at call time, so the declared schema cannot drift from what is
enforced (security review #12). Nothing outside this registry is callable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from finchat.data.store import GoldStore
from finchat.tools import impl
from finchat.tools.impl import ToolResult


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: Any
    params: type[BaseModel]

    @property
    def description(self) -> str:
        return (self.fn.__doc__ or "").strip()

    def json_schema(self) -> dict[str, Any]:
        schema = self.params.model_json_schema()
        schema.pop("title", None)
        return schema


REGISTRY: tuple[ToolSpec, ...] = (
    ToolSpec("list_companies", impl.list_companies, impl.NoParams),
    ToolSpec("search_companies", impl.search_companies, impl.SearchParams),
    ToolSpec("get_company_profile", impl.get_company_profile, impl.CikParams),
    ToolSpec("get_company_financials", impl.get_company_financials, impl.FinancialsParams),
    ToolSpec("compare_companies", impl.compare_companies, impl.CompareParams),
    ToolSpec("get_restatements", impl.get_restatements, impl.RestatementParams),
    ToolSpec("restatement_summary", impl.restatement_summary, impl.NoParams),
    ToolSpec("get_filing_activity", impl.get_filing_activity, impl.ActivityParams),
    ToolSpec("get_data_coverage", impl.get_data_coverage, impl.NoParams),
)

BY_NAME = {t.name: t for t in REGISTRY}


@dataclass
class ToolCallRecord:
    """Trace entry. Free-text args are hashed, not logged verbatim — the
    search query IS the user's question (sec #12)."""

    tool: str
    args_display: dict[str, str]
    row_count: int
    caveats: list[str]
    error_kind: str | None = None


_FREE_TEXT_ARGS = {"q"}


def _display_args(name: str, args: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in args.items():
        if k in _FREE_TEXT_ARGS:
            out[k] = "sha256:" + hashlib.sha256(str(v).encode()).hexdigest()[:8]
        else:
            out[k] = str(v)
    return out


def run_tool(
    store: GoldStore, name: str, raw_args: dict[str, Any]
) -> tuple[dict[str, Any], ToolCallRecord]:
    """Validate -> execute -> envelope. The model NEVER sees exception text;
    failures map to the fixed taxonomy (arch #4 / sec #2)."""
    from finchat.prompts import tool_error_payload

    spec = BY_NAME.get(name)
    if spec is None:
        return tool_error_payload("invalid"), ToolCallRecord(name, {}, 0, [], error_kind="invalid")
    try:
        params = spec.params.model_validate(raw_args or {})
    except ValidationError:
        return tool_error_payload("invalid"), ToolCallRecord(
            name, _display_args(name, raw_args or {}), 0, [], error_kind="invalid"
        )
    try:
        result: ToolResult = spec.fn(store, params)
    except FileNotFoundError:
        return tool_error_payload("not_found"), ToolCallRecord(
            name, _display_args(name, raw_args or {}), 0, [], error_kind="not_found"
        )
    except Exception:
        return tool_error_payload("denied"), ToolCallRecord(
            name, _display_args(name, raw_args or {}), 0, [], error_kind="denied"
        )
    record = ToolCallRecord(
        name, _display_args(name, raw_args or {}), result.row_count, list(result.caveats)
    )
    return result.payload(), record


def payload_chars(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, default=str))
