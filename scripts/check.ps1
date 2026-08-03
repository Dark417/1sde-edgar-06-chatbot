# The check gate (docs/21-test-plan.md). CI runs the same steps.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m ruff format --check .
.venv\Scripts\python -m mypy --strict src/
$env:DEPLOY_ENV = "local"
.venv\Scripts\python -m pytest -m "not live" --cov=src/finchat --cov-fail-under=85
$env:AGENT_IMPL = "adk"
.venv\Scripts\python -m pytest -m loop
Remove-Item Env:AGENT_IMPL
bash scripts/secret-scan.sh
Write-Host "check gate: GREEN"
