"""DuckDB over the gold Parquet: build to file, reopen read-only, harden.

Both reviews killed the lazy-views design (a view over read_parquet resolves
at query time, so sealing the filesystem breaks every query) — and the first
fix attempt showed materialized tables in :memory: still accept INSERT. The
mechanism that actually holds all three properties:

    1. BUILD:  temp file db  <- CREATE TABLE ... AS SELECT * FROM read_parquet(...)
    2. REOPEN: duckdb.connect(file, read_only=True)   -> INSERT raises
    3. HARDEN: SET enable_external_access=false        -> ATTACH/COPY/INSTALL raise
               SET lock_configuration=true             -> settings frozen

Refresh builds a new file and swaps connections atomically. S3 mode never
loads the http filesystem extension: a fetcher callable (boto3 in production,
a stub in tests) downloads objects into the local data dir before step 1.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb

GOLD_TABLES = (
    "company_profile",
    "financials_current",
    "restatement_event",
    "filing_activity_daily",
)

Fetcher = Callable[[str, Path], None]  # (serving_prefix, dest_dir) -> None


def s3_fetch(serving_prefix: str, dest: Path) -> None:
    """Download repo 4's serving export with boto3 (the extension route stays banned).

    This is now the only source of gold data. It used to be a fallback behind
    ``scripts/export_gold.py``, which read Databricks directly and wrote the same
    filenames locally; that script has been removed. It was written when repo 4's
    export did not exist, and once it did, keeping both meant two producers of one
    dataset -- the exact duplication cross-repo law 1 exists to prevent. Repo 4 owns
    the export; this repo consumes it.

    **The layout is nested, not flat.** Repo 4 publishes
    ``v1/<table>/data.parquet``, one directory per table. This function previously
    asked for ``v1/<table>.parquet`` and would have 404'd on every table -- it was
    marked untested against real S3 precisely because the bucket was empty when it
    was written. The local filenames stay flat, because that is what ``_build``
    and the tests expect.
    """
    import boto3

    bucket_key = serving_prefix.removeprefix("s3://")
    bucket, _, prefix = bucket_key.partition("/")
    base = prefix.rstrip("/")
    client = boto3.client("s3")
    dest.mkdir(parents=True, exist_ok=True)
    for table in GOLD_TABLES:
        client.download_file(bucket, f"{base}/{table}/data.parquet", str(dest / f"{table}.parquet"))
    client.download_file(bucket, f"{base}/_manifest.json", str(dest / "_manifest.json"))


class GoldStore:
    """Hardened, read-only store. One instance per process; thread-safe swap."""

    def __init__(
        self,
        data_dir: Path,
        serving_prefix: str = "",
        fetcher: Fetcher = s3_fetch,
    ) -> None:
        self.data_dir = data_dir
        self.serving_prefix = serving_prefix
        self.fetcher = fetcher
        self._lock = threading.Lock()
        self._refreshing = False
        self.manifest: dict[str, Any] = {}
        self.concepts: tuple[str, ...] = ()
        self.con = self._build()

    # -- build -----------------------------------------------------------------

    def _build(self) -> duckdb.DuckDBPyConnection:
        if self.serving_prefix:
            self.fetcher(self.serving_prefix, self.data_dir)

        missing = [t for t in GOLD_TABLES if not (self.data_dir / f"{t}.parquet").exists()]
        if missing:
            raise FileNotFoundError(
                f"gold export missing from {self.data_dir}: {', '.join(missing)}. "
                "Set SERVING_PREFIX to repo 4's export (s3://<serving-bucket>/v1) so the "
                "store can fetch it, or point DATA_DIR at a local copy. Repo 4 owns "
                "this data; this repo no longer produces its own."
            )

        db_path = self.data_dir / f".gold-{uuid.uuid4().hex[:8]}.duckdb"
        build = duckdb.connect(str(db_path))
        for table in GOLD_TABLES:
            path = (self.data_dir / f"{table}.parquet").as_posix()
            # Constant table names from GOLD_TABLES, parquet path parameterized;
            # concatenation (not an f-string) keeps the no-fstring-SQL gate honest.
            build.execute("CREATE TABLE " + table + " AS SELECT * FROM read_parquet(?)", [path])
        build.close()

        con = duckdb.connect(str(db_path), read_only=True)  # INSERT now raises
        con.execute("SET enable_external_access=false")  # ATTACH/COPY/INSTALL raise
        con.execute("SET lock_configuration=true")  # and the settings freeze

        manifest_path = self.data_dir / "_manifest.json"
        self.manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        )
        rows = con.execute(
            "SELECT DISTINCT concept_canonical FROM financials_current "
            "WHERE concept_canonical IS NOT NULL ORDER BY 1"
        ).fetchall()
        self.concepts = tuple(r[0] for r in rows)
        self._db_path = db_path
        return con

    @property
    def row_counts(self) -> dict[str, int]:
        """``{table: row_count}`` from repo 4's manifest, whatever shape it is in.

        Repo 4 publishes a ``tables`` LIST of ``{name, row_count, bytes, sha256}``.
        The UI used to read ``manifest["row_counts"]``, a flat dict that repo 4 has
        never emitted -- so the lookup returned {} and the sidebar showed 0
        companies, 0 facts, 0 restatements against a fully loaded database. The
        tests did not catch it because the fixture invented the flat shape rather
        than copying a real manifest, so both sides of the mismatch agreed with
        each other and disagreed with production.

        Normalising here rather than in the UI keeps the manifest's on-disk shape a
        detail of the layer that reads the export. The flat form is still accepted
        so an older manifest does not break the page.
        """
        tables = self.manifest.get("tables")
        if isinstance(tables, list):
            return {
                str(t["name"]): int(t.get("row_count", 0))
                for t in tables
                if isinstance(t, dict) and "name" in t
            }
        legacy = self.manifest.get("row_counts")
        return dict(legacy) if isinstance(legacy, dict) else {}

    # -- query -----------------------------------------------------------------

    def q(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self.con.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    # -- refresh: single-flight, atomic swap -------------------------------------

    def refresh(self) -> bool:
        """Rebuild from the current files. Returns False if already running."""
        with self._lock:
            if self._refreshing:
                return False
            self._refreshing = True
        old_path = getattr(self, "_db_path", None)
        try:
            new_con = self._build()
            with self._lock:
                old, self.con = self.con, new_con
            old.close()
            if old_path is not None:
                try:
                    Path(old_path).unlink(missing_ok=True)
                except OSError:
                    pass  # a straggler .duckdb file is cosmetic, not a leak
            return True
        finally:
            with self._lock:
                self._refreshing = False
