@echo off
rem Install the QuantDesk clean-repo pre-commit guard into THIS repo's .git/hooks.
rem Idempotent: safe to re-run; backs up any existing hook once (.bak1..9).
setlocal
set ROOT=%~dp0..\..
set HOOK=%ROOT%\.git\hooks\pre-commit
if exist "%HOOK%" (
  for /L %%i in (1,1,9) do (
    if not exist "%HOOK%.bak%%i" (
      copy /Y "%HOOK%" "%HOOK%.bak%%i" >nul && goto :installed_ok_bak
    )
  )
  echo [install] too many backups, refusing to overwrite %HOOK%
  exit /b 2
)
:installed_ok_bak
copy /Y "%~dp0pre-commit.sh" "%HOOK%" >nul
echo [install] guard active: %HOOK%
endlocal & exit /b 0
