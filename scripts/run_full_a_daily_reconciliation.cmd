@echo off
rem Scheduled launcher for the full-A daily reconciliation coordinator.
rem
rem Why this exists: the daily chain refreshes the research pool and the weekly
rem task refreshes residual files, so no single run ever proved that EVERY
rem current-universe symbol has a bar for the target date or an explicit
rem verified exclusion. This launcher is what makes that contract run daily.
rem
rem Offline unless told otherwise: the coordinator performs canonical writes
rem only when BOTH --enable-network and --apply reach it. Pass them from the
rem scheduled task action, never bake them in here.
rem
rem Research only. No SendKey lives in this file or in the task action; the
rem Server chan dispatcher loads its own credential behind its own gates.
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
rem -B also ignores any .pyc already on disk. PYTHONDONTWRITEBYTECODE only stops
rem writing new ones: a stale cache written before it was set would still be READ.
"%PY%" -B "%ROOT%\scripts\run_full_a_daily_reconciliation.py" %*
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:no_python
popd
echo [full-a-reconcile] no usable Python interpreter found; set QUANTDESK_PYTHON or create .venv 1>&2
exit /b 9009
