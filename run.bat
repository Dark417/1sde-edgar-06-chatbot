@echo off
REM Launch the EDGAR chat UI. Refreshes AWS credentials if the SSO session has
REM expired, then starts Streamlit on http://localhost:8501
cd /d "%~dp0"
set "AWS_PROFILE=edgar-sso"
set "AWS_REGION=us-east-2"
set "PYTHONPATH=%~dp0src"
aws sts get-caller-identity --profile %AWS_PROFILE% >nul 2>&1
if errorlevel 1 (
  echo SSO session expired - signing in...
  aws sso login --profile %AWS_PROFILE%
)
if not exist "data\company_profile.parquet" (
  echo No exported data found. Run: .venv\Scripts\python scripts\export_gold.py
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m streamlit run app.py
