"""Bedrock tool-calling loop.

The model plans; the tools compute. This file owns the conversation with
Bedrock and the dispatch of tool calls, and contains no SQL and no arithmetic.

Deliberately a plain loop rather than a framework: the whole thing is ~120
lines, every step is visible, and the tool trace it emits is what the UI shows
the user. A framework would hide exactly the part worth seeing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import boto3

from chatbot import tools as T

# Model selection is probed, not assumed. This account currently cannot invoke
# any Anthropic model -- Bedrock returns "Model use case details have not been
# submitted for this account" until the Anthropic use-case form is completed in
# the console. Amazon Nova needs no such form and supports tool use, so it is
# the working fallback.
#
# The list is in quality order and the first invocable one wins, so the app
# upgrades itself to Claude automatically once the form is submitted. Set
# BEDROCK_MODEL_ID to pin one explicitly and skip probing.
MODEL_CANDIDATES = [
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.amazon.nova-pro-v1:0",
    "us.amazon.nova-lite-v1:0",
]
REGION = os.environ.get("AWS_REGION", "us-east-2")
MAX_TOOL_CALLS = 6

SYSTEM_PROMPT = """You are a research assistant over a dataset of SEC EDGAR filings and XBRL \
financial facts for 8 US public companies.

HOW YOU WORK
- You never calculate. Every number you state must come from a tool result.
  Do not add, average, rank, or convert figures yourself - there is a tool for
  each of those. If no tool gives you the number, say so.
- Always call resolve_company first for a company-specific question, to get the
  cik. If it returns more than one match, ask which one - never guess.
- Call get_data_coverage when asked what you can do, or when a question falls
  outside the data, so you can say precisely what is missing.

WHAT THE DATA IS
- 8 companies only. 11 financial concepts (revenue_total, net_income,
  operating_income, gross_profit, assets_total, liabilities_total,
  equity_total, cash_and_equivalents, eps_basic, eps_diluted,
  shares_outstanding).
- A restatement is a figure a company reported and later corrected in an
  amended filing. This dataset's distinguishing feature is that it keeps both
  the original and the corrected value, so restatements are searchable.
- There is no filing text - only structured figures. No news, no prices, no
  analyst data, nothing outside these 8 companies.

HOW TO ANSWER
- Lead with the answer, then the supporting figures. Use markdown tables when
  showing more than two numbers.
- Always give the unit and the period for a figure. "Revenue was 364.4B USD for
  the year ending 2021-09-25", never a bare number.
- Format large numbers readably (364,357,000,000 USD -> 364.4B USD) but never
  change the value.
- Cite the accession number for figures that have one.
- materiality_band (immaterial/notable/material) is a PRODUCT HEURISTIC chosen
  for this project, not an accounting standard. Say so whenever you use it.
- If a tool reports truncated results or caveats, pass them on to the user.

WHAT YOU REFUSE
- Investment advice of any kind. No buy/sell/hold, no "is this a good
  investment", no predictions. Offer the underlying figures instead.
- Anything outside the dataset. Say what you do have rather than a bare
  "I can't help".
"""

# Tool schemas for Bedrock. Descriptions come from the tool docstrings so there
# is one source of truth for what a tool does.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "resolve_company",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "company name or ticker"}},
            "required": ["query"],
        },
    },
    {"name": "list_companies", "input_schema": {"type": "object", "properties": {}}},
    {
        "name": "get_company_profile",
        "input_schema": {
            "type": "object",
            "properties": {"cik": {"type": "string"}},
            "required": ["cik"],
        },
    },
    {
        "name": "get_financials",
        "input_schema": {
            "type": "object",
            "properties": {
                "cik": {"type": "string"},
                "concept": {"type": "string", "description": "one canonical concept, or omit for all"},
                "fiscal_year": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["cik"],
        },
    },
    {
        "name": "get_restatements",
        "input_schema": {
            "type": "object",
            "properties": {
                "cik": {"type": "string", "description": "omit for all companies"},
                "materiality_band": {"type": "string", "enum": ["immaterial", "notable", "material"]},
                "concept": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    },
    {"name": "restatement_summary", "input_schema": {"type": "object", "properties": {}}},
    {
        "name": "compare_companies",
        "input_schema": {
            "type": "object",
            "properties": {
                "concept": {"type": "string"},
                "fiscal_year": {"type": "integer"},
            },
            "required": ["concept"],
        },
    },
    {
        "name": "get_filing_activity",
        "input_schema": {
            "type": "object",
            "properties": {"form_type": {"type": "string"}, "limit": {"type": "integer"}},
        },
    },
    {"name": "get_data_coverage", "input_schema": {"type": "object", "properties": {}}},
]


def _bedrock_tool_config() -> dict[str, Any]:
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": spec["name"],
                    "description": (T.TOOLS[spec["name"]].__doc__ or "").strip(),
                    "inputSchema": {"json": spec["input_schema"]},
                }
            }
            for spec in TOOL_SPECS
        ]
    }


@dataclass
class TraceEntry:
    tool: str
    args: dict[str, Any]
    row_count: int
    caveats: list[str] = field(default_factory=list)
    error: str | None = None


def pick_model(client: Any) -> str:
    """First candidate this account can actually invoke.

    Probing costs four tokens and one round trip at startup, and turns an
    opaque mid-conversation ResourceNotFoundException into a startup fact the
    UI can display.
    """
    pinned = os.environ.get("BEDROCK_MODEL_ID")
    if pinned:
        return pinned
    errors = []
    for candidate in MODEL_CANDIDATES:
        try:
            client.converse(
                modelId=candidate,
                messages=[{"role": "user", "content": [{"text": "ok"}]}],
                inferenceConfig={"maxTokens": 5},
            )
            return candidate
        except Exception as exc:
            errors.append(f"{candidate.split('.')[-1]}: {type(exc).__name__}")
    raise RuntimeError(
        "no invocable Bedrock model in this account/region. Tried: "
        + "; ".join(errors)
        + ". Enable model access in the Bedrock console (us-east-2)."
    )


class Agent:
    def __init__(self, store: T.GoldStore, model_id: str | None = None, region: str = REGION) -> None:
        self.store = store
        self.client = boto3.client("bedrock-runtime", region_name=region)
        self.model_id = model_id or pick_model(self.client)

    def _run_tool(self, name: str, args: dict[str, Any]) -> tuple[str, TraceEntry]:
        fn = T.TOOLS.get(name)
        if fn is None:
            return json.dumps({"error": f"no such tool: {name}"}), TraceEntry(name, args, 0, error="unknown tool")
        try:
            result: T.ToolResult = fn(self.store, **args)
        except Exception as exc:  # surfaced to the model so it can recover, and to the trace
            msg = f"{type(exc).__name__}: {exc}"
            return json.dumps({"error": msg}), TraceEntry(name, args, 0, error=msg)
        return result.to_model_json(), TraceEntry(name, args, result.row_count, result.caveats)

    def chat(self, messages: list[dict[str, Any]]) -> tuple[str, list[TraceEntry], list[dict]]:
        """Run one turn to completion. Returns (answer, trace, updated messages)."""
        trace: list[TraceEntry] = []

        for _ in range(MAX_TOOL_CALLS + 1):
            response = self.client.converse(
                modelId=self.model_id,
                messages=messages,
                system=[{"text": SYSTEM_PROMPT}],
                toolConfig=_bedrock_tool_config(),
                inferenceConfig={"maxTokens": 2000, "temperature": 0.0},
            )
            out = response["output"]["message"]
            messages.append(out)

            if response.get("stopReason") != "tool_use":
                text = "".join(b.get("text", "") for b in out.get("content", []))
                return text, trace, messages

            tool_results = []
            for block in out.get("content", []):
                if "toolUse" not in block:
                    continue
                use = block["toolUse"]
                payload, entry = self._run_tool(use["name"], use.get("input", {}) or {})
                trace.append(entry)
                tool_results.append(
                    {
                        "toolResult": {
                            "toolUseId": use["toolUseId"],
                            "content": [{"text": payload}],
                            **({"status": "error"} if entry.error else {}),
                        }
                    }
                )
            messages.append({"role": "user", "content": tool_results})

        # Hitting the cap is reported, not hidden -- an answer built from a
        # truncated tool sequence is not trustworthy without saying so.
        return (
            "I reached the limit of tool calls for one question. Please narrow it down.",
            trace,
            messages,
        )
