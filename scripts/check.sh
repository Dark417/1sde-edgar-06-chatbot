#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m ruff format --check .
.venv/Scripts/python -m mypy --strict src/
DEPLOY_ENV=local .venv/Scripts/python -m pytest -m "not live" --cov=src/finchat --cov-fail-under=85
DEPLOY_ENV=local AGENT_IMPL=adk .venv/Scripts/python -m pytest -m loop
bash scripts/secret-scan.sh
echo "check gate: GREEN"
