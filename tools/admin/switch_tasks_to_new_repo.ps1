# QuantDesk R4: switch all Windows scheduled tasks from the old repo to THIS repo.
#
# HOW TO RUN (one time, ~20 minutes):
#   1. Right-click PowerShell -> Run as Administrator
#   2. cd <repo-root>
#   3. powershell -ExecutionPolicy Bypass -File tools\admin\switch_tasks_to_new_repo.ps1
#
# WHAT IT DOES:
#   1. Deletes the 3 deprecated cloud-migration tasks (that direction was
#      rejected on 2026-08-15; they have been crashing with 3221225786 daily).
#   2. Stops the old-repo Daily Data Chain instance if still running
#      (dual-repo rule: only ONE tree may run scheduled tasks at a time).
#   3. Re-registers every QuantDesk task FROM THIS REPO. Each installer derives
#      the repo root from its own file location, so the tasks can never point
#      at the old tree again.
#   4. Re-registers the AI Trader evening task with S4U logon first
#      (falls back to Interactive only if S4U registration is denied).
#   5. Prints a verification table. Review it before closing the window.
#
# ROLLBACK: every installer supports -Action Remove; the cloud tasks are gone
# on purpose (their direction was rejected); re-registering from the OLD repo
# scripts restores the previous state.

$ErrorActionPreference = 'Stop'

# --- 0. sanity checks -------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Please run this script in an ELEVATED PowerShell window (Run as Administrator)." }

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path   # tools\admin -> repo root
if (-not (Test-Path (Join-Path $Root 'scripts\qd.py'))) { throw "Repo root sanity check failed: $Root" }
Write-Output "Repo root: $Root"

# --- 1. delete deprecated cloud tasks --------------------------------------
foreach ($name in 'QuantDesk Cloud Migration Tunnel',
                  'QuantDesk Cloud Migration Upload',
                  'QuantDesk Cloud Snapshot Sync') {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Output "[deleted] $name (deprecated cloud direction)"
    }
}

# --- 2. stop the old-repo chain if it is still running ---------------------
$chain = Get-ScheduledTask -TaskName 'QuantDesk Daily Data Chain' -ErrorAction SilentlyContinue
if ($chain -and $chain.State -eq 'Running') {
    Stop-ScheduledTask -TaskName 'QuantDesk Daily Data Chain'
    Start-Sleep -Seconds 3
    Write-Output "[stopped] running Daily Data Chain instance (it was driving the OLD repo)"
}

# --- 3. re-register every task from THIS repo ------------------------------
# Each installer derives the repo root from $PSScriptRoot, so running it here
# binds the task to THIS tree. -Force semantics come from -Yes.
$jobs = @(
    @{ Name = 'Daily Data Chain';          Script = 'install_daily_data_tasks.ps1';            Args = @('-Action','Install','-LogonMode','S4U','-EnableNetwork','-Yes') },
    @{ Name = 'Daily Review Routine';      Script = 'install_daily_review_routine_task.ps1';   Args = @('-Action','Install','-Yes') },
    @{ Name = 'Morning Brief';             Script = 'install_morning_brief_task.ps1';          Args = @('-Action','Install','-EnableNetwork','-Yes') },
    @{ Name = 'Universe Baseline Refresh'; Script = 'install_universe_baseline_task.ps1';      Args = @('-Action','Install','-Yes') },
    @{ Name = 'Weekly Daily Wide Pool';    Script = 'install_weekly_daily_wide_pool_task.ps1'; Args = @('-Action','Install','-EnableNetwork','-Yes') },
    @{ Name = 'Full A Daily Reconciliation'; Script = 'install_full_a_daily_reconciliation_task.ps1'; Args = @('-Action','Install','-Yes') }
)

foreach ($job in $jobs) {
    $scriptPath = Join-Path $Root ("scripts\" + $job.Script)
    if (-not (Test-Path $scriptPath)) { Write-Output "[skip] $($job.Name): installer missing ($($job.Script))"; continue }
    Write-Output "[install] $($job.Name) ..."
    & powershell -NoProfile -ExecutionPolicy Bypass -File $scriptPath @($job.Args)
    if ($LASTEXITCODE -ne 0 -and $null -ne $LASTEXITCODE) {
        Write-Output "[warn] $($job.Name) installer exited with $LASTEXITCODE - check output above"
    }
}

# Weekly Minute Wide Pool stays DISABLED (minute data paused by design):
# re-register it bound to this repo, then keep it disabled.
$minuteScript = Join-Path $Root 'scripts\install_weekly_minute_wide_pool_task.ps1'
if (Test-Path $minuteScript) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $minuteScript -Action Install -EnableNetwork -Yes
    & powershell -NoProfile -ExecutionPolicy Bypass -File $minuteScript -Action Disable -Yes
    Write-Output "[ok] Weekly Minute Wide Pool re-registered (bound here) and left DISABLED"
}

# --- 4. AI Trader evening task: S4U first -----------------------------------
$aiRegister = Join-Path $Root 'state\ai_trader\register_evening_task.ps1'
if (Test-Path $aiRegister) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $aiRegister
} else {
    Write-Output "[warn] AI trader register script not found: $aiRegister"
}

# --- 5. verification table --------------------------------------------------
Write-Output ""
Write-Output "================ VERIFICATION ================"
Get-ScheduledTask | Where-Object { $_.TaskName -match 'QuantDesk' } | ForEach-Object {
    $info = $_ | Get-ScheduledTaskInfo
    $action = ($_.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join ' ; '
    $pointsHere = $action -like "*$Root*"
    [PSCustomObject]@{
        Task      = $_.TaskName
        State     = $_.State
        LogonType = $_.Principal.LogonType
        NextRun   = $info.NextRunTime
        NewRepo   = if ($pointsHere) { 'YES' } else { 'no' }
        Action    = ($action -replace [regex]::Escape($Root), '<NEW-REPO>')
    }
} | Format-Table -AutoSize -Wrap

Write-Output "Done. Every task above should show NewRepo=YES."
Write-Output "Tonight's natural runs (18:30 chain / 20:00 review / 20:20 AI trader)"
Write-Output "are the first natural validation of this tree - do NOT run them by hand."
