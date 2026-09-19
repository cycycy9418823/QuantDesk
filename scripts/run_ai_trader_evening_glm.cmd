@echo off
rem Nightly evening cycle for the SECOND AI paper trader (GLM brain).
rem
rem Same code path as the default book, but AI_TRADER_PROFILE=glm makes it read
rem and write state/ai_trader_glm + results/ai_trader_glm, with its own
rem llm.env. It shares ONLY the incoming market data (data/daily, 1m, snapshots)
rem with the default book; neither book can read the other's conclusions.
rem
rem Paper only. Never touches state/auto_paper_portfolio.json, never routes a
rem broker order.
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "ROOT=%~dp0.."
set "MODE=--offline"
if /I "%~1"=="--network" set "MODE="
set "AI_TRADER_PROFILE=glm"
rem Enable the LLM brain for scheduled runs. Credentials are read by ai_trader
rem from the gitignored state\ai_trader_glm\llm.env.
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
"%PY%" -B -m ai_trader run %MODE%
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:no_python
popd
echo [ai_trader/glm] no usable Python interpreter found; set QUANTDESK_PYTHON or create .venv 1>&2
exit /b 9009
