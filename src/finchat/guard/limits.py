"""Rate limits, the persisted daily budget, and the kill switch.

Fail direction (arch review #8): DEPLOY_ENV=local is the ONLY fail-open value.
Unset or anything else -> cloud rules -> fail closed. run.bat sets local.

The day counter and token budget persist to data/.budget-<date>.json with
atomic replace (sec review #6): a process restart does not refill the wallet.
Budget is RESERVED before each model call and settled after, so one long turn
cannot overshoot the remainder.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from finchat.config import SSM_PREFIX, is_local

MSGS_PER_MIN = 10
MSGS_PER_DAY = 60
MAX_ROUNDS_PER_CONVERSATION = 40
RESERVE_PER_CALL = 4_000  # tokens reserved ahead of a round; settled to actual
KILL_CACHE_SECONDS = 30


@dataclass
class GuardDecision:
    allowed: bool
    reason: str = ""  # '', 'limit', 'daily', 'budget', 'killed', 'conversation'


class _BudgetFile:
    """UTC-day token/message ledger, atomic-replace on every write."""

    def __init__(self, data_dir: Path, budget_day: int) -> None:
        self.data_dir = data_dir
        self.budget_day = budget_day
        self._lock = threading.Lock()

    def _path(self) -> Path:
        return self.data_dir / f".budget-{datetime.now(UTC):%Y-%m-%d}.json"

    def _read(self) -> dict[str, Any]:
        try:
            data: dict[str, Any] = json.loads(self._path().read_text(encoding="utf-8"))
            return data
        except (FileNotFoundError, ValueError):
            return {"tokens_used": 0, "tokens_reserved": 0, "messages": {}}

    def _write(self, state: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, self._path())

    def reserve(self) -> bool:
        with self._lock:
            s = self._read()
            if s["tokens_used"] + s["tokens_reserved"] + RESERVE_PER_CALL > self.budget_day:
                return False
            s["tokens_reserved"] += RESERVE_PER_CALL
            self._write(s)
            return True

    def settle(self, actual_tokens: int) -> None:
        with self._lock:
            s = self._read()
            s["tokens_reserved"] = max(0, s["tokens_reserved"] - RESERVE_PER_CALL)
            s["tokens_used"] += max(0, actual_tokens)
            self._write(s)

    def count_message(self, session_id: str) -> int:
        with self._lock:
            s = self._read()
            n = int(s["messages"].get(session_id, 0)) + 1
            s["messages"][session_id] = n
            self._write(s)
            return n


class Guard:
    def __init__(
        self,
        data_dir: Path,
        budget_day: int,
        ssm_get: Callable[[str], str | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.budget = _BudgetFile(data_dir, budget_day)
        self.clock = clock
        self._minute: dict[str, deque[float]] = {}
        self._rounds: dict[str, int] = {}
        self._kill_cached: tuple[float, bool] | None = None
        self._ssm_get = ssm_get  # injectable for tests

    # -- kill switch ---------------------------------------------------------

    def _enabled(self) -> bool:
        now = self.clock()
        if self._kill_cached and now - self._kill_cached[0] < KILL_CACHE_SECONDS:
            return self._kill_cached[1]
        if self._ssm_get is not None:
            value = self._ssm_get(f"{SSM_PREFIX}/enabled")
        else:
            from finchat.config import _ssm_get

            value = _ssm_get(f"{SSM_PREFIX}/enabled")
        if value is None:
            # Unreachable SSM: local demos stay alive; anything else fails closed.
            enabled = is_local()
        else:
            enabled = str(value).lower() == "true"
        self._kill_cached = (now, enabled)
        return enabled

    # -- the per-message check -------------------------------------------------

    def check(self, session_id: str) -> GuardDecision:
        if not self._enabled():
            return GuardDecision(False, "killed")

        bucket = self._minute.setdefault(session_id, deque())
        now = self.clock()
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= MSGS_PER_MIN:
            return GuardDecision(False, "limit")

        rounds = self._rounds.get(session_id, 0)
        if rounds >= MAX_ROUNDS_PER_CONVERSATION:
            return GuardDecision(False, "conversation")

        if self.budget.count_message(session_id) > MSGS_PER_DAY:
            return GuardDecision(False, "daily")

        if not self.budget.reserve():
            return GuardDecision(False, "budget")

        bucket.append(now)
        self._rounds[session_id] = rounds + 1
        return GuardDecision(True)

    def settle(self, actual_tokens: int) -> None:
        self.budget.settle(actual_tokens)
