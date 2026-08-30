[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [ValidateSet("Plan", "Status", "Install", "Remove")]
    [string]$Action = "Plan",
    [string]$TaskName = "QuantDesk Full A Daily Reconciliation",
    [ValidateSet("S4U")]
    [string]$LogonMode = "S4U",
    [switch]$Yes,
    [switch]$Json
)

# Managed installer for the full-A daily reconciliation task.
#
# Two weekday triggers by design (20:35 and 22:30). The daily chain owns the
# shared daily-data lock during its own window, so the first trigger can hit
# `deferred_upstream_busy` and exit without doing any provider work. The second
# trigger then resumes the SAME target date. That is why the second run is not a
# duplicate: it is the retry path for a deferral.
#
# The action carries --enable-network --apply because a reconciliation that may
# not write cannot close its own contract. No SendKey appears in the action;
# the Server chan dispatcher loads its credential behind its own gates.
#
# This installer deliberately does NOT touch the legacy weekly Saturday task.
# Retiring that one is a separate decision that the plan gates behind one
# accepted natural run of THIS task.

$ErrorActionPreference = "Stop"
if ($Yes) {
    $ConfirmPreference = "None"
}
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Runner = Join-Path $Root "scripts\run_full_a_daily_reconciliation.cmd"
$Marker = "QuantDesk managed full-a-daily-reconciliation task; schema=1"
$Execute = if ($env:ComSpec) { $env:ComSpec } else { "C:\Windows\System32\cmd.exe" }
$Arguments = '/d /s /c ""{0}" --enable-network --apply --lock-timeout-seconds 1800 --json false"' -f $Runner
$CurrentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value

function Escape-Xml {
    param([string]$Value)
    return [System.Security.SecurityElement]::Escape($Value)
}

function Write-Payload {
    param([object]$Payload)
    if ($Json) {
        $Payload | ConvertTo-Json -Depth 8 -Compress
    } else {
        $Payload | ConvertTo-Json -Depth 8
    }
}

function New-TaskXmlText {
    $DateText = (Get-Date).ToString("yyyy-MM-dd")
    $DescriptionXml = Escape-Xml $Marker
    $SidXml = Escape-Xml $CurrentSid
    $ExecuteXml = Escape-Xml $Execute
    $ArgumentsXml = Escape-Xml $Arguments
    $RootXml = Escape-Xml $Root
    return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>$DescriptionXml</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>$($DateText)T20:35:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek><Monday/><Tuesday/><Wednesday/><Thursday/><Friday/></DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>
    <CalendarTrigger>
      <StartBoundary>$($DateText)T22:30:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek><Monday/><Tuesday/><Wednesday/><Thursday/><Friday/></DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$SidXml</UserId>
      <LogonType>S4U</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowHardTerminate>true</AllowHardTerminate>
    <ExecutionTimeLimit>PT3H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$ExecuteXml</Command>
      <Arguments>$ArgumentsXml</Arguments>
      <WorkingDirectory>$RootXml</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
}

if (-not (Test-Path -LiteralPath $Runner)) {
    throw "Runner not found: $Runner"
}

if ($Action -eq "Plan") {
    Write-Payload ([ordered]@{
        action       = "Plan"
        task_name    = $TaskName
        runner       = $Runner
        execute      = $Execute
        arguments    = $Arguments
        working_dir  = $Root
        logon_mode   = $LogonMode
        triggers     = @("Mon-Fri 20:35", "Mon-Fri 22:30 (retry path for a deferral)")
        touches_legacy_saturday_task = $false
        note         = "Plan only; nothing was registered."
    })
    exit 0
}

if ($Action -eq "Status") {
    $Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $Existing) {
        Write-Payload ([ordered]@{ action = "Status"; task_name = $TaskName; registered = $false })
        exit 0
    }
    $Info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Payload ([ordered]@{
        action        = "Status"
        task_name     = $TaskName
        registered    = $true
        state         = [string]$Existing.State
        last_run_time = [string]$Info.LastRunTime
        last_result   = $Info.LastTaskResult
        next_run_time = [string]$Info.NextRunTime
        missed_runs   = $Info.NumberOfMissedRuns
    })
    exit 0
}

if ($Action -eq "Remove") {
    if ($PSCmdlet.ShouldProcess($TaskName, "Unregister scheduled task")) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Payload ([ordered]@{ action = "Remove"; task_name = $TaskName; removed = $true })
    }
    exit 0
}

# Install
$XmlText = New-TaskXmlText
if ($PSCmdlet.ShouldProcess($TaskName, "Register scheduled task")) {
    Register-ScheduledTask -TaskName $TaskName -Xml $XmlText -Force -ErrorAction Stop | Out-Null
    $Info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Payload ([ordered]@{
        action        = "Install"
        task_name     = $TaskName
        registered    = $true
        next_run_time = [string]$Info.NextRunTime
        arguments     = $Arguments
        working_dir   = $Root
        touches_legacy_saturday_task = $false
    })
}
exit 0
