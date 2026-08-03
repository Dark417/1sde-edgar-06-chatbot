"""V1/V2/V3/V8/G1 over fixture Parquet, through the registry (the real path)."""

from __future__ import annotations

import pytest

from finchat.data.store import GoldStore
from finchat.tools.registry import BY_NAME, run_tool


def test_every_tool_happy_path(store: GoldStore) -> None:
    happy_args = {
        "list_companies": {},
        "search_companies": {"q": "alpha"},
        "get_company_profile": {"cik": "1"},
        "get_company_financials": {"cik": "1"},
        "compare_companies": {"concept": "revenue_total"},
        "get_restatements": {},
        "restatement_summary": {},
        "get_filing_activity": {},
        "get_data_coverage": {},
    }
    assert set(happy_args) == set(BY_NAME)
    for name, args in happy_args.items():
        payload, record = run_tool(store, name, args)
        assert record.error_kind is None, f"{name}: {record.error_kind}"
        assert set(payload) == {"rows", "row_count", "truncated", "caveats", "citations"}


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("search_companies", {"q": "x"}),  # too short
        ("search_companies", {"q": "alpha", "limit": 999}),  # over cap
        ("search_companies", {"q": "alpha", "limit": 0}),  # under cap (sec#12)
        ("get_company_profile", {"cik": "not-digits"}),
        ("get_company_financials", {"cik": "1", "limit": -1}),
        ("get_restatements", {"band": "gigantic"}),
    ],
)
def test_param_rejection(store: GoldStore, name: str, args: dict) -> None:
    payload, record = run_tool(store, name, args)
    assert record.error_kind == "invalid"
    assert payload == {"error": "tool_failed", "kind": "invalid"}


def test_unknown_tool_is_invalid(store: GoldStore) -> None:
    payload, record = run_tool(store, "drop_all_tables", {})
    assert record.error_kind == "invalid"


def test_unknown_concept_rejected_by_derived_enum(store: GoldStore) -> None:
    payload, _ = run_tool(store, "compare_companies", {"concept": "ebitda_adjusted"})
    assert "unknown_concept" in payload["caveats"]
    assert payload["row_count"] == 0


def test_concept_present_in_data_always_accepted(store: GoldStore) -> None:
    for concept in store.concepts:
        payload, record = run_tool(
            store, "get_company_financials", {"cik": "1", "concept": concept}
        )
        assert record.error_kind is None


def test_caveats_are_fixed_codes_never_user_text(store: GoldStore) -> None:
    needle = "zzz-definitely-not-in-any-code"
    payload, _ = run_tool(store, "search_companies", {"q": needle})
    assert "no_match" in payload["caveats"]
    assert not any(needle in c for c in payload["caveats"])


def test_citations_on_financials(store: GoldStore) -> None:
    payload, _ = run_tool(store, "get_company_financials", {"cik": "1"})
    assert payload["citations"]
    assert all("sec.gov" in c["url"] for c in payload["citations"])


def test_free_text_fields_clipped(store: GoldStore) -> None:
    payload, _ = run_tool(store, "list_companies", {})
    for row in payload["rows"]:
        for v in row.values():
            if isinstance(v, str):
                assert len(v) <= 200


def test_no_fstring_sql_in_tools() -> None:
    """SEC2 as a test: zero f-strings containing SELECT in the tool module."""
    import pathlib

    src = pathlib.Path("src/finchat/tools/impl.py").read_text(encoding="utf-8")
    assert 'f"' not in src or "SELECT" not in [seg for seg in src.split('f"')[1:]], (
        "f-string SQL found"
    )
    for chunk in src.split('f"')[1:]:
        assert "SELECT" not in chunk.split('"')[0].upper()
