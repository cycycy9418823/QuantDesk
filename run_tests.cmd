@echo off
setlocal EnableExtensions
set "ROOT=%~dp0"
cd /d "%ROOT%"

call :resolve_python
if errorlevel 1 exit /b 1

echo Project root: %ROOT%
echo Python: %PY%

"%PY%" -m compileall -q quantdesk scripts tests
if errorlevel 1 goto failed

"%PY%" -m py_compile scripts\qd.py scripts\self_check.py
if errorlevel 1 goto failed

where node >nul 2>nul
if errorlevel 1 goto node_missing
node --check app\static\js\daily_research.js
if errorlevel 1 goto failed

"%PY%" scripts\qd.py maintenance test-tier-runner --tier tier0_fast_unit --limit 0 --timeout-seconds 300 --dry-run false --json true --summary true
if errorlevel 1 goto failed

"%PY%" -m pytest tests\test_reported_quality_fixes.py tests\test_rc14_mainline.py tests\test_v21_strategy_core.py tests\test_v241_research_validation.py tests\test_v278_intraday_swing_execution.py tests\test_v360_smart_stop_and_risk_alerts.py tests\test_backtest_metrics.py tests\test_data_audit.py tests\test_walk_forward_hold_period_warning.py -q
if errorlevel 1 goto failed

"%PY%" -m unittest tests.test_script_inventory_governance tests.test_test_inventory_governance tests.test_test_discovery_diagnostics_governance tests.test_test_tier_runner_governance tests.test_frontend_api_contract_governance tests.test_dependency_contract_governance tests.test_data_backtest_contract_governance tests.test_coverage_contract_governance tests.test_gitlab_ci_governance -v
if errorlevel 1 goto failed

"%PY%" scripts\self_check.py
if errorlevel 1 goto failed

"%PY%" scripts\qd.py env-doctor --json true
if errorlevel 1 goto failed

powershell -ExecutionPolicy Bypass -NoProfile -File "%ROOT%export_project.ps1"
if errorlevel 1 goto failed

echo QuantDesk validation and clean zip export passed.
exit /b 0

:resolve_python
if defined QUANTDESK_PYTHON set "PY=%QUANTDESK_PYTHON%"
if not defined PY if exist "%ROOT%.venv\Scripts\python.exe" set "PY=%ROOT%.venv\Scripts\python.exe"
if not defined PY set "PY=python"
"%PY%" --version >nul 2>nul
if not errorlevel 1 exit /b 0
echo Python not available.
echo Activate the project environment or set QUANTDESK_PYTHON to python.exe.
echo Example: set QUANTDESK_PYTHON=E:\miniconda\python.exe
exit /b 1

:node_missing
echo Node.js not available; cannot run frontend syntax check.
goto failed

:failed
echo QuantDesk validation failed.
pause
exit /b 1
