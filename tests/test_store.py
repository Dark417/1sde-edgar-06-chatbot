"""V7 / SEC1 / G6: materialize-then-harden, proven — not asserted vacuously."""

from __future__ import annotations

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
    with pytest.raises(FileNotFoundError, match="export_gold.py"):
        GoldStore(tmp_path)


def test_manifest_and_derived_concepts(store: GoldStore) -> None:
    assert store.manifest["row_counts"]["company_profile"] == 3
    assert "revenue_total" in store.concepts
    assert "eps_basic" in store.concepts


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
