"""SEC2/SEC6/SEC8/V11 as executable gates."""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

SRC = pathlib.Path("src/finchat")


def _src_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in SRC.rglob("*.py"))


def test_forbidden_strings_absent() -> None:
    text = _src_text()
    for needle in ("import databricks", "vertexai", "st.secrets", "litellm.success_callback"):
        assert needle not in text, needle
    # the extension may be discussed in prose, never loaded in code
    for needle in ("INSTALL httpfs", "LOAD httpfs", "load_extension"):
        assert needle not in text, needle


def test_adk_module_disables_telemetry_before_litellm() -> None:
    text = (SRC / "agent/adk_runner.py").read_text(encoding="utf-8")
    assert "LITELLM_LOCAL_MODEL_COST_MAP" in text
    assert "litellm.telemetry = False" in text
    assert text.index("LITELLM_LOCAL_MODEL_COST_MAP") < text.index("import litellm")


def test_no_fstring_select_anywhere() -> None:
    pattern = re.compile(r'f["\'][^"\']*SELECT', re.IGNORECASE)
    for p in SRC.rglob("*.py"):
        assert not pattern.search(p.read_text(encoding="utf-8")), p


def test_core_imports_need_no_network() -> None:
    """V11: tools+data import with sockets disabled, in a clean subprocess."""
    code = (
        "import socket\n"
        "def _no(*a, **k): raise AssertionError('network at import time')\n"
        "socket.socket = _no\n"
        "import sys; sys.path.insert(0, 'src')\n"
        "import finchat.prompts, finchat.tools.impl, finchat.tools.registry\n"
        "import finchat.agent.base\n"
        "print('CLEAN')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "CLEAN" in out.stdout


def test_streamlit_not_imported_outside_ui() -> None:
    for p in SRC.rglob("*.py"):
        if "ui" in p.parts:
            continue
        assert "import streamlit" not in p.read_text(encoding="utf-8"), p
