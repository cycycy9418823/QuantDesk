[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [ValidateSet("Plan", "Status", "Install", "Remove")]
    [string]$Action = "Plan",
    [string]$TaskName = "QuantDesk Daily Data Chain",
    [string]$StatusFixturePath = "",
    # Round 8 裁定十八: S4U runs the task with no interactive desktop session, so
    # no visible console window is spawned that a person could close (the root
    # cause of the 0xC000013A crashes on 07-22/07-23).  Default stays Interactive
    # so this script change alone does NOT alter the live task; switching to S4U
    # requires an explicit -LogonMode S4U re-registration, which per 裁定十八 must
    # be run only with the user's in-person approval.
    [ValidateSet("Interactive", "S4U")]
    [string]$LogonMode = "Interactive",
    [switch]$EnableNetwork,
    [switch]$Yes,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
if ($Yes) {
    $ConfirmPreference = "None"
}
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Runner = Join-Path $Root "scripts\run_daily_data_chain_task.ps1"
$SupervisorDeadlineSeconds = 9900
$Marker = "QuantDesk managed daily-data-chain task; schema=1;"
$Description = "$Marker installer=$PSCommandPath"
$Execute = Join-Path $PSHOME "powershell.exe"
$Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -EnableNetwork' -f $Runner
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$CurrentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$StatusFixture = $null
if (-not [string]::IsNullOrWhiteSpace($StatusFixturePath)) {
    if ($Action -ne "Status") {
        throw "StatusFixturePath is valid only with -Action Status."
    }
    $ResolvedStatusFixturePath = (Resolve-Path -LiteralPath $StatusFixturePath).Path
    $StatusFixture = Get-Content -Raw -Encoding UTF8 -LiteralPath $ResolvedStatusFixturePath | ConvertFrom-Json
    if (
        $null -eq $StatusFixture.PSObject.Properties["task_xml"] -or
        $null -eq $StatusFixture.PSObject.Properties["state"]
    ) {
        throw "Status fixture must define task_xml and state."
    }
}

function Escape-Xml {
    param([string]$Value)
    return [System.Security.SecurityElement]::Escape($Value)
}

function Get-NextWeekdayBoundary {
    <#
        Windows keeps a weekly trigger's StartBoundary as the pattern anchor. When
        the task is re-registered after a trigger time has already passed today,
        a same-day boundary combined with StartWhenAvailable=true makes the
        scheduler treat that occurrence as a missed run and fire it immediately.
        Always anchor on the next strictly-future Monday-Friday occurrence.
    #>
    param(
        [int]$Hour,
        [int]$Minute,
        [datetime]$Now = (Get-Date)
    )

    $Candidate = Get-Date -Year $Now.Year -Month $Now.Month -Day $Now.Day `
        -Hour $Hour -Minute $Minute -Second 0 -Millisecond 0
    while (
        $Candidate -le $Now -or
        $Candidate.DayOfWeek -eq [System.DayOfWeek]::Saturday -or
        $Candidate.DayOfWeek -eq [System.DayOfWeek]::Sunday
    ) {
        $Candidate = $Candidate.AddDays(1)
    }
    return $Candidate.ToString("yyyy-MM-ddTHH:mm:ss")
}

$AfternoonBoundary = Get-NextWeekdayBoundary -Hour 15 -Minute 45
$EveningBoundary = Get-NextWeekdayBoundary -Hour 18 -Minute 30
$ExpectedLogonType = if ($LogonMode -eq "S4U") { "S4U" } else { "InteractiveToken" }
$ExpectedMultipleInstances = "Queue"
$ExpectedExecutionTimeLimit = "PT3H"
$ExpectedStartWhenAvailable = "true"

function Invoke-Schtasks {
    param([string[]]$Values)

    $Info = New-Object System.Diagnostics.ProcessStartInfo
    $Info.FileName = Join-Path $env:SystemRoot "System32\schtasks.exe"
    $Info.UseShellExecute = $false
    $Info.CreateNoWindow = $true
    $Info.RedirectStandardOutput = $true
    $Info.RedirectStandardError = $true
    $Quoted = foreach ($Value in $Values) {
        '"' + ([string]$Value).Replace('"', '\"') + '"'
    }
    $Info.Arguments = $Quoted -join " "
    $Process = New-Object System.Diagnostics.Process
    $Process.StartInfo = $Info
    [void]$Process.Start()
    $Stdout = $Process.StandardOutput.ReadToEnd()
    $Stderr = $Process.StandardError.ReadToEnd()
    $Process.WaitForExit()
    return [pscustomobject]@{
        ExitCode = $Process.ExitCode
        Stdout = $Stdout
        Stderr = $Stderr
    }
}

function Get-TaskXml {
    if ($null -ne $StatusFixture) {
        return [string]$StatusFixture.task_xml
    }
    $Result = Invoke-Schtasks -Values @("/Query", "/TN", $TaskName, "/XML", "ONE")
    if ($Result.ExitCode -ne 0) {
        return ""
    }
    return $Result.Stdout
}

function ConvertTo-WindowsPathKey {
    param([string]$Value)

    $Text = ([string]$Value).Trim().Trim('"').Replace('/', '\')
    if ([string]::IsNullOrWhiteSpace($Text)) {
        return ""
    }
    if (-not [System.IO.Path]::IsPathRooted($Text)) {
        return ""
    }
    try {
        $Text = [System.IO.Path]::GetFullPath($Text)
    } catch {
        # Keep the observed text so a malformed path fails the equality check.
    }
    return $Text.TrimEnd('\').ToLowerInvariant()
}

function Get-TaskActionContract {
    param([string]$TaskXml)

    if ([string]::IsNullOrWhiteSpace($TaskXml)) {
        return $null
    }
    try {
        [xml]$TaskDocument = $TaskXml
        $NamespaceManager = New-Object System.Xml.XmlNamespaceManager($TaskDocument.NameTable)
        $NamespaceManager.AddNamespace("task", "http://schemas.microsoft.com/windows/2004/02/mit/task")
        $ExecNode = $TaskDocument.SelectSingleNode("/task:Task/task:Actions/task:Exec", $NamespaceManager)
        $DescriptionNode = $TaskDocument.SelectSingleNode("/task:Task/task:RegistrationInfo/task:Description", $NamespaceManager)
        if ($null -eq $ExecNode) {
            throw "Task XML has no Exec action."
        }
        $CommandNode = $ExecNode.SelectSingleNode("task:Command", $NamespaceManager)
        $ArgumentsNode = $ExecNode.SelectSingleNode("task:Arguments", $NamespaceManager)
        $WorkingDirectoryNode = $ExecNode.SelectSingleNode("task:WorkingDirectory", $NamespaceManager)
        $ObservedExecute = if ($null -ne $CommandNode) { [string]$CommandNode.InnerText } else { "" }
        $ObservedArguments = if ($null -ne $ArgumentsNode) { [string]$ArgumentsNode.InnerText } else { "" }
        $ObservedWorkingDirectory = if ($null -ne $WorkingDirectoryNode) { [string]$WorkingDirectoryNode.InnerText } else { "" }
        $ObservedDescription = if ($null -ne $DescriptionNode) { [string]$DescriptionNode.InnerText } else { "" }
        # Beyond the Exec action, the install-time contract must also verify the
        # principal logon type, the overlap policy, the hard execution limit, the
        # missed-run policy and that no weekly StartBoundary is left in the past.
        $LogonTypeNode = $TaskDocument.SelectSingleNode("/task:Task/task:Principals/task:Principal/task:LogonType", $NamespaceManager)
        $MultipleInstancesNode = $TaskDocument.SelectSingleNode("/task:Task/task:Settings/task:MultipleInstancesPolicy", $NamespaceManager)
        $ExecutionTimeLimitNode = $TaskDocument.SelectSingleNode("/task:Task/task:Settings/task:ExecutionTimeLimit", $NamespaceManager)
        $StartWhenAvailableNode = $TaskDocument.SelectSingleNode("/task:Task/task:Settings/task:StartWhenAvailable", $NamespaceManager)
        $ObservedLogonType = if ($null -ne $LogonTypeNode) { ([string]$LogonTypeNode.InnerText).Trim() } else { "" }
        $ObservedMultipleInstances = if ($null -ne $MultipleInstancesNode) { ([string]$MultipleInstancesNode.InnerText).Trim() } else { "" }
        $ObservedExecutionTimeLimit = if ($null -ne $ExecutionTimeLimitNode) { ([string]$ExecutionTimeLimitNode.InnerText).Trim() } else { "" }
        $ObservedStartWhenAvailable = if ($null -ne $StartWhenAvailableNode) { ([string]$StartWhenAvailableNode.InnerText).Trim().ToLowerInvariant() } else { "" }
        $BoundaryNodes = $TaskDocument.SelectNodes("/task:Task/task:Triggers/task:CalendarTrigger/task:StartBoundary", $NamespaceManager)
        $ObservedStartBoundaries = @()
        $PastBoundaries = @()
        $Now = Get-Date
        foreach ($Node in $BoundaryNodes) {
            $BoundaryText = ([string]$Node.InnerText).Trim()
            $ObservedStartBoundaries += $BoundaryText
            $Parsed = [datetime]::MinValue
            $Parseable = [datetime]::TryParse(
                $BoundaryText,
                [System.Globalization.CultureInfo]::InvariantCulture,
                [System.Globalization.DateTimeStyles]::None,
                [ref]$Parsed
            )
            if ((-not $Parseable) -or $Parsed -le $Now) {
                $PastBoundaries += $BoundaryText
            }
        }
        $LogonTypeMatches = [string]::Equals(
            $ObservedLogonType,
            $ExpectedLogonType,
            [System.StringComparison]::OrdinalIgnoreCase
        )
        $MultipleInstancesMatches = [string]::Equals(
            $ObservedMultipleInstances,
            $ExpectedMultipleInstances,
            [System.StringComparison]::OrdinalIgnoreCase
        )
        $ExecutionTimeLimitMatches = [string]::Equals(
            $ObservedExecutionTimeLimit,
            $ExpectedExecutionTimeLimit,
            [System.StringComparison]::OrdinalIgnoreCase
        )
        $StartWhenAvailableMatches = ($ObservedStartWhenAvailable -eq $ExpectedStartWhenAvailable)
        $StartBoundariesInFuture = (
            $ObservedStartBoundaries.Count -gt 0 -and $PastBoundaries.Count -eq 0
        )
        $ExecuteMatches = (
            (ConvertTo-WindowsPathKey $ObservedExecute) -eq
            (ConvertTo-WindowsPathKey $Execute)
        )
        $ArgumentsMatch = [string]::Equals(
            $ObservedArguments.Trim(),
            $Arguments.Trim(),
            [System.StringComparison]::OrdinalIgnoreCase
        )
        $WorkingDirectoryMatches = (
            (ConvertTo-WindowsPathKey $ObservedWorkingDirectory) -eq
            (ConvertTo-WindowsPathKey $Root)
        )
        $OwnershipMarkerMatches = $ObservedDescription.StartsWith(
            $Marker,
            [System.StringComparison]::Ordinal
        )
        return [pscustomobject]@{
            execute = $ObservedExecute
            arguments = $ObservedArguments
            working_directory = $ObservedWorkingDirectory
            description = $ObservedDescription
            logon_type = $ObservedLogonType
            multiple_instances = $ObservedMultipleInstances
            execution_time_limit = $ObservedExecutionTimeLimit
            start_when_available = $ObservedStartWhenAvailable
            start_boundaries = $ObservedStartBoundaries
            past_start_boundaries = $PastBoundaries
            LogonTypeMatches = [bool]$LogonTypeMatches
            MultipleInstancesMatches = [bool]$MultipleInstancesMatches
            ExecutionTimeLimitMatches = [bool]$ExecutionTimeLimitMatches
            StartWhenAvailableMatches = [bool]$StartWhenAvailableMatches
            StartBoundariesInFuture = [bool]$StartBoundariesInFuture
            OwnershipMarkerMatches = [bool]$OwnershipMarkerMatches
            ActionMatches = [bool]($ExecuteMatches -and $ArgumentsMatch -and $WorkingDirectoryMatches)
            Matches = [bool](
                $ExecuteMatches -and $ArgumentsMatch -and $WorkingDirectoryMatches -and
                $LogonTypeMatches -and $MultipleInstancesMatches -and
                $ExecutionTimeLimitMatches -and $StartWhenAvailableMatches
            )
            error = ""
        }
    } catch {
        return [pscustomobject]@{
            execute = ""
            arguments = ""
            working_directory = ""
            description = ""
            logon_type = ""
            multiple_instances = ""
            execution_time_limit = ""
            start_when_available = ""
            start_boundaries = @()
            past_start_boundaries = @()
            LogonTypeMatches = $false
            MultipleInstancesMatches = $false
            ExecutionTimeLimitMatches = $false
            StartWhenAvailableMatches = $false
            StartBoundariesInFuture = $false
            OwnershipMarkerMatches = $false
            ActionMatches = $false
            Matches = $false
            error = "task_action_parse_failed:$($_.Exception.Message)"
        }
    }
}

function New-PlanPayload {
    param(
        [string]$RequestedAction,
        [bool]$Exists = $false,
        [bool]$Managed = $false,
        [string]$State = "",
        [object]$LastTaskResult = $null,
        [object]$ObservedAction = $null
    )
    $HasObservedAction = $null -ne $ObservedAction
    $ReportedExecute = if ($HasObservedAction) { [string]$ObservedAction.execute } else { $Execute }
    $ReportedArguments = if ($HasObservedAction) { [string]$ObservedAction.arguments } else { $Arguments }
    $ReportedWorkingDirectory = if ($HasObservedAction) { [string]$ObservedAction.working_directory } else { $Root }
    return [ordered]@{
        schema_version = 1
        action = $RequestedAction.ToLowerInvariant()
        task_name = $TaskName
        exists = $Exists
        managed = $Managed
        state = $State
        last_task_result = $LastTaskResult
        network_authorized = [bool]$EnableNetwork
        execute = $ReportedExecute
        arguments = $ReportedArguments
        working_directory = $ReportedWorkingDirectory
        expected_action = [ordered]@{
            execute = $Execute
            arguments = $Arguments
            working_directory = $Root
            logon_type = $ExpectedLogonType
            multiple_instances = $ExpectedMultipleInstances
            execution_time_limit = $ExpectedExecutionTimeLimit
            start_when_available = $ExpectedStartWhenAvailable
            start_boundaries = @($AfternoonBoundary, $EveningBoundary)
        }
        observed_action = if ($HasObservedAction) {
            [ordered]@{
                execute = [string]$ObservedAction.execute
                arguments = [string]$ObservedAction.arguments
                working_directory = [string]$ObservedAction.working_directory
                description = [string]$ObservedAction.description
                logon_type = [string]$ObservedAction.logon_type
                multiple_instances = [string]$ObservedAction.multiple_instances
                execution_time_limit = [string]$ObservedAction.execution_time_limit
                start_when_available = [string]$ObservedAction.start_when_available
                start_boundaries = @($ObservedAction.start_boundaries)
                past_start_boundaries = @($ObservedAction.past_start_boundaries)
                ownership_marker_matches = [bool]$ObservedAction.OwnershipMarkerMatches
                error = [string]$ObservedAction.error
            }
        } else { $null }
        action_contract_matches = if ($HasObservedAction) { [bool]$ObservedAction.ActionMatches } else { $null }
        settings_contract_matches = if ($HasObservedAction) {
            [ordered]@{
                logon_type = [bool]$ObservedAction.LogonTypeMatches
                multiple_instances = [bool]$ObservedAction.MultipleInstancesMatches
                execution_time_limit = [bool]$ObservedAction.ExecutionTimeLimitMatches
                start_when_available = [bool]$ObservedAction.StartWhenAvailableMatches
                start_boundaries_in_future = [bool]$ObservedAction.StartBoundariesInFuture
            }
        } else { $null }
        full_contract_matches = if ($HasObservedAction) { [bool]$ObservedAction.Matches } else { $null }
        status_source = if ($RequestedAction -eq "Status") {
            if ($null -ne $StatusFixture) { "fixture" } else { "system" }
        } else { $null }
        triggers = @(
            [ordered]@{ kind = "weekly"; days = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"); at = "15:45"; start_boundary = $AfternoonBoundary },
            [ordered]@{ kind = "weekly"; days = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"); at = "18:30"; start_boundary = $EveningBoundary },
            [ordered]@{ kind = "at_logon"; user = $CurrentUser }
        )
        settings = [ordered]@{
            start_when_available = $true
            multiple_instances = "Queue"
            execution_time_limit = "PT3H"
            logon_type = $ExpectedLogonType
            supervisor_deadline_seconds = $SupervisorDeadlineSeconds
            allow_start_on_batteries = $true
            do_not_stop_on_batteries = $true
            start_boundaries_always_future = $true
        }
        ownership_marker = $Marker
    }
}

function Write-Result {
    param([object]$Payload)
    if ($Json) {
        $Payload | ConvertTo-Json -Depth 8 -Compress
    } else {
        $Payload | ConvertTo-Json -Depth 8
    }
}

function New-TaskXmlText {
    $DescriptionXml = Escape-Xml $Description
    $SidXml = Escape-Xml $CurrentSid
    $ExecuteXml = Escape-Xml $Execute
    $ArgumentsXml = Escape-Xml $Arguments
    $RootXml = Escape-Xml $Root
    # Round 8 裁定十八: S4U runs the task whether or not the owning user is
    # interactively logged on and, crucially, on an isolated (non-interactive)
    # window station -- so no visible console window is created that can be
    # manually closed (the confirmed root cause of the 0xC000013A crashes).
    # Default stays InteractiveToken so nothing changes until S4U is explicitly
    # requested AND the re-registration is separately approved in person.
    $LogonTypeXml = if ($LogonMode -eq "S4U") { "S4U" } else { "InteractiveToken" }
    # The AtLogOn trigger only makes sense for an interactive principal; S4U is
    # decoupled from logon, so it is emitted only in Interactive mode.
    $LogonTriggerXml = if ($LogonMode -eq "S4U") {
        ""
    } else {
        @"
    <!-- AtLogOn trigger -->
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$SidXml</UserId>
    </LogonTrigger>
"@
    }
    return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>$DescriptionXml</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>$AfternoonBoundary</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek>
          <Monday/><Tuesday/><Wednesday/><Thursday/><Friday/>
        </DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>
    <CalendarTrigger>
      <StartBoundary>$EveningBoundary</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek>
          <Monday/><Tuesday/><Wednesday/><Thursday/><Friday/>
        </DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>
    $LogonTriggerXml
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$SidXml</UserId>
      <LogonType>$LogonTypeXml</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>Queue</MultipleInstancesPolicy>
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
    Write-Result (New-PlanPayload -RequestedAction $Action)
    exit 0
}

$ExistingXml = Get-TaskXml
$Exists = -not [string]::IsNullOrWhiteSpace($ExistingXml)
$ExistingAction = Get-TaskActionContract -TaskXml $ExistingXml
$Managed = $Exists -and $null -ne $ExistingAction -and $ExistingAction.OwnershipMarkerMatches

# Status must describe the task that is actually registered.  The live S4U
# registration is valid even when this read-only command is invoked without the
# optional ``-LogonMode S4U`` switch (the installer default remains Interactive
# for explicit installs).  Re-evaluate the contract against that observed mode;
# malformed/unsupported logon types still fail closed.
if (
    $Action -eq "Status" -and
    $null -ne $ExistingAction -and
    @("InteractiveToken", "S4U") -contains [string]$ExistingAction.logon_type
) {
    $ExpectedLogonType = [string]$ExistingAction.logon_type
    $ExistingAction = Get-TaskActionContract -TaskXml $ExistingXml
    $Managed = $Exists -and $null -ne $ExistingAction -and $ExistingAction.OwnershipMarkerMatches
}

if ($Action -eq "Status") {
    $StateText = ""
    if ($Exists) {
        if ($null -ne $StatusFixture) {
            $StateText = [string]$StatusFixture.state
        } else {
            $QueryResult = Invoke-Schtasks -Values @("/Query", "/TN", $TaskName, "/FO", "LIST", "/V")
            if ($QueryResult.ExitCode -eq 0) {
                $StateText = $QueryResult.Stdout
            }
        }
    }
    Write-Result (New-PlanPayload -RequestedAction $Action -Exists $Exists -Managed $Managed -State $StateText -ObservedAction $ExistingAction)
    exit 0
}

if ($Action -eq "Install") {
    if (-not $Yes) {
        Write-Error "Install requires -Yes for non-interactive confirmation."
        exit 2
    }
    if (-not $EnableNetwork) {
        Write-Error "Install requires -EnableNetwork because the task performs an explicit network data update."
        exit 2
    }
    if ($Exists -and (-not $Managed)) {
        Write-Error "A non-QuantDesk task already uses this name; refusing to overwrite it."
        exit 3
    }
    if ($PSCmdlet.ShouldProcess($TaskName, "Install or update managed daily data task")) {
        $TempPath = Join-Path $env:TEMP ("quantdesk-daily-data-chain-" + [guid]::NewGuid().ToString("N") + ".xml")
        try {
            $Utf16LeBom = New-Object System.Text.UnicodeEncoding($false, $true)
            [System.IO.File]::WriteAllText($TempPath, (New-TaskXmlText), $Utf16LeBom)
            $CreateResult = Invoke-Schtasks -Values @("/Create", "/TN", $TaskName, "/XML", $TempPath, "/F")
            if ($CreateResult.ExitCode -ne 0) {
                throw "schtasks.exe /Create failed with exit code $($CreateResult.ExitCode): $($CreateResult.Stderr)"
            }
        } finally {
            Remove-Item -LiteralPath $TempPath -Force -ErrorAction SilentlyContinue
        }
        # Round 8 Part B.3: enable the Task Scheduler operational log so that any
        # future task termination (e.g. a manually closed window under the old
        # InteractiveToken principal) is auditable after the fact. This needs an
        # elevated context; it is best-effort and never fails the install, but the
        # outcome is reported so the operator knows whether history is on.
        $HistoryEnabled = $false
        $HistoryDetail = ""
        try {
            $WevtutilPath = Join-Path $env:SystemRoot "System32\wevtutil.exe"
            $HistoryStderr = (& $WevtutilPath "set-log" "Microsoft-Windows-TaskScheduler/Operational" "/enabled:true" 2>&1) | Out-String
            $HistoryEnabled = ($LASTEXITCODE -eq 0)
            $HistoryDetail = if ($HistoryEnabled) {
                "Task Scheduler operational history enabled."
            } else {
                "Could not enable task history (needs elevation): $HistoryStderr"
            }
        } catch {
            $HistoryDetail = "Could not enable task history: $($_.Exception.Message)"
        }
        Write-Verbose $HistoryDetail
    }
    $InstalledXml = Get-TaskXml
    $Installed = -not [string]::IsNullOrWhiteSpace($InstalledXml)
    $InstalledAction = Get-TaskActionContract -TaskXml $InstalledXml
    $InstalledManaged = $Installed -and $null -ne $InstalledAction -and $InstalledAction.OwnershipMarkerMatches
    # Post-install verification covers the Exec action plus the principal logon
    # type, Queue overlap policy, PT3H limit, StartWhenAvailable and the
    # requirement that no registered weekly StartBoundary is already in the past.
    $InstalledReady = (
        $Installed -and $InstalledManaged -and $null -ne $InstalledAction -and $InstalledAction.Matches -and
        $InstalledAction.StartBoundariesInFuture
    )
    Write-Result (New-PlanPayload -RequestedAction $Action -Exists $Installed -Managed $InstalledManaged -ObservedAction $InstalledAction)
    exit $(if ($InstalledReady) { 0 } else { 4 })
}

if ($Action -eq "Remove") {
    if ($Exists -and (-not $Managed)) {
        Write-Error "The same-name task is not managed by this installer; refusing to remove it."
        exit 3
    }
    if ($Exists -and $PSCmdlet.ShouldProcess($TaskName, "Remove managed daily data task")) {
        $DeleteResult = Invoke-Schtasks -Values @("/Delete", "/TN", $TaskName, "/F")
        if ($DeleteResult.ExitCode -ne 0) {
            throw "schtasks.exe /Delete failed with exit code $($DeleteResult.ExitCode): $($DeleteResult.Stderr)"
        }
    }
    Write-Result (New-PlanPayload -RequestedAction $Action -Exists $false -Managed $false)
    exit 0
}
