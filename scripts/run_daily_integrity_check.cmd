@echo off
rem Daily canonical daily-CSV integrity check (read-only scan + evidence report).
rem
rem Why this exists: one all-NUL inserted record sat in 72 data/daily files for a
rem week without being noticed, silently blocking every append for those symbols
rem (the append path correctly refuses a column-mismatched file, so the damage
rem was "stuck" rather than loud). This launcher makes a missing/garbled daily
rem file visible the day it happens instead of seven days later.
rem
rem Read-only on data. It writes exactly one report to
rem results/daily_integrity/daily_integrity_latest.json and never repairs,
rem fetches, or touches canonical daily content.
rem
rem Exit codes are distinct so the task log tells you the state:
rem   0  clean
rem   3  integrity issues found (the alert signal; see the report)
rem   2  usage error (missing daily dir or unreadable universe)
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "ROOT=%~dp0.."
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
pushd "%ROOT%" >nul 2>&1 || exit /b 1

if defined QUANTDESK_PYTHON if exist "%QUANTDESK_PYTHON%" (
  set "PY=%QUANTDESK_PYTHON%"
  goto run_exe
)
if exist "%ROOT%\.venv\Scripts\python.exe" (
  set "PY=%ROOT%\.venv\Scripts\python.exe"
  goto run_exe
)
where python >nul 2>&1
if errorlevel 1 goto no_python
set "PY=python"

:run_exe
"%PY%" -B "%ROOT%\scripts\check_daily_integrity.py" ^
  --daily-dir "%ROOT%\data\daily" ^
  --universe "%ROOT%\data\universe\a_share_symbols.csv" ^
  --output "%ROOT%\results\daily_integrity\daily_integrity_latest.json" ^
  --json false %*
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:no_python
popd
echo [daily-integrity] no usable Python interpreter found; set QUANTDESK_PYTHON or create .venv 1>&2
exit /b 9009
