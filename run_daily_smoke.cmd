@echo off
chcp 65001 >nul
setlocal
set "PROJECT_ROOT=%~dp0"
cd /d "%PROJECT_ROOT%"

if defined QUANTDESK_PYTHON (
  set "PY=%QUANTDESK_PYTHON%"
) else if exist "%PROJECT_ROOT%.venv\Scripts\python.exe" (
  set "PY=%PROJECT_ROOT%.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

echo QuantDesk daily smoke
echo Project root: %PROJECT_ROOT%
echo Python: %PY%
"%PY%" --version >nul 2>nul
if errorlevel 1 (
  echo Python not available.
  echo 请激活 conda 环境，或设置 QUANTDESK_PYTHON 指向 python.exe
  echo Example: set QUANTDESK_PYTHON=E:\miniconda\python.exe
  pause
  exit /b 1
)

echo Offline smoke: no network, no broker, no real orders, no investment advice.
"%PY%" "%PROJECT_ROOT%scripts\qd.py" daily --network false --with-guardrails true
if errorlevel 1 (
  echo Daily smoke report failed.
  pause
  exit /b 1
)

echo Daily smoke report finished.
pause
