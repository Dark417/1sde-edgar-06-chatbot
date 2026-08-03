"""Pull the gold tables out of Databricks into local Parquet.

This stands in for repo 4's S3 serving export, which has not run yet. Once the
files exist the chatbot never talks to Databricks again, so a Free Edition
quota shutdown cannot take the demo down (docs/20-agent-system.md section 0).

Uses the SQL Statement REST API directly rather than `databricks-sql-connector`
or the SDK -- repo 6 forbids both as runtime dependencies, and this script is
the only thing in the repo that touches Databricks at all.

    python scripts/export_gold.py            # refresh data/*.parquet
    python scripts/export_gold.py --check    # report row counts, write nothing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

DATA_DIR = Path(__file__).parents[1] / "data"

# The four gold base tables the tools consume. (Gold views are not exported;
# the tools' aggregates run over these.)
TABLES = [
    "gold.company_profile",
    "gold.financials_current",
    "gold.restatement_event",
    "gold.filing_activity_daily",
]


def _config() -> tuple[str, str, str]:
    host = os.environ.get("DBX_HOST", "").rstrip("/")
    token = os.environ.get("DBX_TOKEN", "")
    warehouse = os.environ.get("DBX_WAREHOUSE_ID", "")
    missing = [
        n
        for n, v in (("DBX_HOST", host), ("DBX_TOKEN", token), ("DBX_WAREHOUSE_ID", warehouse))
        if not v
    ]
    if missing:
        sys.exit(
            f"missing required environment variable(s): {', '.join(missing)}\n"
            "Set them from your gitignored local config; see docs/LOCAL-VALUES.example.md."
        )
    # Security (review finding #7): this request carries a live PAT in the
    # Authorization header. Force https and pin the host to Databricks; an
    # http:// value or a foreign host must die here, not send the token.
    host = host.replace("http://", "").replace("https://", "")
    from urllib.parse import urlparse

    parsed = urlparse(f"https://{host}")
    if not (parsed.hostname or "").endswith(".cloud.databricks.com"):
        sys.exit(
            f"DBX_HOST {parsed.hostname!r} is not a *.cloud.databricks.com host - refusing "
            "to send the PAT anywhere else."
        )
    return f"https://{parsed.hostname}", token, warehouse


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect would replay the Authorization header at whatever host the
    # 30x names. There is no legitimate redirect in this API; refuse them all.
    def redirect_request(self, *args, **kwargs):  # type: ignore[override]
        raise urllib.error.HTTPError(
            args[3].full_url if len(args) > 3 else "",
            302,
            "redirect refused (would leak the PAT)",
            {},
            None,
        )


_OPENER = urllib.request.build_opener(_NoRedirect)

_CHUNK_RE = None  # compiled lazily


def _open(req: urllib.request.Request, timeout: int = 120):
    try:
        return _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200] if e.fp else ""
        sys.exit(f"Databricks API error {e.code}: {body}")


def query(sql: str) -> tuple[list[str], list[list], list[str]]:
    """Run one statement, returning (column names, rows, column type names)."""
    import re

    global _CHUNK_RE
    if _CHUNK_RE is None:
        _CHUNK_RE = re.compile(r"^/api/2\.0/sql/statements/[A-Za-z0-9_-]+/result/chunks/\d+")
    host, token, warehouse = _config()
    body = json.dumps(
        {"statement": sql, "warehouse_id": warehouse, "wait_timeout": "50s", "format": "JSON_ARRAY"}
    ).encode()
    req = urllib.request.Request(
        f"{host}/api/2.0/sql/statements",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with _open(req) as r:
        payload = json.load(r)

    # A statement can come back PENDING; poll rather than assume it finished.
    statement_id = payload.get("statement_id")
    for _ in range(60):
        state = payload.get("status", {}).get("state")
        if state in ("SUCCEEDED", "FAILED", "CANCELED", "CLOSED"):
            break
        time.sleep(2)
        poll = urllib.request.Request(
            f"{host}/api/2.0/sql/statements/{statement_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with _open(poll) as r:
            payload = json.load(r)

    status = payload.get("status", {})
    if status.get("state") != "SUCCEEDED":
        sys.exit(f"query failed: {status.get('error', {}).get('message', status)}")

    schema = payload["manifest"]["schema"]["columns"]
    names = [c["name"] for c in schema]
    types = [c["type_name"] for c in schema]
    rows = payload.get("result", {}).get("data_array") or []

    # Pagination: large results arrive in chunks.
    next_chunk = payload.get("result", {}).get("next_chunk_internal_link")
    while next_chunk:
        if not _CHUNK_RE.match(next_chunk):
            sys.exit(f"unexpected chunk link shape, refusing to follow: {next_chunk[:80]!r}")
        req = urllib.request.Request(
            f"{host}{next_chunk}", headers={"Authorization": f"Bearer {token}"}
        )
        with _open(req) as r:
            chunk = json.load(r)
        rows.extend(chunk.get("data_array") or [])
        next_chunk = chunk.get("next_chunk_internal_link")

    return names, rows, types


def _coerce(value: str | None, type_name: str) -> object:
    """JSON_ARRAY returns everything as strings; restore the useful types.

    Numbers stay numbers so the tools can aggregate them, and dates stay
    strings because every consumer here formats them anyway and DuckDB will
    cast on demand. Nothing is silently defaulted: an unparseable number
    becomes None rather than 0, because a wrong zero in a financial answer is
    worse than a visible gap.
    """
    if value is None:
        return None
    if type_name in ("INT", "LONG", "SHORT", "BYTE"):
        try:
            return int(value)
        except ValueError:
            return None
    if type_name in ("DOUBLE", "FLOAT", "DECIMAL"):
        try:
            return float(value)
        except ValueError:
            return None
    if type_name == "BOOLEAN":
        return value.lower() == "true"
    if type_name in ("ARRAY", "MAP", "STRUCT"):
        # Databricks serialises these as JSON text. Leaving them as strings
        # silently breaks indexing downstream -- tickers[1] returned "[" rather
        # than "AAPL" -- so parse them into real values here.
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return value


def export(table: str) -> int:
    names, rows, types = query(f"SELECT * FROM edgar.{table}")
    columns: dict[str, list] = {n: [] for n in names}
    for row in rows:
        for name, raw, type_name in zip(names, row, types, strict=True):
            columns[name].append(_coerce(raw, type_name))

    out = DATA_DIR / f"{table.split('.')[-1]}.parquet"
    pq.write_table(pa.table(columns), out)
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report counts, write nothing")
    args = ap.parse_args()

    if args.check:
        for t in TABLES:
            _, rows, _ = query(f"SELECT count(*) FROM edgar.{t}")
            print(f"  {t:32} {rows[0][0]:>8} rows")
        return

    DATA_DIR.mkdir(exist_ok=True)
    counts = {}
    for t in TABLES:
        n = export(t)
        counts[t.split(".")[-1]] = n
        print(f"  {t:32} -> {n:>6} rows")

    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "databricks edgar.gold",
        "row_counts": counts,
    }
    (DATA_DIR / "_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"  manifest written to {DATA_DIR / '_manifest.json'}")


if __name__ == "__main__":
    main()
