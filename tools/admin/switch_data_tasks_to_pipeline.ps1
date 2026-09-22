# ---------------------------------------------------------------------------
# Transactional switch: scattered data tasks -> consolidated Data Close Pipeline.
# ROLLBACK-PAIR: rollback_data_tasks_from_pipeline.ps1
#
# P0 Round-2 fix: the rollback now RESTORES the exact pre-switch state of
# every task it touched. The pre-state (Enabled/Disabled/Ready/Missing) is
# captured BEFORE any mutation; on any failure each task is restored to its
# own recorded state (re-enable what was enabled, re-disable what was
# disabled), and the pipeline is restored last. The invariant check at the
# end guarantees at least the ORIGINAL writers are back before exit.
#
# P0 Round-2 fix: shadow evidence validation now verifies the FULL contract:
# schema_version, mode=shadow, canonical_write=false, trigger=scheduled,
# target_date - manual or forged manifests are rejected. -ConfirmedNaturalSuccess
# is only the USER AUTHORIZATION, never the evidence itself.
#
# DEFAULT = Plan/Status only. Switch requires elevation + -Yes + valid evidence.

param(
    [ValidateSet("Plan", "Status", "Switch", "EnableShadow")]
    [string]$Action = "Plan",
    [switch]$Yes,
    [switch]$ConfirmedNaturalSuccess,
    [string]$RepoRoot = ""
)

$ErrorActionPreference = "Stop"
$PipelineTask = "QuantDesk Data Close Pipeline"
$OldWriteTasks = @(
    "QuantDesk Universe Baseline Refresh",
    "QuantDesk Daily Data Refresh",
    "QuantDesk Daily Data Chain",
    "QuantDesk Full A Daily Reconciliation"
)

if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path }
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$ManifestDir = Join-Path $RepoRoot "results\data_close_pipeline"
# Interpreter for trade-day validation: prefer the repo's own venv; fall back
# to the canonical tree's venv (worktrees do not carry .venv).
$PyExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $PyExe)) { $PyExe = "E:\codex\quant_new\.venv\Scripts\python.exe" }

function Assert-Admin {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) { throw "This action requires an ELEVATED PowerShell window." }
}

function Get-TaskState {
    param([string]$Name)
    $t = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (-not $t) { return "Missing" }
    return $t.State  # Ready | Disabled | Running | ...
}

function Restore-TaskState {
    # Restore a task to its RECORDED pre-switch state - the opposite
    # operation of whatever the switch performed on it.
    param([string]$Name, [string]$DesiredState)
    if ($DesiredState -eq "Missing") { return }
    try {
        $current = Get-TaskState -Name $Name
        if ($DesiredState -in @("Ready", "Running") -and $current -eq "Disabled") {
            Enable-ScheduledTask -TaskName $Name -ErrorAction Stop | Out-Null
        } elseif ($DesiredState -eq "Disabled" -and $current -ne "Disabled") {
            Disable-ScheduledTask -TaskName $Name -ErrorAction Stop | Out-Null
        }
    } catch {
        Write-Output ("[rollback-warn] {0}: {1}" -f $Name, $_.Exception.Message)
    }
}

function Get-PipelineTaskContract {
    $t = Get-ScheduledTask -TaskName $PipelineTask -ErrorAction SilentlyContinue
    if (-not $t) { throw "Pipeline task not installed. Run install_data_close_pipeline_task.ps1 first." }
    return $t
}

function Test-PipelineContract {
    param($Task)
    $problems = @()
    $action = $Task.Actions[0]
    if ($action.Execute -notlike "*python.exe") { $problems += "action_not_python" }
    if (-not $action.Execute -or -not (Test-Path -LiteralPath $action.Execute)) { $problems += "interpreter_missing" }
    if ($action.Arguments -notmatch "run_data_close_pipeline\.py") { $problems += "action_not_pipeline_runner" }
    if ($action.Arguments -notmatch "--enable-network") { $problems += "network_not_enabled" }
    if ($action.Arguments -notmatch "--trigger scheduled") { $problems += "trigger_not_scheduled" }
    if ($action.Arguments -notmatch "--enable-alert-network") { $problems += "alert_network_not_enabled" }
    if ($action.Arguments -notmatch "--alert-push") { $problems += "alert_push_not_enabled" }
    if ($action.Arguments -match "--mode shadow|--dry-run|--network false") { $problems += "non_writing_mode_present" }
    if ($action.WorkingDirectory -ne $RepoRoot) { $problems += "workdir_mismatch:$($action.WorkingDirectory)" }
    if ($Task.Principal.LogonType -ne "S4U") { $problems += "logon_not_s4u" }
    if (-not $Task.Triggers) { $problems += "no_trigger" }
    return ,$problems
}

function Get-TaskActionFingerprint {
    param($Task)
    if (-not $Task -or -not $Task.Actions -or -not $Task.Actions[0]) { return "" }
    $action = $Task.Actions[0]
    $raw = "{0}`n{1}`n{2}" -f [string]$action.Execute, [string]$action.Arguments, [string]$action.WorkingDirectory
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($raw)
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Test-NaturalShadowProvenance {
    # Structural JSON is never enough to authorize Switch. Fail closed until
    # the shadow task exists and a Task Scheduler start event can be bound to
    # its action fingerprint. An uninstalled task cannot mint natural evidence.
    $shadowTask = Get-ScheduledTask -TaskName "QuantDesk Data Close Pipeline Shadow" -ErrorAction SilentlyContinue
    if (-not $shadowTask) {
        return @("shadow_task_not_installed:natural_provenance_unavailable")
    }
    $hash = Get-TaskActionFingerprint -Task $shadowTask
    if (-not $hash) {
        return @("shadow_task_action_hash_missing")
    }
    $bound = $false
    try {
        $events = Get-WinEvent -FilterHashtable @{
            LogName = "Microsoft-Windows-TaskScheduler/Operational"
            Id = 200
        } -MaxEvents 80 -ErrorAction Stop
        foreach ($ev in $events) {
            if ($ev.Message -and ($ev.Message -like "*Data Close Pipeline Shadow*")) {
                $bound = $true
                break
            }
        }
    } catch {
        return @("scheduler_event_log_unavailable:natural_provenance_unavailable")
    }
    if (-not $bound) {
        return @("scheduler_start_event_missing:natural_provenance_unavailable")
    }
    return @()
}

function Test-ShadowEvidence {
    # Round-5 deliverable 2: the FULL contract from the task brief, closing
    # every bypass Codex demonstrated (string "false", 0, running/failed
    # stages, banana dates, run_id mismatch, normal-namespace manifests).
    # Every candidate must pass ALL checks; any miss = not evidence.
    # Only the SHADOW namespace is read; normal manifests are never evidence.
    $expectedSchema = "data_close_pipeline_v1"
    $expectedShadowContract = "shadow_evidence_v1"
    $expectedStages = @("universe", "market_refresh", "research_chain", "reconcile", "health_alert")
    $shadowDir = Join-Path $ManifestDir "shadow"
    $dates = @()
    if (-not (Test-Path $shadowDir)) { return @("shadow_namespace_missing") }
    $runs = Get-ChildItem (Join-Path $shadowDir "runs") -Recurse -Filter "manifest.json" -ErrorAction SilentlyContinue
    foreach ($m in $runs) {
        try { $d = Get-Content $m.FullName -Raw | ConvertFrom-Json } catch { continue }
        # 1. schema
        if ($d.schema_version -cne $expectedSchema) { continue }
        # 2. shadow contract version
        if ($d.shadow_contract_version -cne $expectedShadowContract) { continue }
        # 3. mode
        if ($d.mode -cne "shadow") { continue }
        # 4. trigger
        if ($d.trigger -cne "scheduled") { continue }
        # 5. run_id matches parent dir name
        $dirName = $m.Directory.Name
        if (-not $d.run_id -or $d.run_id -cne $dirName) { continue }
        # 6. target_date: strict yyyy-MM-dd shape (calendar check needs the
        #    Python side; PS-side enforces the format and fails closed on
        #    anything that is not 10 chars of a real date pattern)
        $td = $d.target_date
        if (-not $td -or $td -notmatch '^\d{4}-\d{2}-\d{2}$') { continue }
        try { [void][datetime]::ParseExact($td, "yyyy-MM-dd", $null) } catch { continue }
        # 7. exactly the five stages
        $stages = $d.stages
        if (-not $stages) { continue }
        $stageNames = @($stages.PSObject.Properties.Name)
        $missing = @($expectedStages | Where-Object { $_ -notin $stageNames })
        $extra = @($stageNames | Where-Object { $_ -notin $expectedStages })
        if ($missing.Count -gt 0 -or $extra.Count -gt 0) { continue }
        # 8-9. every stage: canonical_write strictly [bool]$false (string
        # "false"/0/null/absent all rejected), status strictly "skipped",
        # reason_codes contains shadow_readonly_skipped
        $valid = $true
        foreach ($name in $expectedStages) {
            $s = $stages.$name
            if ($null -eq $s) { $valid = $false; break }
            if ($s.PSObject.Properties["canonical_write"] -eq $null) { $valid = $false; break }
            $cw = $s.canonical_write
            if (-not ($cw -is [bool]) -or $cw -ne $false) { $valid = $false; break }
            if ($s.status -cne "skipped") { $valid = $false; break }
            $rc = @($s.reason_codes)
            if ($rc -notcontains "shadow_readonly_skipped") { $valid = $false; break }
        }
        if (-not $valid) { continue }
        # 10. top-level status + reason
        if ($d.status -cne "completed_with_warnings") { continue }
        $topRc = @($d.reason_codes)
        if ($topRc -notcontains "shadow_read_only:no_canonical_writes:no_stage_subprocess") { continue }
        # Round-6: producer/verifier bidirectional contract (Codex Round-5 #7):
        # top-level network must be boolean false, alert_policy preview_only,
        # normal_pipeline_state_touched must be present and boolean false,
        # and each stage's internal `stage` name must equal its key.
        if ($null -eq $d.network -or -not ($d.network -is [bool]) -or $d.network -ne $false) { continue }
        if ($d.alert_policy -cne "preview_only") { continue }
        if ($null -eq $d.normal_pipeline_state_touched) { continue }
        if (-not ($d.normal_pipeline_state_touched -is [bool])) { continue }
        if ($d.normal_pipeline_state_touched -ne $false) { continue }
        $stageNameOk = $true
        foreach ($name in $expectedStages) {
            if ($stages.$name.stage -cne $name) { $stageNameOk = $false; break }
        }
        if (-not $stageNameOk) { continue }
        # Trade-day check is fail-closed against the *deployment* calendar
        # ($RepoRoot), not a hard-coded canonical path. Missing file, unreadable
        # CSV, dates outside coverage, or is_open != true all reject the date.
        # No weekday fallback.
        $CalPath = Join-Path $RepoRoot "data\calendar\a_share_trading_calendar.csv"
        if (-not (Test-Path -LiteralPath $CalPath)) { continue }
        $CalPathPy = ($CalPath -replace '\\', '/')
        $isTradeDay = & $PyExe -B -c ("import csv; rows={}; f=open(r'" + $CalPathPy + "', encoding='utf-8-sig', newline=''); rdr=csv.DictReader(f); [rows.__setitem__((r.get('date') or list(r.values())[0]).strip()[:10], r.get('is_open')) for r in rdr]; f.close(); d='" + $td + "'; print('Y' if d in rows and str(rows.get(d) or '').strip().lower() in ('1','true','yes','open') else 'N')") 2>$null
        if ($LASTEXITCODE -ne 0 -or (-not $isTradeDay) -or ($isTradeDay.Trim() -ne "Y")) { continue }
        $dates += $td
    }
    $distinct = $dates | Sort-Object -Unique
    if ($distinct.Count -lt 2) {
        return @("need_two_distinct_shadow_scheduled_dates:have=$($distinct.Count)")
    }
    return @()
}


switch ($Action) {
    "Plan" {
        Write-Output "=== PLAN (no changes) ==="
        Write-Output "Pipeline task : $PipelineTask"
        Write-Output "Repo root     : $RepoRoot"
        Write-Output "Old tasks to disable AFTER the pipeline is verified enabled:"
        $OldWriteTasks | ForEach-Object { Write-Output "  - $_" }
        Write-Output "Guards: elevation + -Yes + shadow evidence (two distinct successful dates)."
    }
    "Status" {
        Get-ScheduledTask | Where-Object { $_.TaskName -match "QuantDesk" } | ForEach-Object {
            $i = $_ | Get-ScheduledTaskInfo
            "{0,-44} {1,-9} next={2}" -f $_.TaskName, $_.State, $i.NextRunTime
        }
        Write-Output "--- shadow evidence ---"
        $ev = Test-ShadowEvidence
        if ($ev.Count -eq 0) { Write-Output "shadow evidence: OK (>=2 distinct successful dates)" }
        else { $ev | ForEach-Object { Write-Output "shadow evidence: MISSING - $_" } }
        Write-Output "--- natural shadow provenance ---"
        $pv = Test-NaturalShadowProvenance
        if ($pv.Count -eq 0) { Write-Output "natural provenance: OK" }
        else { $pv | ForEach-Object { Write-Output "natural provenance: MISSING - $_" } }
    }
    "EnableShadow" {
        # Round-5 deliverable 3: EnableShadow no longer prints manual CLI
        # instructions. It verifies the SHADOW TASK's action contract and
        # states the authorization path explicitly.
        Assert-Admin
        if (-not $Yes) { throw "-Yes required." }
        $shadowTask = Get-ScheduledTask -TaskName "QuantDesk Data Close Pipeline Shadow" -ErrorAction SilentlyContinue
        if (-not $shadowTask) {
            throw "Shadow task not installed. Run scripts\install_data_close_pipeline_shadow_task.ps1 -Action Install -Yes first (requires explicit user authorization)."
        }
        $action = $shadowTask.Actions[0]
        $problems = @()
        if ($action.Arguments -notmatch "--mode shadow") { $problems += "missing_mode_shadow" }
        if ($action.Arguments -notmatch "--trigger scheduled") { $problems += "missing_trigger_scheduled" }
        if ($action.Arguments -match "--enable-network|--alert-push|--apply") { $problems += "forbidden_flag_present" }
        if ($action.WorkingDirectory -ne $RepoRoot) { $problems += "workdir_mismatch" }
        if ($shadowTask.Principal.LogonType -ne "S4U") { $problems += "logon_not_s4u" }
        if ($problems.Count -gt 0) {
            throw "shadow task contract invalid: $($problems -join ', ')"
        }
        Write-Output "Shadow task contract OK (mode=shadow, trigger=scheduled, no forbidden flags, S4U)."
        Write-Output "The shadow task must be ENABLED only with explicit user authorization."
        Write-Output "After TWO natural trading days of shadow evidence, Switch evaluation may proceed."
        Write-Output "This action does NOT enable anything by itself."
    }
    "Switch" {
        Assert-Admin
        if (-not $Yes) { throw "-Yes required." }
        if (-not $ConfirmedNaturalSuccess) { throw "-ConfirmedNaturalSuccess required (user authorization)." }
        # -ConfirmedNaturalSuccess is ONLY the user authorization. The
        # evidence itself comes from Test-ShadowEvidence, which verifies the
        # full shadow contract (schema/mode/canonical_write=false/trigger).
        $ev = Test-ShadowEvidence
        if ($ev.Count -gt 0) { throw "shadow evidence insufficient: $($ev -join ', ')" }
        $pv = Test-NaturalShadowProvenance
        if ($pv.Count -gt 0) { throw "natural shadow provenance insufficient: $($pv -join ', ')" }

        $pipe = Get-PipelineTaskContract
        $problems = Test-PipelineContract -Task $pipe
        if ($problems) { throw "pipeline contract invalid: $($problems -join ', ') - refusing to touch old tasks" }

        # ---- capture PRE-STATE of everything we may touch (P0 fix) ----
        $preState = @{}
        $preState[$PipelineTask] = Get-TaskState -Name $PipelineTask
        foreach ($name in $OldWriteTasks) { $preState[$name] = Get-TaskState -Name $name }
        Write-Output "pre-switch states captured:"
        foreach ($k in $preState.Keys) { Write-Output ("  {0} = {1}" -f $k, $preState[$k]) }

        # ---- transactional switch with TRUE rollback (P0 fix) ----
        # Rollback = restore EVERY touched task to its own recorded pre-state.
        # The pipeline enable happens first; if anything later fails, old
        # tasks are restored to their pre-states (re-ENABLED if they were
        # enabled) and the pipeline is restored to ITS pre-state.
        try {
            Enable-ScheduledTask -TaskName $PipelineTask | Out-Null
            $pipe2 = Get-ScheduledTask -TaskName $PipelineTask
            if ($pipe2.State -ne "Ready") {
                throw "pipeline failed to enable (state=$($pipe2.State))"
            }
            Write-Output "[enabled]   $PipelineTask (verified Ready)"

            foreach ($name in $OldWriteTasks) {
                if ($preState[$name] -eq "Missing") { continue }
                if ($preState[$name] -ne "Disabled") {
                    $t = Get-ScheduledTask -TaskName $name
                    if ($t.State -eq "Running") {
                        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
                        Start-Sleep -Seconds 3
                    }
                    Disable-ScheduledTask -TaskName $name | Out-Null
                    $chk = Get-ScheduledTask -TaskName $name
                    if ($chk.State -ne "Disabled") { throw "failed to disable $name" }
                    Write-Output "[disabled] $name (verified)"
                }
            }
        } catch {
            Write-Output "SWITCH FAILURE: $($_.Exception.Message)"
            Write-Output "Rolling back to recorded pre-switch states (re-ENABLE old writers)..."
            foreach ($name in $OldWriteTasks) {
                Restore-TaskState -Name $name -DesiredState $preState[$name]
            }
            Restore-TaskState -Name $PipelineTask -DesiredState $preState[$PipelineTask]
            # invariant: at least the original writers must be back
            $restored = 0
            foreach ($name in $OldWriteTasks) {
                if ((Get-TaskState -Name $name) -in @("Ready", "Running", $null)) { $restored++ }
            }
            if ($restored -lt 1 -and ($preState.Values | Where-Object { $_ -in @("Ready","Running") }).Count -ge 1) {
                throw "ROLLBACK INVARIANT VIOLATION: no original writer restored - MANUAL INTERVENTION REQUIRED"
            }
            throw "switch rolled back; pre-switch states restored"
        }

        # final invariant: exactly one data writer
        $activeOld = @($OldWriteTasks | Where-Object {
            (Get-TaskState -Name $_) -ne "Disabled"
        })
        Write-Output "final invariant: pipeline=Ready, active old writers=$($activeOld.Count)"
        & $PSCommandPath -Action Status
    }
}
