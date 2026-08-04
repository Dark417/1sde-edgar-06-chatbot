"""V7 / SEC1 / G6: materialize-then-harden, proven — not asserted vacuously."""

from __future__ import annotations

import pathlib
import threading
from pathlib import Path

import duckdb
import pytest

from finchat.data.store import GoldStore


def test_queries_work_after_hardening(store: GoldStore) -> None:
    rows = store.q("SELECT count(*) AS n FROM company_profile")
    assert rows[0]["n"] == 3


def test_external_access_is_off(store: GoldStore) -> None:
    value = store.q("SELECT current_setting('enable_external_access') AS v")[0]["v"]
    assert str(value).lower() == "false"


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO company_profile (cik) VALUES ('x')",
        "ATTACH 'other.duckdb' AS other",
        "COPY company_profile TO 'out.csv'",
        "INSTALL httpfs",
    ],
)
def test_writes_and_escapes_raise(store: GoldStore, sql: str) -> None:
    with pytest.raises(duckdb.Error):
        store.q(sql)


def test_reading_new_parquet_after_hardening_raises(store: GoldStore, fixture_dir: Path) -> None:
    # The filesystem is sealed: even a legitimate parquet path must fail now.
    path = (fixture_dir / "company_profile.parquet").as_posix()
    with pytest.raises(duckdb.Error):
        store.q(f"SELECT * FROM read_parquet('{path}')")


def test_missing_data_dir_names_the_fix(tmp_path: Path) -> None:
    """The error must name repo 4's export, not the script that used to produce one."""
    with pytest.raises(FileNotFoundError, match="SERVING_PREFIX"):
        GoldStore(tmp_path)


def test_s3_fetch_asks_for_repo4s_nested_layout(tmp_path: Path) -> None:
    """Repo 4 publishes v1/<table>/data.parquet, not v1/<table>.parquet.

    This asked for the flat form and would have 404'd on every table. It was never
    caught because the bucket was empty when it was written, so "untested against
    real S3" was doing a lot of work in the docstring.
    """
    asked: list[str] = []

    class FakeClient:
        def download_file(self, bucket: str, key: str, dest: str) -> None:
            asked.append(key)
            pathlib.Path(dest).write_bytes(b"")

    import finchat.data.store as store_mod

    original = store_mod.boto3 if hasattr(store_mod, "boto3") else None
    import sys
    import types

    fake_boto3 = types.SimpleNamespace(client=lambda _svc: FakeClient())
    sys.modules["boto3"] = fake_boto3
    try:
        store_mod.s3_fetch("s3://a-bucket/v1", tmp_path)
    finally:
        if original is None:
            sys.modules.pop("boto3", None)

    assert "v1/financials_current/data.parquet" in asked
    assert "v1/_manifest.json" in asked
    assert not any(k.endswith("financials_current.parquet") for k in asked)


def test_manifest_and_derived_concepts(store: GoldStore) -> None:
    assert store.row_counts["company_profile"] == 3
    assert "revenue_total" in store.concepts
    assert "eps_basic" in store.concepts


def test_row_counts_reads_repo4s_table_list(store: GoldStore) -> None:
    """The sidebar's numbers, against the shape repo 4 actually publishes.

    Guards the bug this replaced: every count silently 0 on a loaded database,
    because the UI read a `row_counts` key that no real manifest contains.
    """
    assert store.row_counts == {
        "company_profile": 3,
        "financials_current": 4,
        "restatement_event": 1,
        "filing_activity_daily": 2,
    }


def test_row_counts_survives_a_manifest_it_cannot_read(store: GoldStore) -> None:
    """Missing or malformed manifest costs the metrics, never the page."""
    for bad in ({}, {"tables": "nonsense"}, {"tables": [{"row_count": 5}]}):
        store.manifest = bad
        assert store.row_counts == {}
    store.manifest = {"row_counts": {"company_profile": 9}}  # legacy flat form
    assert store.row_counts == {"company_profile": 9}


def test_refresh_single_flight(store: GoldStore) -> None:
    results: list[bool] = []
    blocker = threading.Event()
    original_build = store._build

    def slow_build():
        blocker.wait(timeout=2)
        return original_build()

    store._build = slow_build  # type: ignore[method-assign]
    t1 = threading.Thread(target=lambda: results.append(store.refresh()))
    t1.start()
    import time

    time.sleep(0.1)
    results.append(store.refresh())  # second caller must be rejected
    blocker.set()
    t1.join()
    assert sorted(results) == [False, True]
    assert store.q("SELECT count(*) AS n FROM company_profile")[0]["n"] == 3


def test_s3_mode_uses_injected_fetcher(fixture_dir: Path) -> None:
    calls: list[tuple[str, Path]] = []

    def fake_fetch(prefix: str, dest: Path) -> None:
        calls.append((prefix, dest))  # files already exist in fixture_dir

    GoldStore(fixture_dir, serving_prefix="s3://bucket/v1", fetcher=fake_fetch)
    assert calls == [("s3://bucket/v1", fixture_dir)]
