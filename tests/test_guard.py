"""V6/G5: boundaries exact, budget persisted, fail direction correct."""

from __future__ import annotations

from pathlib import Path

import pytest

from finchat.guard.limits import MSGS_PER_MIN, RESERVE_PER_CALL, Guard


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture()
def guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Guard:
    monkeypatch.setenv("DEPLOY_ENV", "local")
    return Guard(tmp_path, budget_day=100_000, ssm_get=lambda name: "true", clock=FakeClock())


def test_minute_bucket_boundary(guard: Guard) -> None:
    for i in range(MSGS_PER_MIN):
        assert guard.check("s1").allowed, f"message {i + 1} should pass"
        guard.settle(10)
    assert guard.check("s1").reason == "limit"
    # a minute later the bucket drains
    guard.clock.t += 61  # type: ignore[attr-defined]
    assert guard.check("s1").allowed


def test_daily_counter_is_per_session_and_persisted(tmp_path: Path, monkeypatch) -> None:
    """The 40-round conversation cap bites before the 60/day cap in a single
    process, so the daily cap is exercised across RESTARTS - which is exactly
    the property sec review #6 demanded: the counter is file-backed."""
    monkeypatch.setenv("DEPLOY_ENV", "local")
    clock = FakeClock()
    allowed, last_reason = 0, ""
    for _restart in range(3):  # 3 guards, same dir: 40 + 20 + 0
        g = Guard(tmp_path, budget_day=10_000_000, ssm_get=lambda n: "true", clock=clock)
        for _ in range(40):
            clock.t += 7  # stay under the minute bucket
            d = g.check("s1")
            if d.allowed:
                allowed += 1
                g.settle(1)
            else:
                last_reason = d.reason
    assert allowed == 60  # MSGS_PER_DAY, enforced across restarts
    assert last_reason == "daily"


def test_budget_reserved_before_call_and_survives_restart(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DEPLOY_ENV", "local")
    clock = FakeClock()
    budget = RESERVE_PER_CALL * 2  # room for exactly two reservations
    g = Guard(tmp_path, budget_day=budget, ssm_get=lambda n: "true", clock=clock)
    assert g.check("a").allowed
    g.settle(RESERVE_PER_CALL)
    clock.t += 7
    assert g.check("b").allowed
    g.settle(RESERVE_PER_CALL)
    clock.t += 7
    assert g.check("c").reason == "budget"
    # restart does not refill (sec #6)
    g2 = Guard(tmp_path, budget_day=budget, ssm_get=lambda n: "true", clock=clock)
    clock.t += 7
    assert g2.check("d").reason == "budget"


def test_kill_switch_and_cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DEPLOY_ENV", "local")
    clock = FakeClock()
    value = {"v": "true"}
    g = Guard(tmp_path, budget_day=100_000, ssm_get=lambda n: value["v"], clock=clock)
    assert g.check("s").allowed
    value["v"] = "false"
    clock.t += 5  # inside the 30s cache: still allowed
    assert g.check("s").allowed
    clock.t += 31  # cache expired: killed
    assert g.check("s").reason == "killed"


def test_fail_direction(tmp_path: Path, monkeypatch) -> None:
    clock = FakeClock()
    # SSM unreachable + DEPLOY_ENV=local -> open
    monkeypatch.setenv("DEPLOY_ENV", "local")
    g = Guard(tmp_path, budget_day=100_000, ssm_get=lambda n: None, clock=clock)
    assert g.check("s").allowed
    # SSM unreachable + env UNSET -> closed (the dangerous direction is never default)
    monkeypatch.delenv("DEPLOY_ENV", raising=False)
    g2 = Guard(tmp_path / "x", budget_day=100_000, ssm_get=lambda n: None, clock=clock)
    assert g2.check("s").reason == "killed"
