"""V12: resolution order and fail messages."""

from __future__ import annotations

import pytest

from finchat import config


def test_missing_required_names_both_keys(monkeypatch) -> None:
    with pytest.raises(RuntimeError, match="MY_ENV.*(/ssm/key)?"):
        config.resolve("MY_ENV", "/ssm/key", None)


def test_env_beats_ssm(monkeypatch) -> None:
    monkeypatch.setenv("SOME_KEY", "from-env")
    monkeypatch.setattr(config, "_ssm_get", lambda name: "from-ssm")
    assert config.resolve("SOME_KEY", "/x", "d") == "from-env"


def test_ssm_beats_default(monkeypatch) -> None:
    monkeypatch.delenv("SOME_KEY", raising=False)
    monkeypatch.setattr(config, "_ssm_get", lambda name: "from-ssm")
    assert config.resolve("SOME_KEY", "/x", "d") == "from-ssm"


def test_default_when_ssm_unreachable(monkeypatch) -> None:
    monkeypatch.delenv("SOME_KEY", raising=False)
    monkeypatch.setattr(config, "_ssm_get", lambda name: None)
    assert config.resolve("SOME_KEY", "/x", "d") == "d"


def test_pin_bypasses_probe() -> None:
    class Exploding:
        def converse(self, **k):  # pragma: no cover
            raise AssertionError("probe must not run when pinned")

    assert config.pick_model(Exploding(), "my.model") == "my.model"


def test_probe_picks_first_invocable() -> None:
    calls = []

    class Client:
        def converse(self, modelId, **k):
            calls.append(modelId)
            if "nova-pro" in modelId:
                return {}
            raise RuntimeError("gated")

    chosen = config.pick_model(Client(), config.PROBE_SENTINEL)
    assert "nova-pro" in chosen
    assert calls[: len(calls)] == list(config.MODEL_CANDIDATES[: len(calls)])


def test_probe_exhaustion_names_setup_doc() -> None:
    class Dead:
        def converse(self, **k):
            raise RuntimeError("no")

    with pytest.raises(RuntimeError, match="SETUP-CREDENTIALS"):
        config.pick_model(Dead(), config.PROBE_SENTINEL)
