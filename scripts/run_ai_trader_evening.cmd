@echo off
rem Nightly evening cycle for the independent 100k AI paper trader.
rem
rem Why this exists: ai_trader only settles a committed plan when it is actually
rem run. Between 2026-08-17 and 2026-08-21 nobody ran it, so its plan for
rem 2026-08-18 was never settled and the ledger stayed at 0 trades. A scheduled
rem evening run is what makes the trader actually live.
rem
rem Offline by default: this cycle reads local canonical daily bars, the trading
rem calendar and the research-desk recommendation pool. Announcement fetching is
rem opt-in via --network so a scheduled run never depends on the internet.
rem
rem Paper only. Writes only state/ai_trader/. Never touches
rem state/auto_paper_portfolio.json, never routes a broker order.
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "ROOT=%~dp0.."
set "MODE=--offline"
if /I "%~1"=="--network" set "MODE="
rem Enable the LLM brain for scheduled runs. Credentials are read by ai_trader
rem from the gitignored state\ai_trader\llm.env; this only flips the opt-in gate.
set "AI_TRADER_LLM_ENABLED=1"
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
rem writing new ones: a stale cache written before it was set would still be READ,
rem so an edited source file could run as its old compiled version.
"%PY%" -B -m ai_trader run %MODE%
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:no_python
popd
echo [ai_trader] no usable Python interpreter found; set QUANTDESK_PYTHON or create .venv 1>&2
exit /b 9009
