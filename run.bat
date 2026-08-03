@echo off
REM Local launcher. DEPLOY_ENV=local is REQUIRED: it is the only value that
REM lets the kill switch fail open when AWS is unreachable (design section 6).
cd /d "%~dp0"
set "AWS_PROFILE=edgar-sso"
set "AWS_REGION=us-east-2"
set "DEPLOY_ENV=local"
aws sts get-caller-identity --profile %AWS_PROFILE% >nul 2>&1
if errorlevel 1 (
  echo SSO session expired - signing in...
  aws sso login --profile %AWS_PROFILE%
)
if not exist "data\company_profile.parquet" (
  echo No exported data. See docs\SETUP-CREDENTIALS.md section 1.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m streamlit run app.py
