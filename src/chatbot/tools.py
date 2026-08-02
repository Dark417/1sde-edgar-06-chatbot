"""The tool layer: every number the assistant says originates here.

The governing rule of this repo (AGENTS.md rule 2): **the model never computes
a number.** It picks a tool and arguments; the tool runs fixed, reviewed SQL and
returns rows. Sums, rankings, deltas and date filtering happen in SQL, and the
model only writes prose around results it did not calculate.

That is not fussiness. `financial_fact` is bitemporal, so a generated query that
forgets to scope `period_type` cheerfully compares a quarter against a fiscal
year, and one that forgets `accession_number` double-counts a restated fact.
Neither errors. Both produce a plausible wrong number, which in a financial
answer is the worst possible failure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

DATA_DIR = Path(__file__).parents[2] / "data"
ROW_CAP = 200

_SEC_FILING_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=&dateb=&owner=include&count=40"


@dataclass
class ToolResult:
    """What a tool hands back. Never a bare list.

    `caveats` and `truncated` exist so the model can be honest about limits it
    cannot otherwise see -- a silently truncated ranking is a wrong ranking.
    """

    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool = False
    caveats: list[str] = field(default_factory=list)
    citations: list[dict[str, str]] = field(default_factory=list)

    def to_model_json(self) -> str:
        return json.dumps(
            {
                "rows": self.rows,
                "row_count": self.row_count,
                "truncated": self.truncated,
                "caveats": self.caveats,
                "citations": self.citations,
            },
            default=str,
        )


class GoldStore:
    """DuckDB over the exported gold Parquet. Read-only, process-lifetime."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.data_dir = data_dir
        self.con = duckdb.connect(database=":memory:")
        for name in (
            "company_profile",
            "financials_current",
            "restatement_event",
            "filing_activity_daily",
        ):
            path = data_dir / f"{name}.parquet"
            if not path.exists():
                raise FileNotFoundError(
                    f"{path} missing. Run scripts/export_gold.py first -- the app "
                    "reads exported Parquet, never Databricks directly."
                )
            self.con.execute(
                f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{path.as_posix()}')"
            )
        manifest_path = data_dir / "_manifest.json"
        self.manifest: dict[str, Any] = (
            json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        )

    def q(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        cur = self.con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _cap(rows: list[dict], caveats: list[str] | None = None) -> ToolResult:
    caveats = caveats or []
    truncated = len(rows) > ROW_CAP
    if truncated:
        caveats.append(f"Only the first {ROW_CAP} rows are shown; more matched.")
    return ToolResult(rows[:ROW_CAP], len(rows[:ROW_CAP]), truncated, caveats)


# ---------------------------------------------------------------------------
# Tools. Docstrings are written FOR THE MODEL: what it answers, what it does
# not, and the units of anything returned.
# ---------------------------------------------------------------------------


def resolve_company(store: GoldStore, query: str) -> ToolResult:
    """Find a company by name or ticker. ALWAYS call this first for any
    company-specific question, to turn a name into a cik.

    Returns cik, company_name, ticker, industry. If more than one match comes
    back, ask the user which they meant instead of guessing -- answering
    confidently about the wrong company is worse than a clarifying question.
    Does not answer anything about financials.
    """
    like = f"%{query.lower().strip()}%"
    rows = store.q(
        """
        SELECT cik, company_name, tickers[1] AS ticker, sic_description AS industry,
               filing_count, restatement_count
        FROM company_profile
        WHERE lower(company_name) LIKE ?
           OR lower(coalesce(tickers[1],'')) LIKE ?
        ORDER BY company_name
        """,
        (like, like),
    )
    caveats = []
    if not rows:
        caveats.append(
            f"No company matches '{query}'. The dataset covers only 8 companies; "
            "call list_companies to see them."
        )
    elif len(rows) > 1:
        caveats.append("Multiple matches - ask the user which one they mean before continuing.")
    return ToolResult(rows, len(rows), caveats=caveats)


def list_companies(store: GoldStore) -> ToolResult:
    """List every company in the dataset. Use for 'what companies do you have',
    'what can you tell me about', or when resolve_company finds nothing.
    """
    rows = store.q(
        """
        SELECT cik, company_name, tickers[1] AS ticker, sic_description AS industry,
               filing_count, restatement_count
        FROM company_profile ORDER BY company_name
        """
    )
    return ToolResult(rows, len(rows), caveats=["This is the complete universe: 8 companies."])


def get_company_profile(store: GoldStore, cik: str) -> ToolResult:
    """Profile for one company: industry, incorporation, fiscal year end,
    tickers, exchanges, filing counts and how many restatements it has.
    Requires a cik from resolve_company. Returns no financial figures.
    """
    rows = store.q(
        """
        SELECT cik, company_name, sic_description AS industry, entity_type,
               state_of_incorporation, fiscal_year_end, tickers, exchanges,
               filing_count, first_filed_date, last_filed_date, restatement_count
        FROM company_profile WHERE cik = ?
        """,
        (cik,),
    )
    return ToolResult(rows, len(rows))


def get_financials(
    store: GoldStore,
    cik: str,
    concept: str | None = None,
    fiscal_year: int | None = None,
    limit: int = 40,
) -> ToolResult:
    """Reported financial figures for one company, latest assertion per period.

    concept must be one of: revenue_total, net_income, operating_income,
    gross_profit, assets_total, liabilities_total, equity_total,
    cash_and_equivalents, eps_basic, eps_diluted, shares_outstanding.
    Omit it to get all concepts.

    Every row carries `unit` (usually USD) and the period it covers. `value` is
    already in that unit -- do not rescale it. `was_restated` true means this
    figure was later corrected; use get_restatements for the detail.
    """
    sql = """
        SELECT cik, company_name, concept_canonical AS concept, unit, value,
               period_start, period_end, period_type, fiscal_year, fiscal_period,
               form_type, accession_number, filed_date, was_restated
        FROM financials_current WHERE cik = ?
    """
    params: list[Any] = [cik]
    if concept:
        sql += " AND concept_canonical = ?"
        params.append(concept)
    if fiscal_year:
        sql += " AND fiscal_year = ?"
        params.append(fiscal_year)
    sql += " ORDER BY period_end DESC, concept_canonical LIMIT ?"
    params.append(min(limit, ROW_CAP))

    rows = store.q(sql, tuple(params))
    result = _cap(rows)
    result.citations = [
        {"accession_number": r["accession_number"], "url": _SEC_FILING_URL.format(cik=cik)}
        for r in rows[:10]
        if r.get("accession_number")
    ]
    if not rows:
        result.caveats.append(
            "No figures for that combination. The dataset holds 11 concepts; try omitting "
            "concept or fiscal_year."
        )
    return result


def get_restatements(
    store: GoldStore,
    cik: str | None = None,
    materiality_band: str | None = None,
    concept: str | None = None,
    limit: int = 25,
) -> ToolResult:
    """Restatements: figures a company reported, then later corrected.

    This is the dataset's centrepiece. Each row gives the original value and
    the restated value, the two filings that asserted them, the change in
    absolute terms (delta_abs) and percent (delta_pct), and how many days
    passed (days_to_restatement).

    materiality_band is one of immaterial (<1%), notable (1-5%), material
    (>5%). IMPORTANT: those thresholds are a product heuristic chosen for this
    project, NOT an accounting standard - always say so when you report a band.

    Omit cik for a cross-company view. Sorted by largest percentage change.
    """
    sql = """
        SELECT cik, company_name, concept_canonical AS concept, unit,
               period_end, period_type, original_value, restated_value,
               delta_abs, delta_pct, materiality_band, days_to_restatement,
               original_accession_number, original_form_type, original_filed_date,
               restated_accession_number, restated_form_type, restated_filed_date
        FROM restatement_event WHERE 1=1
    """
    params: list[Any] = []
    if cik:
        sql += " AND cik = ?"
        params.append(cik)
    if materiality_band:
        sql += " AND materiality_band = ?"
        params.append(materiality_band)
    if concept:
        sql += " AND concept_canonical = ?"
        params.append(concept)
    sql += " ORDER BY abs(delta_pct) DESC NULLS LAST LIMIT ?"
    params.append(min(limit, ROW_CAP))

    rows = store.q(sql, tuple(params))
    result = _cap(
        rows,
        [
            "materiality_band is a product heuristic (immaterial <1%, notable 1-5%, "
            "material >5%), not an accounting standard."
        ],
    )
    result.citations = [
        {
            "accession_number": f"{r['original_accession_number']} -> {r['restated_accession_number']}",
            "url": _SEC_FILING_URL.format(cik=r["cik"]),
        }
        for r in rows[:10]
    ]
    return result


def restatement_summary(store: GoldStore) -> ToolResult:
    """Aggregate restatement statistics across all companies: counts by
    materiality band, by company, and by concept. Use for 'overall', 'summary',
    'which company restates most', 'what gets restated most often'.
    """
    by_band = store.q(
        "SELECT materiality_band, count(*) AS events, round(avg(abs(delta_pct))*100,2) AS avg_abs_change_pct "
        "FROM restatement_event GROUP BY 1 ORDER BY events DESC"
    )
    by_company = store.q(
        "SELECT company_name, cik, count(*) AS events, "
        "sum(CASE WHEN materiality_band='material' THEN 1 ELSE 0 END) AS material_events "
        "FROM restatement_event GROUP BY 1,2 ORDER BY events DESC"
    )
    by_concept = store.q(
        "SELECT concept_canonical AS concept, count(*) AS events "
        "FROM restatement_event GROUP BY 1 ORDER BY events DESC LIMIT 10"
    )
    total = store.q("SELECT count(*) AS total, round(avg(days_to_restatement),1) AS avg_days FROM restatement_event")
    return ToolResult(
        rows=[{"totals": total, "by_band": by_band, "by_company": by_company, "by_concept": by_concept}],
        row_count=1,
        caveats=[
            "materiality_band is a product heuristic, not an accounting standard.",
            "Covers 8 companies only.",
        ],
    )


def compare_companies(store: GoldStore, concept: str, fiscal_year: int | None = None) -> ToolResult:
    """Compare one financial measure across every company in the dataset.

    Use for 'who has the highest revenue', 'compare net income', rankings and
    league tables. concept must be one of the eleven canonical concepts.
    Returns one row per company with its most recent value for that concept,
    largest first. Values carry their unit; do not compare across units.
    """
    sql = """
        WITH ranked AS (
          SELECT cik, company_name, concept_canonical, unit, value, period_end,
                 fiscal_year, fiscal_period, accession_number,
                 row_number() OVER (PARTITION BY cik ORDER BY period_end DESC) AS rn
          FROM financials_current
          WHERE concept_canonical = ?
    """
    params: list[Any] = [concept]
    if fiscal_year:
        sql += " AND fiscal_year = ?"
        params.append(fiscal_year)
    sql += """
        )
        SELECT cik, company_name, concept_canonical AS concept, unit, value,
               period_end, fiscal_year, fiscal_period, accession_number
        FROM ranked WHERE rn = 1 ORDER BY value DESC NULLS LAST
    """
    rows = store.q(sql, tuple(params))
    result = _cap(rows)
    if rows and len({r["unit"] for r in rows}) > 1:
        result.caveats.append("Rows use different units - do not rank across them.")
    if not rows:
        result.caveats.append(f"No company reports '{concept}'. Check the concept name.")
    return result


def get_filing_activity(store: GoldStore, form_type: str | None = None, limit: int = 30) -> ToolResult:
    """Filing volume over time: how many filings were made on each date, how
    many were amendments, and how many distinct companies filed. Use for
    trends, 'how active', 'when do they file'. Not company-specific.
    """
    sql = "SELECT filed_date, base_form_type, filing_count, amendment_count, distinct_cik_count FROM filing_activity_daily WHERE 1=1"
    params: list[Any] = []
    if form_type:
        sql += " AND base_form_type = ?"
        params.append(form_type.upper())
    sql += " ORDER BY filed_date DESC LIMIT ?"
    params.append(min(limit, ROW_CAP))
    rows = store.q(sql, tuple(params))
    return _cap(rows)


def get_data_coverage(store: GoldStore) -> ToolResult:
    """What this dataset does and does not contain: companies, concepts, date
    range, row counts, freshness. Call this when asked what you can answer, or
    when a question falls outside the data, so you can say precisely what is
    missing rather than guessing.
    """
    counts = store.q(
        "SELECT (SELECT count(*) FROM company_profile) AS companies, "
        "(SELECT count(*) FROM financials_current) AS financial_facts, "
        "(SELECT count(*) FROM restatement_event) AS restatements, "
        "(SELECT count(*) FROM filing_activity_daily) AS activity_days"
    )
    concepts = store.q("SELECT DISTINCT concept_canonical FROM financials_current ORDER BY 1")
    dates = store.q("SELECT min(filed_date) AS earliest, max(filed_date) AS latest FROM financials_current")
    return ToolResult(
        rows=[
            {
                "counts": counts[0] if counts else {},
                "concepts": [c["concept_canonical"] for c in concepts],
                "filing_date_range": dates[0] if dates else {},
                "exported_at": store.manifest.get("generated_at"),
            }
        ],
        row_count=1,
        caveats=[
            "Only 8 companies. No filing text, only structured XBRL figures.",
            "Data is a point-in-time export, not live.",
        ],
    )


# Registry: name -> (callable, needs_store)
TOOLS = {
    "resolve_company": resolve_company,
    "list_companies": list_companies,
    "get_company_profile": get_company_profile,
    "get_financials": get_financials,
    "get_restatements": get_restatements,
    "restatement_summary": restatement_summary,
    "compare_companies": compare_companies,
    "get_filing_activity": get_filing_activity,
    "get_data_coverage": get_data_coverage,
}
