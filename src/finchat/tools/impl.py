"""The nine tools. Every number the assistant says originates here.

Invariants (each tested):
- params validated by Pydantic BEFORE any SQL bind; limit is conint(1..cap) so
  a model-supplied -1 cannot widen anything (sec review #12);
- every statement parameterized — no f-string SQL (grep-gated);
- caveats are FIXED CODES, never interpolated user text (sec #3): the
  renderer restates queries, the model never sees them echoed as "data";
- payload serialization capped by characters, not rows (sec #12).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

from finchat.data.store import GoldStore

ROW_CAP = 50
PAYLOAD_CHAR_CAP = 12_000
FREE_TEXT_CAP = 200
SEC_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"

Cik = Annotated[str, StringConstraints(pattern=r"^\d{1,10}$")]
Query = Annotated[str, StringConstraints(min_length=2, max_length=50)]
LimitS = Annotated[int, Field(ge=1, le=20)]
LimitM = Annotated[int, Field(ge=1, le=ROW_CAP)]
LimitL = Annotated[int, Field(ge=1, le=60)]
Band = Literal["immaterial", "notable", "material"]


@dataclass
class ToolResult:
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool = False
    caveats: list[str] = field(default_factory=list)  # fixed codes only
    citations: list[dict[str, str]] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        """Model-facing dict, char-capped. Truncation is reported, never silent."""
        out: dict[str, Any] = {
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "caveats": list(self.caveats),
            "citations": self.citations,
        }
        raw = json.dumps(out, default=str)
        while len(raw) > PAYLOAD_CHAR_CAP and out["rows"]:
            out["rows"] = out["rows"][: max(1, len(out["rows"]) // 2)]
            out["truncated"] = True
            if "payload_capped" not in out["caveats"]:
                out["caveats"].append("payload_capped")
            raw = json.dumps(out, default=str)
        return out


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > FREE_TEXT_CAP:
        return value[:FREE_TEXT_CAP]
    return value


def _rows(store: GoldStore, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [{k: _clip(v) for k, v in r.items()} for r in store.q(sql, params)]


# --------------------------------------------------------------------------
# Param models (the registry derives both Bedrock and LangChain schemas from
# these, so the declared schema can never drift from what validates — sec #12)
# --------------------------------------------------------------------------


class NoParams(BaseModel):
    pass


class SearchParams(BaseModel):
    q: Query
    limit: LimitS = 10


class CikParams(BaseModel):
    cik: Cik


class FinancialsParams(BaseModel):
    cik: Cik
    concept: str | None = None
    fiscal_year: int | None = Field(default=None, ge=1990, le=2100)
    limit: LimitM = 40


class CompareParams(BaseModel):
    concept: str
    fiscal_year: int | None = Field(default=None, ge=1990, le=2100)


class RestatementParams(BaseModel):
    cik: Cik | None = None
    band: Band | None = None
    concept: str | None = None
    limit: LimitM = 25


class ActivityParams(BaseModel):
    form_type: str | None = Field(default=None, max_length=12)
    limit: LimitL = 30


def _check_concept(store: GoldStore, concept: str | None) -> list[str]:
    """Concept validity comes from the DATA (derived enum, arch #7)."""
    if concept is not None and concept not in store.concepts:
        return ["unknown_concept"]
    return []


# --------------------------------------------------------------------------
# The tools. Docstrings are written for the model.
# --------------------------------------------------------------------------


def list_companies(store: GoldStore, p: NoParams) -> ToolResult:
    """List every company in the dataset (the complete universe of 8), with
    cik, name, ticker, industry and how many restatements each has. Use for
    'what companies do you have' or when a company search finds nothing."""
    rows = _rows(
        store,
        "SELECT cik, company_name, tickers[1] AS ticker, sic_description AS industry,"
        " filing_count, restatement_count FROM company_profile ORDER BY company_name",
    )
    return ToolResult(rows, len(rows), caveats=["complete_universe"])


def search_companies(store: GoldStore, p: SearchParams) -> ToolResult:
    """Find companies by name or ticker fragment. Call this FIRST for any
    company-specific question to get the cik. Multiple matches -> ask the user
    which one; zero matches -> the company is not in this 8-company dataset."""
    like = f"%{p.q.lower().strip()}%"
    rows = _rows(
        store,
        "SELECT cik, company_name, tickers[1] AS ticker, sic_description AS industry"
        " FROM company_profile WHERE lower(company_name) LIKE ?"
        " OR lower(coalesce(tickers[1], '')) LIKE ? ORDER BY company_name LIMIT ?",
        (like, like, p.limit),
    )
    caveats = []
    if not rows:
        caveats.append("no_match")
    elif len(rows) > 1:
        caveats.append("ambiguous")
    return ToolResult(rows, len(rows), caveats=caveats)


def get_company_profile(store: GoldStore, p: CikParams) -> ToolResult:
    """Profile for one company by cik: industry, incorporation state, fiscal
    year end, tickers, filing counts, restatement count. No financial figures."""
    rows = _rows(
        store,
        "SELECT cik, company_name, sic_description AS industry, entity_type,"
        " state_of_incorporation, fiscal_year_end, tickers, exchanges, filing_count,"
        " first_filed_date, last_filed_date, restatement_count"
        " FROM company_profile WHERE cik = ?",
        (p.cik.zfill(10),),
    )
    return ToolResult(rows, len(rows), caveats=[] if rows else ["not_found"])


def get_company_financials(store: GoldStore, p: FinancialsParams) -> ToolResult:
    """Reported figures for one company, latest assertion per period. Every
    row carries unit and period; value is already in that unit — never rescale.
    was_restated=true means the figure was later corrected (get_restatements
    for detail). concept omitted = all concepts."""
    bad = _check_concept(store, p.concept)
    if bad:
        return ToolResult([], 0, caveats=bad + [f"valid_concepts:{','.join(store.concepts)}"])
    sql = (
        "SELECT cik, company_name, concept_canonical AS concept, unit, value,"
        " period_start, period_end, period_type, fiscal_year, fiscal_period,"
        " form_type, accession_number, filed_date, was_restated"
        " FROM financials_current WHERE cik = ?"
    )
    params: list[Any] = [p.cik.zfill(10)]
    if p.concept:
        sql += " AND concept_canonical = ?"
        params.append(p.concept)
    if p.fiscal_year:
        sql += " AND fiscal_year = ?"
        params.append(p.fiscal_year)
    sql += " ORDER BY period_end DESC, concept_canonical LIMIT ?"
    params.append(p.limit)
    rows = _rows(store, sql, tuple(params))
    citations = [
        {"accession_number": str(r["accession_number"]), "url": SEC_URL.format(cik=r["cik"])}
        for r in rows[:10]
        if r.get("accession_number")
    ]
    return ToolResult(rows, len(rows), caveats=[] if rows else ["no_data"], citations=citations)


def compare_companies(store: GoldStore, p: CompareParams) -> ToolResult:
    """Rank every company on one measure (latest value per company, largest
    first). Use for 'who has the highest revenue' and any cross-company
    comparison. Values carry units; never rank across different units."""
    bad = _check_concept(store, p.concept)
    if bad:
        return ToolResult([], 0, caveats=bad + [f"valid_concepts:{','.join(store.concepts)}"])
    sql = (
        "WITH ranked AS ("
        " SELECT cik, company_name, concept_canonical, unit, value, period_end,"
        "  fiscal_year, fiscal_period, accession_number,"
        "  row_number() OVER (PARTITION BY cik ORDER BY period_end DESC) AS rn"
        " FROM financials_current WHERE concept_canonical = ?"
    )
    params: list[Any] = [p.concept]
    if p.fiscal_year:
        sql += " AND fiscal_year = ?"
        params.append(p.fiscal_year)
    sql += (
        ") SELECT cik, company_name, concept_canonical AS concept, unit, value,"
        " period_end, fiscal_year, fiscal_period, accession_number"
        " FROM ranked WHERE rn = 1 ORDER BY value DESC NULLS LAST LIMIT ?"
    )
    params.append(ROW_CAP)
    rows = _rows(store, sql, tuple(params))
    caveats = []
    if rows and len({r["unit"] for r in rows}) > 1:
        caveats.append("mixed_units")
    if not rows:
        caveats.append("no_data")
    return ToolResult(rows, len(rows), caveats=caveats)


def get_restatements(store: GoldStore, p: RestatementParams) -> ToolResult:
    """Restatements: figures a company reported and later corrected. Each row
    has original and restated values, both filings, delta_abs, delta_pct, and
    materiality_band — which is a PRODUCT HEURISTIC (immaterial <1 percent,
    notable 1-5, material >5), not an accounting standard; always say so.
    Omit cik for the cross-company view; sorted by largest percent change."""
    bad = _check_concept(store, p.concept)
    if bad:
        return ToolResult([], 0, caveats=bad)
    sql = (
        "SELECT cik, company_name, concept_canonical AS concept, unit, period_end,"
        " period_type, original_value, restated_value, delta_abs, delta_pct,"
        " materiality_band, days_to_restatement, original_accession_number,"
        " original_filed_date, restated_accession_number, restated_filed_date"
        " FROM restatement_event WHERE 1=1"
    )
    params: list[Any] = []
    if p.cik:
        sql += " AND cik = ?"
        params.append(p.cik.zfill(10))
    if p.band:
        sql += " AND materiality_band = ?"
        params.append(p.band)
    if p.concept:
        sql += " AND concept_canonical = ?"
        params.append(p.concept)
    sql += " ORDER BY abs(delta_pct) DESC NULLS LAST LIMIT ?"
    params.append(p.limit)
    rows = _rows(store, sql, tuple(params))
    citations = [
        {
            "accession_number": (
                str(r["original_accession_number"]) + " -> " + str(r["restated_accession_number"])
            ),
            "url": SEC_URL.format(cik=r["cik"]),
        }
        for r in rows[:10]
    ]
    return ToolResult(rows, len(rows), caveats=["materiality_is_heuristic"], citations=citations)


def restatement_summary(store: GoldStore, p: NoParams) -> ToolResult:
    """Aggregate restatement statistics: totals, by materiality band, by
    company, by concept. Use for 'overall', 'summary', 'who restates most'."""
    totals = _rows(
        store,
        "SELECT count(*) AS total, round(avg(days_to_restatement), 1) AS avg_days"
        " FROM restatement_event",
    )
    by_band = _rows(
        store,
        "SELECT materiality_band, count(*) AS events FROM restatement_event"
        " GROUP BY 1 ORDER BY events DESC",
    )
    by_company = _rows(
        store,
        "SELECT company_name, cik, count(*) AS events,"
        " sum(CASE WHEN materiality_band = 'material' THEN 1 ELSE 0 END) AS material_events"
        " FROM restatement_event GROUP BY 1, 2 ORDER BY events DESC",
    )
    by_concept = _rows(
        store,
        "SELECT concept_canonical AS concept, count(*) AS events"
        " FROM restatement_event GROUP BY 1 ORDER BY events DESC LIMIT 10",
    )
    row = {"totals": totals, "by_band": by_band, "by_company": by_company, "by_concept": by_concept}
    return ToolResult([row], 1, caveats=["materiality_is_heuristic", "complete_universe"])


def get_filing_activity(store: GoldStore, p: ActivityParams) -> ToolResult:
    """Filing volume by date: filings, amendments, distinct companies per day.
    For trends and 'how active'. Not company-specific."""
    sql = (
        "SELECT filed_date, base_form_type, filing_count, amendment_count,"
        " distinct_cik_count FROM filing_activity_daily WHERE 1=1"
    )
    params: list[Any] = []
    if p.form_type:
        sql += " AND base_form_type = ?"
        params.append(p.form_type.upper())
    sql += " ORDER BY filed_date DESC LIMIT ?"
    params.append(p.limit)
    rows = _rows(store, sql, tuple(params))
    return ToolResult(rows, len(rows), caveats=[] if rows else ["no_data"])


def get_data_coverage(store: GoldStore, p: NoParams) -> ToolResult:
    """What the dataset contains and does not: companies, concepts, date
    range, counts, export freshness. Call when asked what you can answer or
    when a question falls outside the data, so the refusal can be precise."""
    counts = _rows(
        store,
        "SELECT (SELECT count(*) FROM company_profile) AS companies,"
        " (SELECT count(*) FROM financials_current) AS financial_facts,"
        " (SELECT count(*) FROM restatement_event) AS restatements,"
        " (SELECT count(*) FROM filing_activity_daily) AS activity_days",
    )
    dates = _rows(
        store,
        "SELECT min(filed_date) AS earliest, max(filed_date) AS latest FROM financials_current",
    )
    row = {
        "counts": counts[0] if counts else {},
        "concepts": list(store.concepts),
        "filing_date_range": dates[0] if dates else {},
        "exported_at": store.manifest.get("generated_at"),
    }
    return ToolResult([row], 1, caveats=["complete_universe", "point_in_time_export"])
