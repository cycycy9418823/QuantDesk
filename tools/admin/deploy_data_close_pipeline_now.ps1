# One-shot deploy authorized after trial-20260904-rerun-deploy2
# completed all five stages (Stage 3 stdout=completed).
# Installs QuantDesk Data Close Pipeline ENABLED and disables the four
# old data writers. Does not touch Morning Brief / AI Trader / Daily Review.
# Requires elevated PowerShell.

param([switch]$Yes)

$ErrorActionPreference = "Stop"
$LogPath = "E:\codex\claude\deploy_data_close_pipeline_now.log"
function Write-Log([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:ss"), $msg
    Add-Content -Path $LogPath -Value $line -Encoding utf8
    Write-Output $msg
}
New-Item -ItemType Directory -Force -Path (Split-Path $LogPath) | Out-Null
"" | Set-Content -Path $LogPath -Encoding utf8
$TaskName = "QuantDesk Data Close Pipeline"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Runner = Join-Path $Root "scripts\run_data_close_pipeline.py"
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$OldWriteTasks = @(
    "QuantDesk Universe Baseline Refresh",
    "QuantDesk Daily Data Refresh",
    "QuantDesk Daily Data Chain",
    "QuantDesk Full A Daily Reconciliation"
)

try {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) { throw "This action requires an ELEVATED PowerShell window." }
    if (-not $Yes) { throw "-Yes is required." }
    if (-not (Test-Path $Runner)) { throw "Runner missing: $Runner" }
    if (-not (Test-Path $Py)) { throw "Interpreter missing: $Py" }

    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $act = New-ScheduledTaskAction -Execute $Py -Argument ('-B "{0}" --enable-network --trigger scheduled --enable-alert-network --alert-push' -f $Runner) -WorkingDirectory $Root
    $trgMain = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '3:40PM'
    $trgRetry = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '6:30PM'
    $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 4) -MultipleInstances IgnoreNew
    $prn = New-ScheduledTaskPrincipal -UserId $me -LogonType S4U -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger @($trgMain, $trgRetry) -Principal $prn -Settings $set -Force | Out-Null
    Enable-ScheduledTask -TaskName $TaskName | Out-Null
    $pipe = Get-ScheduledTask -TaskName $TaskName
    if ($pipe.State -ne "Ready") { throw "pipeline failed to enable (state=$($pipe.State))" }
    Write-Log ("[enabled] {0} state={1}" -f $TaskName, $pipe.State)

    foreach ($name in $OldWriteTasks) {
        $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if (-not $t) {
            Write-Log ("[skip] {0} missing" -f $name)
            continue
        }
        if ($t.State -eq "Running") {
            Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 3
        }
        Disable-ScheduledTask -TaskName $name | Out-Null
        $chk = Get-ScheduledTask -TaskName $name
        if ($chk.State -ne "Disabled") { throw "failed to disable $name (state=$($chk.State))" }
        Write-Log ("[disabled] {0}" -f $name)
    }

    Write-Log "deploy_ok=true"
    Get-ScheduledTask | Where-Object { $_.TaskName -match "QuantDesk" } | ForEach-Object {
        Write-Log ("{0,-48} {1}" -f $_.TaskName, $_.State)
    }
} catch {
    Write-Log ("deploy_failed: {0}" -f $_.Exception.Message)
    throw
}
