# Install the consolidated Data Close Pipeline task.
#
# DEFAULT = Plan/Status only. Install/Disable/Remove require an elevated
# PowerShell AND the explicit -Yes switch (user authorization).
#
# Contract (task brief OPS-DATA-PIPELINE-CONSOLIDATION-20260901 section 10):
#   - new task is created DISABLED first (shadow validation before enabling)
#   - old tasks are never touched by this installer (rollback script handles them)
#   - no secrets in the action; working directory = this repo
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\install_data_close_pipeline_task.ps1              # Plan
#   powershell -ExecutionPolicy Bypass -File scripts\install_data_close_pipeline_task.ps1 -Action Status
#   powershell -ExecutionPolicy Bypass -File scripts\install_data_close_pipeline_task.ps1 -Action Install -Yes   # elevated

param(
    [ValidateSet("Plan", "Status", "Install", "Disable", "Remove")]
    [string]$Action = "Plan",
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$TaskName = "QuantDesk Data Close Pipeline"
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
        Write-Output "Schedule: weekdays 15:40 main run; 18:30 resume unfinished stages from checkpoint"
        if (Test-Path $Py) { Write-Output "Action:   $Py -B `"$Runner`" --enable-network --trigger scheduled --enable-alert-network --alert-push" }
        else { Write-Output "Action:   <interpreter missing here: $Py> (this installer must run from the deployment tree that owns the .venv)" }
        Write-Output "WorkDir:  $Root"
        Write-Output "Logon:    S4U"
        Write-Output "Install:  re-run with -Action Install -Yes (elevated)"
        Write-Output "NOTE: installs DISABLED. Old tasks keep running until the user"
        Write-Output "      explicitly switches (tools\admin\switch_data_tasks_to_pipeline.ps1)."
        Write-Output "NOTE: No SendKey is stored in this task action; Server酱 key is read from the environment at delivery time."
    }
    "Status" {
        $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($t) {
            $i = $t | Get-ScheduledTaskInfo
            Write-Output ("state=" + $t.State + " logon=" + $t.Principal.LogonType + " next=" + $i.NextRunTime + " last_result=" + $i.LastTaskResult)
        } else { Write-Output "not_installed" }
    }
    "Install" {
        Assert-Admin
        if (-not $Yes) { throw "-Yes is required for Install (user authorization)." }
        if (-not (Test-Path $Runner)) { throw "Runner missing: $Runner" }
        $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $act = New-ScheduledTaskAction -Execute $Py -Argument ('-B "{0}" --enable-network --trigger scheduled --enable-alert-network --alert-push' -f $Runner) -WorkingDirectory $Root
        $trgMain = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '3:40PM'
        $trgRetry = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '6:30PM'
        $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 4) -MultipleInstances IgnoreNew
        $prn = New-ScheduledTaskPrincipal -UserId $me -LogonType S4U -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger @($trgMain, $trgRetry) -Principal $prn -Settings $set -Force | Out-Null
        # Immediately disable: shadow validation comes before enabling.
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        $t = Get-ScheduledTask -TaskName $TaskName
        Write-Output ("installed_disabled=true state=" + $t.State)
        Write-Output "Next: run shadow comparison, then tools\admin\switch_data_tasks_to_pipeline.ps1 to enable."
    }
    "Disable" {
        Assert-Admin
        if (-not $Yes) { throw "-Yes is required for Disable." }
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        Write-Output "disabled"
    }
    "Remove" {
        Assert-Admin
        if (-not $Yes) { throw "-Yes is required for Remove." }
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "removed"
    }
}
