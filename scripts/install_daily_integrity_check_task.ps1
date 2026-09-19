# Install the daily integrity check task (GENERATED ONLY - NOT INSTALLED).
#
# Per the repo's task convention this installer is a deliverable; the operator
# decides when to run it. It only registers a scheduled task; it never touches
# data/daily or any other canonical content.
#
# Schedule contract:
#   - "QuantDesk Daily Integrity Check", daily at 16:30
#   - Action: cmd /c scripts\run_daily_integrity_check.cmd
#   - Read-only scan of data/daily; writes one report to
#     results/daily_integrity/daily_integrity_latest.json
#   - No network, no repair, no broker, no canonical daily write
#   - MultipleInstances = IgnoreNew   (a second trigger while running is dropped)
#   - StartWhenAvailable = $true      (a missed window still produces today's
#     evidence; the scan only reads, so being late is harmless)
#   - S4U logon, working directory = this repo, no secrets in the action
#   - Exit code 0 = clean, 3 = issues found (the signal), 2 = usage error
#
# HOW TO INSTALL (after acceptance):
#   1. Right-click PowerShell -> Run as Administrator
#   2. cd <repo root>
#   3. powershell -ExecutionPolicy Bypass -File scripts\install_daily_integrity_check_task.ps1 -Yes
#
# HOW TO REMOVE:
#   Unregister-ScheduledTask -TaskName "QuantDesk Daily Integrity Check" -Confirm:$false

param([switch]$Yes)
$ErrorActionPreference = "Stop"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Run this in an ELEVATED PowerShell window." }

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Runner = Join-Path $Root "scripts\run_daily_integrity_check.cmd"
if (-not (Test-Path $Runner)) { throw "Runner missing: $Runner" }
$Cmd = Join-Path $env:SystemRoot "system32\cmd.exe"
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$act = New-ScheduledTaskAction -Execute $Cmd `
  -Argument ('/d /s /c ""{0}""' -f $Runner) `
  -WorkingDirectory $Root

$trg = New-ScheduledTaskTrigger -Daily -At '4:30PM'

$set = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
  -StartWhenAvailable:$true `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$prn = New-ScheduledTaskPrincipal -UserId $me -LogonType S4U -RunLevel Limited

Register-ScheduledTask -TaskName "QuantDesk Daily Integrity Check" `
  -Action $act -Trigger $trg -Principal $prn -Settings $set -Force | Out-Null

$t = Get-ScheduledTask -TaskName "QuantDesk Daily Integrity Check"
$info = $t | Get-ScheduledTaskInfo
Write-Output ("logon=" + $t.Principal.LogonType)
Write-Output ("state=" + $t.State)
Write-Output ("next_run=" + $info.NextRunTime)
Write-Output "Read-only scan; report at results/daily_integrity/daily_integrity_latest.json."
