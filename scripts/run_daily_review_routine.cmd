@echo off
rem Exactly-once evening research routine for scheduled execution.
rem
rem Why this exists: the daily data chain and the morning brief each had a
rem scheduled task, but the post-close review routine did not. It ran by hand,
rem which produced a 17-of-59-trading-day execution rate between 2026-05-14 and
rem 2026-08-05. Conditional-plan exits are only evaluated on days the routine
rem actually runs, so the skipped days silently discarded every exit signal that
rem fired on them -- no closed trades, no effective sell samples.
rem
rem Offline by default: this routine consumes artifacts the chain already wrote
rem and must never fetch. Guardrails stay on.
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "ROOT=%~dp0.."
set "MODE=offline"
if "%~1"=="" goto args_ok
if /I "%~1"=="--with-guardrails" if "%~2"=="" (
  set "MODE=offline"
  goto args_ok
)
goto usage

:args_ok
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
pushd "%ROOT%" >nul 2>&1 || exit /b 1
if defined QUANTDESK_PYTHON if exist "%QUANTDESK_PYTHON%" (
  set "PY=%QUANTDESK_PYTHON%"
  goto run_exe
)
if exist "%ROOT%\.venv\Scripts\python.exe" (
  set "PY=%ROOT%\.venv\Scripts\python.exe"
  goto run_exe
)
where py >nul 2>&1
if not errorlevel 1 goto run_py
where python >nul 2>&1
if errorlevel 1 goto no_python
set "PY=python"

:run_exe
"%PY%" "%ROOT%\scripts\run_daily_review_orchestrator.py" --summary true
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:run_py
py -3 "%ROOT%\scripts\run_daily_review_orchestrator.py" --summary true
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:no_python
popd
echo QuantDesk Python runtime was not found. 1>&2
exit /b 1

:usage
echo Usage: run_daily_review_routine.cmd [--with-guardrails] 1>&2
exit /b 2
