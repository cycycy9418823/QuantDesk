@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "ROOT=%~dp0.."
set "MODE=offline"
if "%~1"=="" goto args_ok
if /I "%~1"=="--enable-network" if /I "%~2"=="--apply" if "%~3"=="" (
  set "MODE=network_apply"
  goto args_ok
)
if /I "%~1"=="--enable-network" if /I "%~2"=="--apply" if /I "%~3"=="--lock-timeout-seconds" if "%~4"=="21600" if "%~5"=="" (
  set "MODE=network_apply_wait"
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
if "%MODE%"=="network_apply" (
  "%PY%" "%ROOT%\scripts\run_weekly_minute_wide_pool.py" --enable-network --apply --json true --summary true
) else if "%MODE%"=="network_apply_wait" (
  "%PY%" "%ROOT%\scripts\run_weekly_minute_wide_pool.py" --enable-network --apply --lock-timeout-seconds 21600 --json true --summary true
) else (
  "%PY%" "%ROOT%\scripts\run_weekly_minute_wide_pool.py" --network false --apply false --json true --summary true
)
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:run_py
if "%MODE%"=="network_apply" (
  py -3 "%ROOT%\scripts\run_weekly_minute_wide_pool.py" --enable-network --apply --json true --summary true
) else if "%MODE%"=="network_apply_wait" (
  py -3 "%ROOT%\scripts\run_weekly_minute_wide_pool.py" --enable-network --apply --lock-timeout-seconds 21600 --json true --summary true
) else (
  py -3 "%ROOT%\scripts\run_weekly_minute_wide_pool.py" --network false --apply false --json true --summary true
)
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:no_python
popd
echo QuantDesk Python runtime was not found. 1>&2
exit /b 1

:usage
echo Usage: run_weekly_minute_wide_pool.cmd [--enable-network --apply [--lock-timeout-seconds 21600]] 1>&2
exit /b 2
