@echo off
setlocal EnableExtensions
set "ROOT=%~dp0"
cd /d "%ROOT%"

call :resolve_python
if errorlevel 1 exit /b 1

echo Project root: %ROOT%
echo Python: %PY%

"%PY%" "%ROOT%scripts\self_check.py"
if errorlevel 1 goto failed

"%PY%" "%ROOT%scripts\qd.py" env-doctor --json true
if errorlevel 1 goto failed

echo QuantDesk self check passed.
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

:failed
echo QuantDesk self check failed.
pause
exit /b 1
