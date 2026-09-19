# Install the intraday minute refresh task (GENERATED ONLY - NOT INSTALLED).
#
# Per the 2026-09-01 data-plane brief this installer is a deliverable for
# review; the operator (or Codex acceptance) decides when to run it.
#
# Schedule contract:
#   - "QuantDesk Intraday Data Refresh", weekdays only
#   - 09:35 first round, every 5 minutes until 11:30
#   - 13:05 resume,      every 5 minutes until 15:05 (15:05 = final check)
#   - MultipleInstances = IgnoreNew  (a second trigger while running is dropped)
#   - StartWhenAvailable = $false    (a missed round is not replayed; the next
#     round catches up by design - each round fetches all missing minutes)
#   - S4U logon, working directory = this repo, no secrets in the action
#   - Battery policy (honest declaration): default Task Scheduler settings
#     allow start on batteries. The collector is lightweight (a few dozen
#     requests per round); on battery the rounds still run.
#
# HOW TO INSTALL (after acceptance):
#   1. Right-click PowerShell -> Run as Administrator
#   2. cd <repo root>
#   3. powershell -ExecutionPolicy Bypass -File scripts\install_intraday_data_refresh_task.ps1 -Yes

param([switch]$Yes)
$ErrorActionPreference = "Stop"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Run this in an ELEVATED PowerShell window." }

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Runner = Join-Path $Root "scripts\run_intraday_data_refresh.py"
if (-not (Test-Path $Runner)) { throw "Runner missing: $Runner" }
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$act = New-ScheduledTaskAction -Execute $Py `
  -Argument ('-B "{0}" --enable-network --enable-alert-network --alert-push --alert-dry-run false' -f $Runner) `
  -WorkingDirectory $Root

$trgMorning = New-ScheduledTaskTrigger -Weekly `
  -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '9:35AM'
$trgMorning.Repetition = (New-ScheduledTaskTrigger -Once -At '9:35AM' -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Minutes 115)).Repetition

$trgAfternoon = New-ScheduledTaskTrigger -Weekly `
  -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '1:05PM'
$trgAfternoon.Repetition = (New-ScheduledTaskTrigger -Once -At '1:05PM' -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Minutes 120)).Repetition

$set = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
  -StartWhenAvailable:$false `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$prn = New-ScheduledTaskPrincipal -UserId $me -LogonType S4U -RunLevel Limited

Register-ScheduledTask -TaskName "QuantDesk Intraday Data Refresh" `
  -Action $act -Trigger @($trgMorning, $trgAfternoon) -Principal $prn -Settings $set -Force | Out-Null

$t = Get-ScheduledTask -TaskName "QuantDesk Intraday Data Refresh"
$info = $t | Get-ScheduledTaskInfo
Write-Output ("logon=" + $t.Principal.LogonType)
Write-Output ("state=" + $t.State)
Write-Output ("next_run=" + $info.NextRunTime)
Write-Output "No secrets are stored in this task; the Server-chan SendKey is read from the environment at delivery time."
