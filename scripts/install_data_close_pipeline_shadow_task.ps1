# Install the Data Close Pipeline SHADOW task (SOURCE ONLY - NOT INSTALLED).
#
# Round-5 deliverable 3: a natural-shadow scheduled task contract. The shadow
# task runs the pipeline in read-only shadow mode on a schedule, producing
# evidence manifests WITHOUT any network, canonical writes, or subprocesses.
#
# Contract (task brief §4):
#   action: python -B run_data_close_pipeline.py --mode shadow --trigger scheduled
#   NO --enable-network, NO --alert-push, NO --apply
#   S4U principal (fail-closed if S4U cannot be created - NO Interactive fallback)
#   installed DISABLED first; old writer tasks are never touched
#   Plan/Status default; Install requires elevation + -Yes (NOT executed this round)
#
# HOW TO RUN (after user authorization + acceptance):
#   powershell -ExecutionPolicy Bypass -File scripts\install_data_close_pipeline_shadow_task.ps1              # Plan
#   powershell -ExecutionPolicy Bypass -File scripts\install_data_close_pipeline_shadow_task.ps1 -Action Status
#   powershell -ExecutionPolicy Bypass -File scripts\install_data_close_pipeline_shadow_task.ps1 -Action Install -Yes  # elevated

param(
    [ValidateSet("Plan", "Status", "Install", "Disable", "Remove")]
    [string]$Action = "Plan",
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$TaskName = "QuantDesk Data Close Pipeline Shadow"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Runner = Join-Path $Root "scripts\run_data_close_pipeline.py"
$Py = Join-Path $Root ".venv\Scripts\python.exe"

function Assert-Admin {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) { throw "This action requires an ELEVATED PowerShell window." }
}

switch ($Action) {
    "Plan" {
        Write-Output "=== PLAN (no changes) ==="
        Write-Output "Task:     $TaskName"
        if ((Test-Path $Py) -and (Test-Path $Runner)) {
            Write-Output "Action:   $Py -B `"$Runner`" --mode shadow --trigger scheduled"
        } else {
            Write-Output "Action:   BLOCKED - interpreter or runner missing (must run from the deployment tree)"
        }
        Write-Output "WorkDir:  $Root"
        Write-Output "Logon:    S4U (fail-closed; NO Interactive fallback)"
        Write-Output "Schedule: weekdays 15:50 (after the normal data window, read-only)"
        Write-Output "Forbids:  --enable-network / --alert-push / --apply (shadow is offline+read-only)"
        Write-Output "Install:  DISABLED first. Requires elevation + -Yes. Old tasks untouched."
    }
    "Status" {
        $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($t) {
            $i = $t | Get-ScheduledTaskInfo
            Write-Output ("state=" + $t.State + " logon=" + $t.Principal.LogonType + " next=" + $i.NextRunTime)
        } else { Write-Output "not_installed" }
    }
    "Install" {
        Assert-Admin
        if (-not $Yes) { throw "-Yes is required for Install (user authorization)." }
        if (-not (Test-Path $Py)) { throw "Interpreter missing: $Py" }
        if (-not (Test-Path $Runner)) { throw "Runner missing: $Runner" }
        $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $act = New-ScheduledTaskAction -Execute $Py `
          -Argument ('-B "{0}" --mode shadow --trigger scheduled' -f $Runner) `
          -WorkingDirectory $Root
        $trg = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '3:50PM'
        $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew
        # S4U only - fail-closed, NO Interactive fallback (task brief §4)
        $prn = New-ScheduledTaskPrincipal -UserId $me -LogonType S4U -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger $trg -Principal $prn -Settings $set -Force | Out-Null
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        $t = Get-ScheduledTask -TaskName $TaskName
        Write-Output ("installed_disabled=true state=" + $t.State)
        Write-Output ("action=" + $t.Actions[0].Execute + " " + $t.Actions[0].Arguments)
        Write-Output ("workdir=" + $t.Actions[0].WorkingDirectory)
        Write-Output ("logon=" + $t.Principal.LogonType)
        Write-Output "Next: user authorizes enable -> two natural shadow days -> Switch evaluation."
    }
    "Disable" {
        Assert-Admin
        if (-not $Yes) { throw "-Yes required." }
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        Write-Output "disabled"
    }
    "Remove" {
        Assert-Admin
        if (-not $Yes) { throw "-Yes required." }
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "removed"
    }
}
