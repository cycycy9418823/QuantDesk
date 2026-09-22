# ROLLBACK: restore the scattered data tasks and disable the pipeline.
# One elevated command restores the pre-switch state. Never touches data.

param(
    [ValidateSet("Plan", "Status", "Rollback")]
    [string]$Action = "Plan",
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$PipelineTask = "QuantDesk Data Close Pipeline"
$OldWriteTasks = @(
    "QuantDesk Universe Baseline Refresh",
    "QuantDesk Daily Data Refresh",
    "QuantDesk Daily Data Chain",
    "QuantDesk Full A Daily Reconciliation"
)

function Assert-Admin {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) { throw "This action requires an ELEVATED PowerShell window." }
}

switch ($Action) {
    "Plan" {
        Write-Output "=== ROLLBACK PLAN (no changes) ==="
        Write-Output "1. Disable $PipelineTask (if running: stop first)"
        Write-Output "2. Re-enable the four old write tasks with their original triggers:"
        $OldWriteTasks | ForEach-Object { Write-Output "   - $_" }
        Write-Output "Data files are never modified by this script."
    }
    "Status" {
        Get-ScheduledTask | Where-Object { $_.TaskName -match "QuantDesk" } | ForEach-Object {
            $i = $_ | Get-ScheduledTaskInfo
            "{0,-44} {1,-9} next={2}" -f $_.TaskName, $_.State, $i.NextRunTime
        }
    }
    "Rollback" {
        Assert-Admin
        if (-not $Yes) { throw "-Yes required for Rollback." }
        $pipe = Get-ScheduledTask -TaskName $PipelineTask -ErrorAction SilentlyContinue
        if ($pipe) {
            if ($pipe.State -eq "Running") { Stop-ScheduledTask -TaskName $PipelineTask; Start-Sleep -Seconds 3 }
            Disable-ScheduledTask -TaskName $PipelineTask | Out-Null
            Write-Output "[disabled] $PipelineTask"
        }
        foreach ($name in $OldWriteTasks) {
            $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            if ($t) {
                Enable-ScheduledTask -TaskName $name | Out-Null
                Write-Output "[enabled]  $name (original trigger preserved)"
            } else {
                Write-Output "[missing]  $name (was never disabled or already removed)"
            }
        }
        $pipeState = "Missing"
        $pipeNow = Get-ScheduledTask -TaskName $PipelineTask -ErrorAction SilentlyContinue
        if ($pipeNow) { $pipeState = [string]$pipeNow.State }
        $activeOld = @($OldWriteTasks | Where-Object {
            $st = Get-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue
            $st -and $st.State -ne "Disabled"
        })
        if ($pipeState -ne "Disabled" -and $pipeState -ne "Missing") {
            throw "ROLLBACK UNIQUE-WRITER VIOLATION: pipeline still $pipeState"
        }
        if ($activeOld.Count -lt 1) {
            throw "ROLLBACK UNIQUE-WRITER VIOLATION: no original writer restored"
        }
        & $PSCommandPath -Action Status
        Write-Output ("Rollback complete. unique_writer_ok pipeline={0} active_old={1}" -f $pipeState, $activeOld.Count)
    }
}
