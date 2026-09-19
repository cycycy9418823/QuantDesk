# Install the unified daily data refresh task (fast path).
#
# HOW TO RUN (one time):
#   1. Right-click PowerShell -> Run as Administrator
#   2. cd <repo-root>
#   3. powershell -ExecutionPolicy Bypass -File scripts\install_daily_data_refresh_task.ps1 -Yes
#
# Schedule: weekdays 15:40 (after close snapshot settles at ~15:35, before the
# 15:45 chain - which then finds the pool already fresh and finishes fast).
# S4U logon (runs without the user being logged in).

param([switch]$Yes)
$ErrorActionPreference = "Stop"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Run this in an ELEVATED PowerShell window." }

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Runner = Join-Path $Root "scripts\run_daily_data_refresh.py"
if (-not (Test-Path $Runner)) { throw "Runner missing: $Runner" }

$Py = Join-Path $Root ".venv\Scripts\python.exe"
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$act = New-ScheduledTaskAction -Execute $Py `
  -Argument ('-B "{0}" --enable-network --enable-alert-network --alert-push --alert-dry-run false' -f $Runner) `
  -WorkingDirectory $Root
$trg = New-ScheduledTaskTrigger -Weekly `
  -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '3:40PM'
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 45) -MultipleInstances IgnoreNew
$prn = New-ScheduledTaskPrincipal -UserId $me -LogonType S4U -RunLevel Limited

Register-ScheduledTask -TaskName "QuantDesk Daily Data Refresh" `
  -Action $act -Trigger $trg -Principal $prn -Settings $set -Force | Out-Null

$t = Get-ScheduledTask -TaskName "QuantDesk Daily Data Refresh"
$info = $t | Get-ScheduledTaskInfo
Write-Output ("logon=" + $t.Principal.LogonType)
Write-Output ("next_run=" + $info.NextRunTime)
Write-Output ("state=" + $t.State)
