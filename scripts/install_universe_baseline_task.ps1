[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [ValidateSet("Plan", "Status", "Install", "Remove")]
    [string]$Action = "Plan",
    [string]$TaskName = "QuantDesk Universe Baseline Refresh",
    [switch]$Yes,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
if ($Yes) { $ConfirmPreference = "None" }
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Script = Join-Path $Root "scripts\refresh_universe_baseline.py"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Execute = if (Test-Path -LiteralPath $Python -PathType Leaf) { $Python } else { "python.exe" }
$Arguments = '-B "{0}"' -f $Script
$Marker = "QuantDesk repository-owned universe baseline task; schema=2;"

function Invoke-Schtasks {
    param([string[]]$Values)
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = Join-Path $env:SystemRoot "System32\schtasks.exe"
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.Arguments = (($Values | ForEach-Object { '"' + ([string]$_).Replace('"', '\"') + '"' }) -join " ")
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    [pscustomobject]@{ ExitCode = $process.ExitCode; Stdout = $stdout; Stderr = $stderr }
}

function New-Payload {
    param(
        [string]$RequestedAction,
        [bool]$Exists,
        [string]$State = "",
        [string]$ObservedAction = ""
    )
    [ordered]@{
        schema_version = 2
        action = $RequestedAction.ToLowerInvariant()
        task_name = $TaskName
        exists = $Exists
        state = $State
        marker = $Marker
        expected_action = [ordered]@{
            execute = $Execute
            arguments = $Arguments
            working_directory = $Root
            source_script = $Script
        }
        observed_action = $ObservedAction
        repository_owned = [bool](
            $ObservedAction -and
            $ObservedAction.Contains($Script) -and
            -not $ObservedAction.Contains("codex\claude")
        )
    }
}

$query = Invoke-Schtasks -Values @("/Query", "/TN", $TaskName, "/XML")
$exists = $query.ExitCode -eq 0
$xml = [string]$query.Stdout

if ($Action -eq "Plan") {
    $payload = New-Payload -RequestedAction $Action -Exists $exists
    if ($Json) { $payload | ConvertTo-Json -Depth 8 } else { $payload }
    exit 0
}

if ($Action -eq "Status") {
    $stateResult = Invoke-Schtasks -Values @("/Query", "/TN", $TaskName, "/FO", "LIST", "/V")
    $payload = New-Payload -RequestedAction $Action -Exists $exists `
        -State ([string]$stateResult.Stdout) -ObservedAction $xml
    if ($Json) { $payload | ConvertTo-Json -Depth 8 } else { $payload }
    exit 0
}

if ($Action -eq "Remove") {
    if ($exists -and $PSCmdlet.ShouldProcess($TaskName, "Remove repository-owned universe baseline task")) {
        $result = Invoke-Schtasks -Values @("/Delete", "/TN", $TaskName, "/F")
        if ($result.ExitCode -ne 0) { throw $result.Stderr }
    }
    exit 0
}

if (-not $PSCmdlet.ShouldProcess($TaskName, "Install repository-owned universe baseline task")) {
    exit 0
}

$boundary = (Get-Date).Date.AddDays(1)
while ($boundary.DayOfWeek -eq [System.DayOfWeek]::Saturday -or
       $boundary.DayOfWeek -eq [System.DayOfWeek]::Sunday) {
    $boundary = $boundary.AddDays(1)
}
$boundaryText = $boundary.ToString("yyyy-MM-ddT15:30:00")
$taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>$Marker source=$Script</Description>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>$([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value)</UserId>
      <LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
  <Settings>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
  </Settings>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>$boundaryText</StartBoundary>
      <ScheduleByWeek>
        <WeeksInterval>1</WeeksInterval>
        <DaysOfWeek><Monday/><Tuesday/><Wednesday/><Thursday/><Friday/></DaysOfWeek>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>$Execute</Command>
      <Arguments>$Arguments</Arguments>
      <WorkingDirectory>$Root</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
$temp = [System.IO.Path]::GetTempFileName()
try {
    $encoding = New-Object System.Text.UnicodeEncoding($false, $true)
    [System.IO.File]::WriteAllText($temp, $taskXml, $encoding)
    $result = Invoke-Schtasks -Values @("/Create", "/TN", $TaskName, "/XML", $temp, "/F")
    if ($result.ExitCode -ne 0) { throw $result.Stderr }
} finally {
    Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
}
if ($Json) {
    (New-Payload -RequestedAction $Action -Exists $true) | ConvertTo-Json -Depth 8
}
exit 0
