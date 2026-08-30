[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [ValidateSet("Plan", "Status", "Install", "Disable", "Remove")]
    [string]$Action = "Plan",
    [string]$TaskName = "QuantDesk Daily Review Routine",
    [string]$StatusFixturePath = "",
    [switch]$Yes,
    [switch]$Json
)

# The runner invokes the exactly-once orchestrator. It remains offline by
# contract and consumes only artifacts already published by the data chain.
$ErrorActionPreference = "Stop"
if ($Yes) { $ConfirmPreference = "None" }
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Runner = Join-Path $Root "scripts\run_daily_review_routine.cmd"
$Marker = "QuantDesk managed daily-review-routine task; schema=1"
$Execute = if ($env:ComSpec) { $env:ComSpec } else { "C:\Windows\System32\cmd.exe" }
$Arguments = '/d /s /c ""{0}" --with-guardrails"' -f $Runner
$CurrentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$StatusFixture = $null
$FixtureTaskXmlPhase = "before"

if (-not [string]::IsNullOrWhiteSpace($StatusFixturePath)) {
    if ($Action -notin @("Status", "Disable")) {
        throw "StatusFixturePath is valid only with -Action Status or Disable."
    }
    $ResolvedFixture = (Resolve-Path -LiteralPath $StatusFixturePath).Path
    $StatusFixture = Get-Content -Raw -Encoding UTF8 -LiteralPath $ResolvedFixture | ConvertFrom-Json
    if ($null -eq $StatusFixture.PSObject.Properties["task_xml_before"] -or $null -eq $StatusFixture.PSObject.Properties["state"]) {
        throw "Status fixture must define task_xml_before and numeric state."
    }
    $FixtureState = 0
    if (-not [int]::TryParse([string]$StatusFixture.state, [ref]$FixtureState) -or $FixtureState -lt 0 -or $FixtureState -gt 4) {
        throw "Status fixture state must be a Task Scheduler state code from 0 through 4."
    }
    if ($Action -eq "Disable" -and $null -eq $StatusFixture.PSObject.Properties["task_xml_after_disable"]) {
        throw "Disable fixture must define task_xml_after_disable."
    }
}

function Escape-Xml {
    param([string]$Value)
    return [System.Security.SecurityElement]::Escape($Value)
}

function ConvertTo-WindowsPathKey {
    param([string]$Value)
    $Text = ([string]$Value).Trim().Trim('"').Replace('/', '\')
    if ([string]::IsNullOrWhiteSpace($Text) -or -not [System.IO.Path]::IsPathRooted($Text)) { return "" }
    try { $Text = [System.IO.Path]::GetFullPath($Text) } catch { return "" }
    return $Text.TrimEnd('\').ToLowerInvariant()
}

# The offline routine needs no credential, so there is deliberately no
# environment-variable presence gate here. Reporting a credential field at all
# would invite a reader to believe this task can deliver externally.

function Invoke-Schtasks {
    param([string[]]$Values)
    if ($null -ne $StatusFixture) {
        throw "Fixture mode forbids invoking schtasks.exe."
    }
    $Info = New-Object System.Diagnostics.ProcessStartInfo
    $Info.FileName = Join-Path $env:SystemRoot "System32\schtasks.exe"
    $Info.UseShellExecute = $false
    $Info.CreateNoWindow = $true
    $Info.RedirectStandardOutput = $true
    $Info.RedirectStandardError = $true
    $Info.Arguments = (($Values | ForEach-Object { '"' + ([string]$_).Replace('"', '\"') + '"' }) -join " ")
    $Process = New-Object System.Diagnostics.Process
    $Process.StartInfo = $Info
    [void]$Process.Start()
    $Stdout = $Process.StandardOutput.ReadToEnd()
    $Stderr = $Process.StandardError.ReadToEnd()
    $Process.WaitForExit()
    return [pscustomobject]@{ ExitCode = $Process.ExitCode; Stdout = $Stdout; Stderr = $Stderr }
}

function Get-TaskXml {
    if ($null -ne $StatusFixture) {
        if ($FixtureTaskXmlPhase -eq "after_disable") {
            return [string]$StatusFixture.task_xml_after_disable
        }
        return [string]$StatusFixture.task_xml_before
    }
    $Result = Invoke-Schtasks -Values @("/Query", "/TN", $TaskName, "/XML", "ONE")
    if ($Result.ExitCode -ne 0) { return "" }
    return $Result.Stdout
}

function Get-TaskState {
    if ($null -ne $StatusFixture) { return [int]$StatusFixture.state }
    $Service = $null
    try {
        $Service = New-Object -ComObject "Schedule.Service"
        $Service.Connect()
        $Task = $Service.GetFolder("\").GetTask($TaskName)
        return [int]$Task.State
    } catch {
        return 0
    } finally {
        if ($null -ne $Service -and [System.Runtime.InteropServices.Marshal]::IsComObject($Service)) {
            [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($Service)
        }
    }
}

function Get-TaskContract {
    param([string]$TaskXml)
    if ([string]::IsNullOrWhiteSpace($TaskXml)) { return $null }
    try {
        [xml]$Document = $TaskXml
        $Ns = New-Object System.Xml.XmlNamespaceManager($Document.NameTable)
        $Ns.AddNamespace("task", "http://schemas.microsoft.com/windows/2004/02/mit/task")
        $Exec = $Document.SelectSingleNode("/task:Task/task:Actions/task:Exec", $Ns)
        $Description = $Document.SelectSingleNode("/task:Task/task:RegistrationInfo/task:Description", $Ns)
        $Trigger = $Document.SelectSingleNode("/task:Task/task:Triggers/task:CalendarTrigger", $Ns)
        $Principal = $Document.SelectSingleNode("/task:Task/task:Principals/task:Principal", $Ns)
        $Settings = $Document.SelectSingleNode("/task:Task/task:Settings", $Ns)
        if ($null -eq $Exec -or $null -eq $Trigger -or $null -eq $Principal -or $null -eq $Settings) {
            throw "Task XML is missing a required node."
        }
        $ObservedExecute = [string]$Exec.SelectSingleNode("task:Command", $Ns).InnerText
        $ObservedArguments = [string]$Exec.SelectSingleNode("task:Arguments", $Ns).InnerText
        $ObservedRoot = [string]$Exec.SelectSingleNode("task:WorkingDirectory", $Ns).InnerText
        $ObservedDescription = if ($null -ne $Description) { [string]$Description.InnerText } else { "" }
        $ObservedStart = [string]$Trigger.SelectSingleNode("task:StartBoundary", $Ns).InnerText
        $ObservedDays = @($Trigger.SelectNodes("task:ScheduleByWeek/task:DaysOfWeek/*", $Ns) | ForEach-Object { $_.LocalName })
        $ExpectedDays = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
        $DaysMatch = $ObservedDays.Count -eq 5 -and @(Compare-Object $ObservedDays $ExpectedDays).Count -eq 0
        $Logon = [string]$Principal.SelectSingleNode("task:LogonType", $Ns).InnerText
        $RunLevelNode = $Principal.SelectSingleNode("task:RunLevel", $Ns)
        $RunLevel = if ($null -eq $RunLevelNode -or [string]::IsNullOrWhiteSpace([string]$RunLevelNode.InnerText)) {
            "LeastPrivilege"
        } else { [string]$RunLevelNode.InnerText }
        $Multiple = [string]$Settings.SelectSingleNode("task:MultipleInstancesPolicy", $Ns).InnerText
        $StartWhenAvailable = [string]$Settings.SelectSingleNode("task:StartWhenAvailable", $Ns).InnerText
        $Limit = [string]$Settings.SelectSingleNode("task:ExecutionTimeLimit", $Ns).InnerText
        $EnabledNode = $Settings.SelectSingleNode("task:Enabled", $Ns)
        $RestartIntervalNode = $Settings.SelectSingleNode("task:RestartOnFailure/task:Interval", $Ns)
        $RestartCountNode = $Settings.SelectSingleNode("task:RestartOnFailure/task:Count", $Ns)
        # Task Scheduler omits default-valued settings when it normalizes XML.
        # Settings/Enabled therefore defaults to true when the node is absent.
        $Enabled = if ($null -eq $EnabledNode) { $true } else { [string]$EnabledNode.InnerText -eq "true" }
        $RestartInterval = if ($null -ne $RestartIntervalNode) { [string]$RestartIntervalNode.InnerText } else { "" }
        $RestartCount = if ($null -ne $RestartCountNode) { [string]$RestartCountNode.InnerText } else { "" }
        $OwnerMatches = [string]::Equals($ObservedDescription, $Marker, [System.StringComparison]::Ordinal)
        $ActionMatches = (
            (ConvertTo-WindowsPathKey $ObservedExecute) -eq (ConvertTo-WindowsPathKey $Execute) -and
            [string]::Equals($ObservedArguments.Trim(), $Arguments.Trim(), [System.StringComparison]::OrdinalIgnoreCase) -and
            (ConvertTo-WindowsPathKey $ObservedRoot) -eq (ConvertTo-WindowsPathKey $Root)
        )
        $TriggerMatches = $DaysMatch -and $ObservedStart -match 'T20:00:00$'
        $SettingsMatches = (
            $Logon -eq "S4U" -and $RunLevel -eq "LeastPrivilege" -and
            $Multiple -eq "IgnoreNew" -and $StartWhenAvailable -eq "true" -and
            $Limit -eq "PT2H" -and $Enabled -and
            $RestartInterval -eq "PT5M" -and $RestartCount -eq "2"
        )
        return [pscustomobject]@{
            execute = $ObservedExecute; arguments = $ObservedArguments; working_directory = $ObservedRoot
            description = $ObservedDescription; start_boundary = $ObservedStart; days = $ObservedDays
            logon_type = $Logon; run_level = $RunLevel; multiple_instances = $Multiple
            start_when_available = $StartWhenAvailable; execution_time_limit = $Limit
            enabled = [bool]$Enabled; restart_interval = $RestartInterval; restart_count = $RestartCount
            OwnershipMarkerMatches = [bool]$OwnerMatches; ActionMatches = [bool]$ActionMatches
            TriggerMatches = [bool]$TriggerMatches; SettingsMatches = [bool]$SettingsMatches; error = ""
        }
    } catch {
        return [pscustomobject]@{
            execute = ""; arguments = ""; working_directory = ""; description = ""; start_boundary = ""
            days = @(); logon_type = ""; run_level = ""; multiple_instances = ""
            start_when_available = ""; execution_time_limit = ""; OwnershipMarkerMatches = $false
            enabled = $false; restart_interval = ""; restart_count = ""
            ActionMatches = $false; TriggerMatches = $false; SettingsMatches = $false
            error = "task_contract_parse_failed:$($_.Exception.GetType().Name)"
        }
    }
}

function Get-NextBoundary {
    # 20:00 post-close, not the morning brief's 08:35: this file started as a copy
    # of that installer and the hour must track the verifier below, which requires
    # T20:00:00. A stale hour here installs a real task whose trigger silently
    # fails its own contract check.
    $Candidate = (Get-Date).Date.AddHours(20)
    if ($Candidate -le (Get-Date)) { $Candidate = $Candidate.AddDays(1) }
    while ($Candidate.DayOfWeek -eq "Saturday" -or $Candidate.DayOfWeek -eq "Sunday") { $Candidate = $Candidate.AddDays(1) }
    return $Candidate.ToString("yyyy-MM-ddTHH:mm:ss")
}

function ConvertTo-StateCode {
    # Task Scheduler's COM API exposes a locale-independent numeric enum:
    # unknown=0, disabled=1, queued=2, ready=3, running=4.
    param([object]$State)
    if ($null -eq $State) { return "not_queried" }
    $Code = 0
    if (-not [int]::TryParse([string]$State, [ref]$Code)) { return "unknown" }
    switch ($Code) {
        1       { return "disabled" }
        2       { return "queued" }
        3       { return "ready" }
        4       { return "running" }
        default { return "unknown" }
    }
}

function New-Result {
    param([string]$RequestedAction, [bool]$Exists = $false, [object]$State = $null, [object]$Observed = $null)
    $HasObserved = $null -ne $Observed
    $Managed = $HasObserved -and [bool]$Observed.OwnershipMarkerMatches
    $ActionMatches = if ($HasObserved) { [bool]$Observed.ActionMatches } else { $null }
    $TriggerMatches = if ($HasObserved) { [bool]$Observed.TriggerMatches } else { $null }
    $SettingsMatches = if ($HasObserved) { [bool]$Observed.SettingsMatches } else { $null }
    $XmlEnabled = $HasObserved -and [bool]$Observed.enabled
    $CanonicalState = ConvertTo-StateCode -State $State
    $OperationalState = $CanonicalState -in @("queued", "ready", "running")
    $Enabled = $XmlEnabled -and $CanonicalState -ne "disabled"
    # No credential term: an offline routine is ready when the task contract
    # matches. Requiring a credential here would make "ready" unattainable.
    $InstalledReady = $Exists -and $Managed -and $ActionMatches -and $TriggerMatches -and $SettingsMatches -and $Enabled -and $OperationalState
    # Only comparison verdicts are published. The expected and observed action
    # details stay internal: they are what the contract booleans are computed
    # from, and reprinting them is how an untrusted task definition would get a
    # channel into our own output.
    return [ordered]@{
        schema_version = 1; action = $RequestedAction.ToLowerInvariant(); task_name = $TaskName
        installed = [bool]$Exists; exists = [bool]$Exists; managed = [bool]$Managed
        installed_ready = [bool]$InstalledReady
        state_code = if ($Exists -and $HasObserved -and (-not $Enabled)) { "disabled" } else { $CanonicalState }
        status_source = if ($RequestedAction -eq "Status") { if ($null -ne $StatusFixture) { "fixture" } else { "system" } } else { $null }
        network_authorized = $false
        offline_by_contract = $true
        contract_parse_failed = if ($HasObserved) { -not [string]::IsNullOrWhiteSpace([string]$Observed.error) } else { $null }
        action_contract_matches = $ActionMatches; trigger_contract_matches = $TriggerMatches
        settings_contract_matches = $SettingsMatches
        triggers = @([ordered]@{ kind = "weekly"; days = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"); at = "20:00" })
        settings = [ordered]@{ start_when_available = $true; multiple_instances = "IgnoreNew"; execution_time_limit = "PT2H"; run_level = "LeastPrivilege"; enabled = [bool]$Enabled; restart_interval = "PT5M"; restart_count = 2 }
        ownership_marker = $Marker; registration_authorized = $false
    }
}

function Write-Result {
    param([object]$Payload)
    if ($Json) { $Payload | ConvertTo-Json -Depth 8 -Compress } else { $Payload | ConvertTo-Json -Depth 8 }
}

function New-TaskXmlText {
    $DescriptionXml = Escape-Xml $Marker
    $SidXml = Escape-Xml $CurrentSid
    $ExecuteXml = Escape-Xml $Execute
    $ArgumentsXml = Escape-Xml $Arguments
    $RootXml = Escape-Xml $Root
    $Boundary = Get-NextBoundary
    return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>$DescriptionXml</Description></RegistrationInfo>
  <Triggers><CalendarTrigger><StartBoundary>$Boundary</StartBoundary><Enabled>true</Enabled><ScheduleByWeek><DaysOfWeek><Monday/><Tuesday/><Wednesday/><Thursday/><Friday/></DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek></CalendarTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>$SidXml</UserId><LogonType>S4U</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable><AllowHardTerminate>true</AllowHardTerminate><ExecutionTimeLimit>PT2H</ExecutionTimeLimit><RestartOnFailure><Interval>PT5M</Interval><Count>2</Count></RestartOnFailure><Enabled>true</Enabled><Priority>7</Priority></Settings>
  <Actions Context="Author"><Exec><Command>$ExecuteXml</Command><Arguments>$ArgumentsXml</Arguments><WorkingDirectory>$RootXml</WorkingDirectory></Exec></Actions>
</Task>
"@
}

if (-not (Test-Path -LiteralPath $Runner)) { throw "Runner not found: $Runner" }
if ($Action -eq "Plan") {
    Write-Result (New-Result -RequestedAction $Action)
    exit 0
}

$ExistingXml = Get-TaskXml
$Exists = -not [string]::IsNullOrWhiteSpace($ExistingXml)
$Observed = Get-TaskContract -TaskXml $ExistingXml
$Managed = $Exists -and $null -ne $Observed -and $Observed.OwnershipMarkerMatches

if ($Action -eq "Status") {
    $StateCode = if ($Exists) { Get-TaskState } else { $null }
    Write-Result (New-Result -RequestedAction $Action -Exists $Exists -State $StateCode -Observed $Observed)
    exit 0
}

if ($Action -eq "Install") {
    if (-not $Yes) { Write-Error "Install requires -Yes for non-interactive confirmation."; exit 2 }
    # No network or credential precondition: the routine is offline by contract.
    if ($Exists -and (-not $Managed)) { Write-Error "A same-name unmanaged task exists; refusing to overwrite it."; exit 3 }
    if ($PSCmdlet.ShouldProcess($TaskName, "Install managed daily review task")) {
        $TempPath = Join-Path $env:TEMP ("quantdesk-daily-review-" + [guid]::NewGuid().ToString("N") + ".xml")
        try {
            $Utf16LeBom = New-Object System.Text.UnicodeEncoding($false, $true)
            [System.IO.File]::WriteAllText($TempPath, (New-TaskXmlText), $Utf16LeBom)
            $Create = Invoke-Schtasks -Values @("/Create", "/TN", $TaskName, "/XML", $TempPath, "/F")
            if ($Create.ExitCode -ne 0) { throw "schtasks.exe /Create failed with exit code $($Create.ExitCode)." }
        } finally { Remove-Item -LiteralPath $TempPath -Force -ErrorAction SilentlyContinue }
    }
    $InstalledXml = Get-TaskXml
    $Installed = -not [string]::IsNullOrWhiteSpace($InstalledXml)
    $InstalledContract = Get-TaskContract -TaskXml $InstalledXml
    $InstalledState = if ($Installed) { Get-TaskState } else { $null }
    $Result = New-Result -RequestedAction $Action -Exists $Installed -State $InstalledState -Observed $InstalledContract
    if ($Result.installed_ready) { $Result.registration_authorized = $true }
    Write-Result $Result
    exit $(if ($Result.installed_ready) { 0 } else { 4 })
}

if ($Action -eq "Disable") {
    if (-not $Yes) { Write-Error "Disable requires -Yes for non-interactive confirmation."; exit 2 }
    if ($Exists -and (-not $Managed)) { Write-Error "A same-name unmanaged task exists; refusing to disable it."; exit 3 }
    if ($Exists -and $PSCmdlet.ShouldProcess($TaskName, "Disable managed daily review task")) {
        if ($null -ne $StatusFixture) {
            $FixtureTaskXmlPhase = "after_disable"
        } else {
            $Change = Invoke-Schtasks -Values @("/Change", "/TN", $TaskName, "/Disable")
            if ($Change.ExitCode -ne 0) { throw "schtasks.exe /Change failed with exit code $($Change.ExitCode)." }
        }
    }
    $DisabledXml = Get-TaskXml
    $DisabledExists = -not [string]::IsNullOrWhiteSpace($DisabledXml)
    $DisabledObserved = Get-TaskContract -TaskXml $DisabledXml
    $DisabledState = if ($DisabledExists) { Get-TaskState } else { $null }
    $Result = New-Result -RequestedAction $Action -Exists $DisabledExists -State $DisabledState -Observed $DisabledObserved
    Write-Result $Result
    $DisableVerified = (-not $Exists) -or ($DisabledExists -and $null -ne $DisabledObserved -and (-not [bool]$DisabledObserved.enabled))
    exit $(if ($DisableVerified) { 0 } else { 4 })
}

if ($Action -eq "Remove") {
    if (-not $Yes) { Write-Error "Remove requires -Yes for non-interactive confirmation."; exit 2 }
    if ($Exists -and (-not $Managed)) { Write-Error "A same-name unmanaged task exists; refusing to remove it."; exit 3 }
    if ($Exists -and $PSCmdlet.ShouldProcess($TaskName, "Remove managed daily review task")) {
        $Delete = Invoke-Schtasks -Values @("/Delete", "/TN", $TaskName, "/F")
        if ($Delete.ExitCode -ne 0) { throw "schtasks.exe /Delete failed with exit code $($Delete.ExitCode)." }
    }
    Write-Result (New-Result -RequestedAction $Action)
    exit 0
}
