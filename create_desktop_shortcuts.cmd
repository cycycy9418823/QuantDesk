@echo off
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"

echo QuantDesk desktop shortcut setup
echo Project root: %ROOT%

if not exist "%ROOT%create_desktop_shortcuts.ps1" (
  echo Missing script: %ROOT%create_desktop_shortcuts.ps1
  pause
  exit /b 1
)

powershell -ExecutionPolicy Bypass -File "%ROOT%create_desktop_shortcuts.ps1"
if errorlevel 1 (
  echo Failed to create QuantDesk desktop shortcuts.
  pause
  exit /b 1
)

echo.
echo Desktop shortcut created:
echo - QuantDesk
echo.
echo You can now double-click the desktop icons to start QuantDesk.
pause
exit /b 0
