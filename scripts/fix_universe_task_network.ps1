# Fix the Universe Baseline Refresh task: CORRECT action args + S4U logon.
#
# WHY (2026-09-02 corrected by Codex preacceptance): refresh_universe_baseline.py
# has NO --enable-network parameter. The earlier version of this script appended
# that phantom flag, making the CLI exit 2 every run (the 15:30 failure root
# cause). The script's network gating lives in its provider layer via the
# approved-source guard - the CLI runs AS-IS. While here, switch the principal
# from Interactive to S4U so the 15:30 run no longer depends on a login session.
#
# HOW TO RUN (elevated, one time):
#   1. Right-click PowerShell -> Run as Administrator
#   2. cd <repo-root>
#   3. powershell -ExecutionPolicy Bypass -File scripts\fix_universe_task_network.ps1

$ErrorActionPreference = "Stop"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Run this in an ELEVATED PowerShell window." }

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$Runner = Join-Path $Root "scripts\refresh_universe_baseline.py"
if (-not (Test-Path $Py)) { throw "Interpreter missing: $Py" }
if (-not (Test-Path $Runner)) { throw "Runner missing: $Runner" }
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$act = New-ScheduledTaskAction -Execute $Py `
  -Argument ('-B "{0}"' -f $Runner) `
  -WorkingDirectory $Root
$trg = New-ScheduledTaskTrigger -Daily -At "3:30PM"
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew

$prn = $null
try {
  $prn = New-ScheduledTaskPrincipal -UserId $me -LogonType S4U -RunLevel Limited
  Register-ScheduledTask -TaskName "QuantDesk Universe Baseline Refresh" `
    -Action $act -Trigger $trg -Principal $prn -Settings $set -Force | Out-Null
} catch {
  $prn = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName "QuantDesk Universe Baseline Refresh" `
    -Action $act -Trigger $trg -Principal $prn -Settings $set -Force | Out-Null
}

$t = Get-ScheduledTask -TaskName "QuantDesk Universe Baseline Refresh"
$info = $t | Get-ScheduledTaskInfo
Write-Output ("logon=" + $t.Principal.LogonType)
Write-Output ("action=" + $t.Actions[0].Execute + " " + $t.Actions[0].Arguments)
Write-Output ("next_run=" + $info.NextRunTime)
Write-Output ("state=" + $t.State)
