@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "ROOT=%~dp0.."
set "MODE=offline"
if "%~1"=="" goto args_ok
if /I "%~1"=="--enable-network" (
  set "MODE=network"
  goto check_extra
)
if /I "%~1"=="--offline-smoke" (
  set "MODE=smoke"
  goto check_extra
)
goto usage

:check_extra
if not "%~2"=="" goto usage

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
if "%MODE%"=="network" (
  "%PY%" "%ROOT%\scripts\run_daily_data_chain.py" --trigger windows_scheduled_task --enable-network --json true --summary true
) else if "%MODE%"=="smoke" (
  "%PY%" "%ROOT%\scripts\run_daily_data_chain.py" --trigger windows_offline_smoke --network false --dry-run true --symbols 600000 --rebuild-features false --json true --summary true
) else (
  "%PY%" "%ROOT%\scripts\run_daily_data_chain.py" --trigger manual_runner --network false --json true --summary true
)
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:run_py
if "%MODE%"=="network" (
  py -3 "%ROOT%\scripts\run_daily_data_chain.py" --trigger windows_scheduled_task --enable-network --json true --summary true
) else if "%MODE%"=="smoke" (
  py -3 "%ROOT%\scripts\run_daily_data_chain.py" --trigger windows_offline_smoke --network false --dry-run true --symbols 600000 --rebuild-features false --json true --summary true
) else (
  py -3 "%ROOT%\scripts\run_daily_data_chain.py" --trigger manual_runner --network false --json true --summary true
)
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:no_python
popd
echo QuantDesk Python runtime was not found. 1>&2
exit /b 1

:usage
echo Usage: run_daily_data_chain.cmd [--enable-network^|--offline-smoke] 1>&2
exit /b 2
