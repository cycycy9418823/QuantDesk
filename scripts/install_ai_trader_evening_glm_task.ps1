# Install the SECOND AI-trader evening task (GLM profile). GENERATED ONLY - NOT INSTALLED.
#
# Schedule contract:
#   - "QuantDesk AI Trader Evening (GLM)", weekdays 20:40 (staggered 20 min after
#     the default book so the two runs never contend for the shared read path)
#   - Action: cmd /c scripts\run_ai_trader_evening_glm.cmd  (sets AI_TRADER_PROFILE=glm)
#   - Writes only state/ai_trader_glm + results/ai_trader_glm; needs
#     state/ai_trader_glm/llm.env with a token before it can use the model
#   - Paper only; no broker, no real orders
#
# HOW TO INSTALL (after acceptance, in an ELEVATED PowerShell):
#   powershell -ExecutionPolicy Bypass -File scripts\install_ai_trader_evening_glm_task.ps1 -Yes
#
# HOW TO REMOVE:
#   Unregister-ScheduledTask -TaskName "QuantDesk AI Trader Evening (GLM)" -Confirm:$false

param([switch]$Yes)
$ErrorActionPreference = "Stop"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Run this in an ELEVATED PowerShell window." }

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Runner = Join-Path $Root "scripts\run_ai_trader_evening_glm.cmd"
if (-not (Test-Path -LiteralPath $Runner)) { throw "Runner missing: $Runner" }
$EnvFile = Join-Path $Root "state\ai_trader_glm\llm.env"
if (-not (Test-Path -LiteralPath $EnvFile)) {
  Write-Warning "state\ai_trader_glm\llm.env is missing; the GLM book will run without the model until you place it."
}
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$act = New-ScheduledTaskAction -Execute 'cmd.exe' `
  -Argument ('/d /s /c ""{0}""' -f $Runner) `
  -WorkingDirectory $Root
$trg = New-ScheduledTaskTrigger -Weekly `
  -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '8:40PM'
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
try {
  $prn = New-ScheduledTaskPrincipal -UserId $me -LogonType S4U -RunLevel Limited
  Register-ScheduledTask -TaskName 'QuantDesk AI Trader Evening (GLM)' `
    -Action $act -Trigger $trg -Principal $prn -Settings $set -Force | Out-Null
} catch {
  $prn = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName 'QuantDesk AI Trader Evening (GLM)' `
    -Action $act -Trigger $trg -Principal $prn -Settings $set -Force | Out-Null
}
$t = Get-ScheduledTask -TaskName 'QuantDesk AI Trader Evening (GLM)'
$info = $t | Get-ScheduledTaskInfo
Write-Output ('logon=' + $t.Principal.LogonType + ' runlevel=' + $t.Principal.RunLevel)
Write-Output ('state=' + $t.State)
Write-Output ('next_run=' + $info.NextRunTime)
