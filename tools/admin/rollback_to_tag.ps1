# ROLLBACK: return the canonical tree's code to a tagged version.
# Default action is Plan (read-only). Rollback never rewrites history: it
# tags the current state, then commits the tagged tree on top, so every
# rollback can itself be undone by rolling back to the pre-rollback tag.
# Only tracked source changes. data/, state/, results/, logs/ are untouched.

param(
    [ValidateSet("Plan", "Rollback")]
    [string]$Action = "Plan",
    [string]$Tag = "",
    [switch]$Yes,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Invoke-Git {
    param([string[]]$GitArgs)
    $output = & git -C $Root @GitArgs 2>&1
    if ($LASTEXITCODE -ne 0) { throw ("git " + ($GitArgs -join " ") + " failed: " + ($output -join "`n")) }
    return $output
}

function Get-RunningQuantTasks {
    @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
        $_.TaskName -like "QuantDesk*" -and $_.State -eq "Running"
    } | ForEach-Object { $_.TaskName })
}

if (-not $Tag) {
    Write-Output "Available version tags (newest first):"
    Invoke-Git @("tag", "--sort=-creatordate", "-l") | Select-Object -First 15 | ForEach-Object { Write-Output "  $_" }
    Write-Output ""
    Write-Output "Usage: rollback_to_tag.ps1 -Tag <tag>            (show the plan, change nothing)"
    Write-Output "       rollback_to_tag.ps1 -Action Rollback -Tag <tag>"
    exit 0
}

Invoke-Git @("rev-parse", "--verify", "--quiet", "refs/tags/$Tag") | Out-Null
$head = (Invoke-Git @("rev-parse", "--short", "HEAD")) -join ""
$branch = (Invoke-Git @("rev-parse", "--abbrev-ref", "HEAD")) -join ""
$undone = @(Invoke-Git @("log", "--oneline", "$Tag..HEAD"))
$dirty = @(Invoke-Git @("status", "--porcelain", "--untracked-files=no"))
$running = Get-RunningQuantTasks

Write-Output "=== ROLLBACK PLAN ==="
Write-Output "Repository : $Root"
Write-Output "Branch     : $branch at $head"
Write-Output "Target tag : $Tag"
Write-Output ("Commits whose changes will be undone: " + $undone.Count)
$undone | Select-Object -First 30 | ForEach-Object { Write-Output "  $_" }
Write-Output ("Uncommitted tracked changes (will be stashed, not lost): " + $dirty.Count)
if ($running.Count -gt 0) {
    Write-Output ("QuantDesk tasks running right now: " + ($running -join ", "))
} else {
    Write-Output "QuantDesk tasks running right now: none"
}

if ($Action -eq "Plan") {
    Write-Output ""
    Write-Output "Plan only. Nothing was changed."
    exit 0
}

if ($running.Count -gt 0 -and -not $Force) {
    throw "A QuantDesk task is running. Wait until it finishes (or pass -Force)."
}
if (-not $Yes) {
    $answer = Read-Host "Type the tag name ($Tag) to roll back, anything else to cancel"
    if ($answer -ne $Tag) { Write-Output "Cancelled. Nothing was changed."; exit 1 }
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$safety = "pre-rollback-$stamp"
Invoke-Git @("tag", "-a", $safety, "-m", "State before rolling back to $Tag") | Out-Null
if ($dirty.Count -gt 0) {
    Invoke-Git @("stash", "push", "-m", "rollback-$stamp uncommitted changes") | Out-Null
}
Invoke-Git @("read-tree", "-u", "--reset", $Tag) | Out-Null
$staged = @(Invoke-Git @("diff", "--cached", "--name-only"))
if ($staged.Count -eq 0) {
    Write-Output "Code already matches $Tag. Safety tag $safety was created; nothing else changed."
    exit 0
}
Invoke-Git @("commit", "-q", "-m", "rollback: return code to $Tag", "-m", "Previous state is tagged $safety. Undo with: rollback_to_tag.ps1 -Action Rollback -Tag $safety") | Out-Null
$new = (Invoke-Git @("rev-parse", "--short", "HEAD")) -join ""
Write-Output ""
Write-Output "Done. Code now matches $Tag (commit $new, $($staged.Count) files changed)."
Write-Output "To undo this rollback: rollback_to_tag.ps1 -Action Rollback -Tag $safety"
if ($dirty.Count -gt 0) { Write-Output "Your uncommitted changes are saved in: git stash list" }
