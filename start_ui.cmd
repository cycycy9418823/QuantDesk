@echo off
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"

set "PY=%ROOT%.venv\Scripts\python.exe"
set "QD=%ROOT%scripts\qd.py"

echo Project root: %ROOT%
echo Python: %PY%

if not exist "%PY%" (
  echo Python venv not found: %PY%
  echo Please run: powershell -ExecutionPolicy Bypass -File "%ROOT%setup_windows.ps1"
  pause
  exit /b 1
)

if not exist "%QD%" (
  echo QuantDesk CLI not found: %QD%
  pause
  exit /b 1
)

echo Starting QuantDesk through qd.py app on port 8000...
if "%~1"=="" (
  echo Startup provider network: disabled ^(default^).
) else (
  echo Forwarding explicit app arguments: %*
)
echo To authorize a bounded startup refresh, pass --update-on-start explicitly.
"%PY%" "%QD%" app %*
if errorlevel 1 (
  echo QuantDesk UI failed to start.
  pause
  exit /b 1
)
pause
