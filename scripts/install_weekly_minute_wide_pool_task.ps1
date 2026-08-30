[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [ValidateSet("Plan", "Status", "Install", "Disable", "Enable", "Remove")]
    [string]$Action = "Plan",
    [string]$TaskName = "QuantDesk Weekly Minute Wide Pool",
    [string]$StatusFixturePath = "",
    [ValidateSet("S4U")]
    [string]$LogonMode = "S4U",
    [switch]$EnableNetwork,
    [switch]$Yes,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
if ($Yes) {
    $ConfirmPreference = "None"
}
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Runner = Join-Path $Root "scripts\run_weekly_minute_wide_pool.cmd"
$Marker = "QuantDesk managed weekly-minute-wide-pool task; schema=1"
$Execute = if ($env:ComSpec) { $env:ComSpec } else { "C:\Windows\System32\cmd.exe" }
$Arguments = '/d /s /c ""{0}" --enable-network --apply --lock-timeout-seconds 21600"' -f $Runner
$CurrentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$StatusFixture = $null
$UseAfterFixtureXml = $false

if (-not [string]::IsNullOrWhiteSpace($StatusFixturePath)) {
    if ($Action -notin @("Status", "Disable", "Enable")) {
        throw "StatusFixturePath is valid only with -Action Status, Disable, or Enable."
    }
    $ResolvedFixture = (Resolve-Path -LiteralPath $StatusFixturePath).Path
    $StatusFixture = Get-Content -Raw -Encoding UTF8 -LiteralPath $ResolvedFixture | ConvertFrom-Json
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

function ConvertTo-WindowsPathKey {
    param([string]$Value)
    $Text = ([string]$Value).Trim().Trim('"').Replace('/', '\')
    if ([string]::IsNullOrWhiteSpace($Text) -or -not [System.IO.Path]::IsPathRooted($Text)) {
        return ""
    }
    try {
        $Text = [System.IO.Path]::GetFullPath($Text)
    } catch {
        return ""
    }
    return $Text.TrimEnd('\').ToLowerInvariant()
}

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

function Get-TaskComInfo {
    $Service = $null
    $Folder = $null
    $Task = $null
    try {
        $Service = New-Object -ComObject "Schedule.Service"
        $Service.Connect()
        $TrimmedName = $TaskName.Trim('\')
        $LastSlash = $TrimmedName.LastIndexOf('\')
        $FolderPath = if ($LastSlash -ge 0) {
            '\' + $TrimmedName.Substring(0, $LastSlash)
        } else {
            '\'
        }
        $LeafName = if ($LastSlash -ge 0) {
            $TrimmedName.Substring($LastSlash + 1)
        } else {
            $TrimmedName
        }
        $Folder = $Service.GetFolder($FolderPath)
        $Task = $Folder.GetTask($LeafName)
        return [pscustomobject]@{
            Status = "found"
            Error = ""
            EnabledKnown = $true
            Enabled = [bool]$Task.Enabled
        }
    } catch {
        $HResultHex = "0x{0:X8}" -f ($_.Exception.HResult -band 0xffffffff)
        if ($HResultHex -in @("0x80070002", "0x8004130F")) {
            return [pscustomobject]@{
                Status = "not_found"
                Error = $HResultHex
                EnabledKnown = $false
                Enabled = $null
            }
        }
        return [pscustomobject]@{
            Status = "query_error"
            Error = "task_presence_query_failed:$HResultHex"
            EnabledKnown = $false
            Enabled = $null
        }
    } finally {
        foreach ($Item in @($Task, $Folder, $Service)) {
            if ($null -ne $Item -and [System.Runtime.InteropServices.Marshal]::IsComObject($Item)) {
                [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($Item)
            }
        }
    }
}

function Get-TaskQueryResult {
    if ($null -ne $StatusFixture) {
        $FixtureXml = [string]$StatusFixture.task_xml
        if (
            $UseAfterFixtureXml -and
            $null -ne $StatusFixture.PSObject.Properties["task_xml_after"]
        ) {
            $FixtureXml = [string]$StatusFixture.task_xml_after
        }
        $FixtureStatus = if ($null -ne $StatusFixture.PSObject.Properties["query_status"]) {
            [string]$StatusFixture.query_status
        } elseif ([string]::IsNullOrWhiteSpace($FixtureXml)) {
            "not_found"
        } else {
            "found"
        }
        return [pscustomobject]@{
            Status = $FixtureStatus
            Xml = $FixtureXml
            ExitCode = if ($null -ne $StatusFixture.PSObject.Properties["query_exit_code"]) {
                [int]$StatusFixture.query_exit_code
            } else { 0 }
            Error = if ($null -ne $StatusFixture.PSObject.Properties["query_error"]) {
                [string]$StatusFixture.query_error
            } else { "" }
            EnabledKnown = if ($UseAfterFixtureXml) {
                $null -ne $StatusFixture.PSObject.Properties["task_enabled_after"]
            } else {
                $null -ne $StatusFixture.PSObject.Properties["task_enabled"]
            }
            Enabled = if (
                $UseAfterFixtureXml -and
                $null -ne $StatusFixture.PSObject.Properties["task_enabled_after"]
            ) {
                [bool]$StatusFixture.task_enabled_after
            } elseif ($null -ne $StatusFixture.PSObject.Properties["task_enabled"]) {
                [bool]$StatusFixture.task_enabled
            } else { $null }
        }
    }
    $Result = Invoke-Schtasks -Values @("/Query", "/TN", $TaskName, "/XML", "ONE")
    if ($Result.ExitCode -eq 0) {
        $ComInfo = Get-TaskComInfo
        return [pscustomobject]@{
            Status = "found"
            Xml = $Result.Stdout
            ExitCode = 0
            Error = ""
            EnabledKnown = [bool]$ComInfo.EnabledKnown
            Enabled = $ComInfo.Enabled
        }
    }
    $Presence = Get-TaskComInfo
    return [pscustomobject]@{
        Status = if ($Presence.Status -eq "not_found") { "not_found" } else { "query_error" }
        Xml = ""
        ExitCode = [int]$Result.ExitCode
        Error = if ($Presence.Status -eq "not_found") {
            [string]$Presence.Error
        } else {
            "$($Presence.Error);schtasks_exit=$($Result.ExitCode)"
        }
        EnabledKnown = [bool]$Presence.EnabledKnown
        Enabled = $Presence.Enabled
    }
}

function Get-TaskContract {
    param(
        [string]$TaskXml,
        [object]$TaskEnabledOverride = $null
    )
    if ([string]::IsNullOrWhiteSpace($TaskXml)) {
        return $null
    }
    try {
        [xml]$Document = $TaskXml
        $Ns = New-Object System.Xml.XmlNamespaceManager($Document.NameTable)
        $Ns.AddNamespace("task", "http://schemas.microsoft.com/windows/2004/02/mit/task")
        $ExecNodes = @($Document.SelectNodes("/task:Task/task:Actions/task:Exec", $Ns))
        $ActionNodes = @($Document.SelectNodes("/task:Task/task:Actions/*", $Ns))
        $DescriptionNodes = @($Document.SelectNodes("/task:Task/task:RegistrationInfo/task:Description", $Ns))
        $TriggerNodes = @($Document.SelectNodes("/task:Task/task:Triggers/task:CalendarTrigger", $Ns))
        $AllTriggerNodes = @($Document.SelectNodes("/task:Task/task:Triggers/*", $Ns))
        $PrincipalNodes = @($Document.SelectNodes("/task:Task/task:Principals/task:Principal", $Ns))
        $SettingsNodes = @($Document.SelectNodes("/task:Task/task:Settings", $Ns))
        $Exec = if ($ExecNodes.Count -ge 1) { $ExecNodes[0] } else { $null }
        $Trigger = if ($TriggerNodes.Count -ge 1) { $TriggerNodes[0] } else { $null }
        $Principal = if ($PrincipalNodes.Count -ge 1) { $PrincipalNodes[0] } else { $null }
        $Settings = if ($SettingsNodes.Count -ge 1) { $SettingsNodes[0] } else { $null }
        $ObservedExecute = if ($null -ne $Exec) {
            $Node = $Exec.SelectSingleNode("task:Command", $Ns)
            if ($null -ne $Node) { [string]$Node.InnerText } else { "" }
        } else { "" }
        $ObservedArguments = if ($null -ne $Exec) {
            $Node = $Exec.SelectSingleNode("task:Arguments", $Ns)
            if ($null -ne $Node) { [string]$Node.InnerText } else { "" }
        } else { "" }
        $ObservedRoot = if ($null -ne $Exec) {
            $Node = $Exec.SelectSingleNode("task:WorkingDirectory", $Ns)
            if ($null -ne $Node) { [string]$Node.InnerText } else { "" }
        } else { "" }
        $ObservedDescription = if ($DescriptionNodes.Count -eq 1) {
            [string]$DescriptionNodes[0].InnerText
        } else { "" }
        $StartNode = if ($null -ne $Trigger) {
            $Trigger.SelectSingleNode("task:StartBoundary", $Ns)
        } else { $null }
        $ObservedStart = if ($null -ne $StartNode) { [string]$StartNode.InnerText } else { "" }
        $DayNodes = if ($null -ne $Trigger) {
            @($Trigger.SelectNodes("task:ScheduleByWeek/task:DaysOfWeek/*", $Ns))
        } else { @() }
        $ObservedDays = @($DayNodes | ForEach-Object { $_.LocalName })
        $WeeksNode = if ($null -ne $Trigger) {
            $Trigger.SelectSingleNode("task:ScheduleByWeek/task:WeeksInterval", $Ns)
        } else { $null }
        $TriggerEnabledNode = if ($null -ne $Trigger) {
            $Trigger.SelectSingleNode("task:Enabled", $Ns)
        } else { $null }
        $ObservedWeeks = if ($null -ne $WeeksNode) { [string]$WeeksNode.InnerText } else { "" }
        $ObservedTriggerEnabled = if ($null -eq $TriggerEnabledNode) {
            $true
        } else {
            [string]$TriggerEnabledNode.InnerText -eq "true"
        }
        $HasForbiddenTriggerShape = $null -ne $Trigger -and (
            $null -ne $Trigger.SelectSingleNode("task:Repetition", $Ns) -or
            $null -ne $Trigger.SelectSingleNode("task:EndBoundary", $Ns) -or
            $null -ne $Trigger.SelectSingleNode("task:RandomDelay", $Ns)
        )
        $LogonNode = if ($null -ne $Principal) {
            $Principal.SelectSingleNode("task:LogonType", $Ns)
        } else { $null }
        $RunLevelNode = if ($null -ne $Principal) {
            $Principal.SelectSingleNode("task:RunLevel", $Ns)
        } else { $null }
        $UserIdNode = if ($null -ne $Principal) {
            $Principal.SelectSingleNode("task:UserId", $Ns)
        } else { $null }
        $ObservedLogon = if ($null -ne $LogonNode) { [string]$LogonNode.InnerText } else { "" }
        $ObservedRunLevel = if ($null -ne $RunLevelNode) { [string]$RunLevelNode.InnerText } else { "LeastPrivilege" }
        $ObservedUserId = if ($null -ne $UserIdNode) { [string]$UserIdNode.InnerText } else { "" }
        $EnabledNode = if ($null -ne $Settings) { $Settings.SelectSingleNode("task:Enabled", $Ns) } else { $null }
        $MultipleNode = if ($null -ne $Settings) { $Settings.SelectSingleNode("task:MultipleInstancesPolicy", $Ns) } else { $null }
        $DisallowBatteryNode = if ($null -ne $Settings) { $Settings.SelectSingleNode("task:DisallowStartIfOnBatteries", $Ns) } else { $null }
        $StopBatteryNode = if ($null -ne $Settings) { $Settings.SelectSingleNode("task:StopIfGoingOnBatteries", $Ns) } else { $null }
        $StartAvailableNode = if ($null -ne $Settings) { $Settings.SelectSingleNode("task:StartWhenAvailable", $Ns) } else { $null }
        $NetworkNode = if ($null -ne $Settings) { $Settings.SelectSingleNode("task:RunOnlyIfNetworkAvailable", $Ns) } else { $null }
        $TerminateNode = if ($null -ne $Settings) { $Settings.SelectSingleNode("task:AllowHardTerminate", $Ns) } else { $null }
        $LimitNode = if ($null -ne $Settings) { $Settings.SelectSingleNode("task:ExecutionTimeLimit", $Ns) } else { $null }
        $PriorityNode = if ($null -ne $Settings) { $Settings.SelectSingleNode("task:Priority", $Ns) } else { $null }
        $EnabledText = if ($null -ne $EnabledNode) { [string]$EnabledNode.InnerText } else { "" }
        $ObservedEnabled = if ($null -ne $TaskEnabledOverride) {
            [bool]$TaskEnabledOverride
        } elseif ($EnabledText -eq "false") {
            $false
        } else {
            $true
        }
        $ObservedMultiple = if ($null -ne $MultipleNode) { [string]$MultipleNode.InnerText } else { "" }
        $ObservedDisallowBattery = if ($null -ne $DisallowBatteryNode) { [string]$DisallowBatteryNode.InnerText } else { "" }
        $ObservedStopBattery = if ($null -ne $StopBatteryNode) { [string]$StopBatteryNode.InnerText } else { "" }
        $ObservedStartAvailable = if ($null -ne $StartAvailableNode) { [string]$StartAvailableNode.InnerText } else { "" }
        $ObservedNetworkOnly = if ($null -ne $NetworkNode) { [string]$NetworkNode.InnerText } else { "false" }
        $ObservedTerminate = if ($null -ne $TerminateNode) { [string]$TerminateNode.InnerText } else { "true" }
        $ObservedLimit = if ($null -ne $LimitNode) { [string]$LimitNode.InnerText } else { "" }
        $ObservedPriority = if ($null -ne $PriorityNode) { [string]$PriorityNode.InnerText } else { "7" }
        $ExecuteMatches = (
            $ActionNodes.Count -eq 1 -and
            $ExecNodes.Count -eq 1 -and
            (ConvertTo-WindowsPathKey $ObservedExecute) -eq
            (ConvertTo-WindowsPathKey $Execute)
        )
        $ArgumentsMatch = [string]::Equals(
            $ObservedArguments.Trim(),
            $Arguments.Trim(),
            [System.StringComparison]::OrdinalIgnoreCase
        )
        $RootMatches = (
            (ConvertTo-WindowsPathKey $ObservedRoot) -eq
            (ConvertTo-WindowsPathKey $Root)
        )
        $OwnerMatches = [string]::Equals(
            $ObservedDescription,
            $Marker,
            [System.StringComparison]::Ordinal
        )
        $TriggerMatches = (
            $AllTriggerNodes.Count -eq 1 -and
            $TriggerNodes.Count -eq 1 -and
            $ObservedDays.Count -eq 1 -and
            $ObservedDays[0] -eq "Saturday" -and
            $ObservedStart -match 'T09:30:00$' -and
            $ObservedWeeks -eq "1" -and
            $ObservedTriggerEnabled -eq $true -and
            -not $HasForbiddenTriggerShape
        )
        $LogonMatches = (
            $PrincipalNodes.Count -eq 1 -and
            [string]::Equals($ObservedUserId, $CurrentSid, [System.StringComparison]::OrdinalIgnoreCase) -and
            $ObservedLogon -eq "S4U" -and
            $ObservedRunLevel -eq "LeastPrivilege"
        )
        $SettingsMatches = (
            $SettingsNodes.Count -eq 1 -and
            $null -ne $ObservedEnabled -and
            $ObservedMultiple -eq "Queue" -and
            $ObservedDisallowBattery -eq "false" -and
            $ObservedStopBattery -eq "false" -and
            $ObservedStartAvailable -eq "true" -and
            $ObservedNetworkOnly -eq "false" -and
            $ObservedTerminate -eq "true" -and
            $ObservedLimit -eq "PT6H" -and
            $ObservedPriority -eq "7"
        )
        return [pscustomobject]@{
            execute = $ObservedExecute
            arguments = $ObservedArguments
            working_directory = $ObservedRoot
            description = $ObservedDescription
            start_boundary = $ObservedStart
            days = $ObservedDays
            logon_type = $ObservedLogon
            run_level = $ObservedRunLevel
            user_id = $ObservedUserId
            enabled = $ObservedEnabled
            trigger_enabled = $ObservedTriggerEnabled
            weeks_interval = $ObservedWeeks
            multiple_instances = $ObservedMultiple
            start_when_available = $ObservedStartAvailable
            run_only_if_network_available = $ObservedNetworkOnly
            execution_time_limit = $ObservedLimit
            exec_count = $ExecNodes.Count
            action_count = $ActionNodes.Count
            calendar_trigger_count = $TriggerNodes.Count
            trigger_count = $AllTriggerNodes.Count
            OwnershipMarkerMatches = [bool]$OwnerMatches
            ActionMatches = [bool]($ExecuteMatches -and $ArgumentsMatch -and $RootMatches)
            TriggerMatches = [bool]$TriggerMatches
            LogonMatches = [bool]$LogonMatches
            SettingsMatches = [bool]$SettingsMatches
            error = ""
        }
    } catch {
        return [pscustomobject]@{
            execute = ""
            arguments = ""
            working_directory = ""
            description = ""
            start_boundary = ""
            days = @()
            logon_type = ""
            run_level = ""
            user_id = ""
            enabled = $null
            trigger_enabled = $null
            weeks_interval = ""
            multiple_instances = ""
            start_when_available = ""
            run_only_if_network_available = ""
            execution_time_limit = ""
            exec_count = 0
            action_count = 0
            calendar_trigger_count = 0
            trigger_count = 0
            OwnershipMarkerMatches = $false
            ActionMatches = $false
            TriggerMatches = $false
            LogonMatches = $false
            SettingsMatches = $false
            error = "task_contract_parse_failed:$($_.Exception.Message)"
        }
    }
}

function Test-TaskContractReady {
    param([object]$Observed)
    return (
        $null -ne $Observed -and
        [bool]$Observed.OwnershipMarkerMatches -and
        [bool]$Observed.ActionMatches -and
        [bool]$Observed.TriggerMatches -and
        [bool]$Observed.LogonMatches -and
        [bool]$Observed.SettingsMatches
    )
}

function New-Result {
    param(
        [string]$RequestedAction,
        [bool]$Exists = $false,
        [string]$State = "",
        [object]$Observed = $null,
        [string]$RequestedTaskState = ""
    )
    $HasObserved = $null -ne $Observed
    $Managed = $HasObserved -and [bool]$Observed.OwnershipMarkerMatches
    $ActionMatches = if ($HasObserved) { [bool]$Observed.ActionMatches } else { $null }
    $TriggerMatches = if ($HasObserved) { [bool]$Observed.TriggerMatches } else { $null }
    $LogonMatches = if ($HasObserved) { [bool]$Observed.LogonMatches } else { $null }
    $SettingsMatches = if ($HasObserved) { [bool]$Observed.SettingsMatches } else { $null }
    $ObservedEnabled = if ($HasObserved) { $Observed.enabled } else { $null }
    $ContractReady = [bool](Test-TaskContractReady $Observed)
    $InstalledReady = ($Exists -and $ContractReady -and $ObservedEnabled -eq $true)
    return [ordered]@{
        schema_version = 1
        action = $RequestedAction.ToLowerInvariant()
        task_name = $TaskName
        installed = [bool]$Exists
        exists = [bool]$Exists
        managed = [bool]$Managed
        contract_ready = [bool]$ContractReady
        installed_ready = [bool]$InstalledReady
        state = $State
        requested_task_state = if ([string]::IsNullOrWhiteSpace($RequestedTaskState)) {
            $null
        } else {
            $RequestedTaskState.ToLowerInvariant()
        }
        status_source = if ($RequestedAction -eq "Status") {
            if ($null -ne $StatusFixture) { "fixture" } else { "system" }
        } else { $null }
        network_authorized = [bool]$EnableNetwork
        logon_mode = $LogonMode
        expected_action = [ordered]@{
            execute = $Execute
            arguments = $Arguments
            working_directory = $Root
        }
        observed_action = if ($HasObserved) {
            [ordered]@{
                execute = [string]$Observed.execute
                arguments = [string]$Observed.arguments
                working_directory = [string]$Observed.working_directory
                description = [string]$Observed.description
                start_boundary = [string]$Observed.start_boundary
                days = @($Observed.days)
                logon_type = [string]$Observed.logon_type
                error = [string]$Observed.error
            }
        } else { $null }
        action_contract_matches = $ActionMatches
        trigger_contract_matches = $TriggerMatches
        logon_contract_matches = $LogonMatches
        settings_contract_matches = $SettingsMatches
        observed_enabled = $ObservedEnabled
        triggers = @(
            [ordered]@{ kind = "weekly"; days = @("Saturday"); at = "09:30" }
        )
        settings = [ordered]@{
            start_when_available = $true
            multiple_instances = "Queue"
            execution_time_limit = "PT6H"
            run_level = "LeastPrivilege"
        }
        ownership_marker = $Marker
        registration_authorized = $false
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
      <StartBoundary>$($DateText)T09:30:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek><Saturday/></DaysOfWeek>
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
    <Enabled>true</Enabled>
    <MultipleInstancesPolicy>Queue</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowHardTerminate>true</AllowHardTerminate>
    <ExecutionTimeLimit>PT6H</ExecutionTimeLimit>
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
    Write-Result (New-Result -RequestedAction $Action)
    exit 0
}

function Write-TaskQueryFailure {
    param([object]$Query)
    Write-Result ([ordered]@{
        schema_version = 1
        ok = $false
        status = "task_query_failed"
        action = $Action.ToLowerInvariant()
        task_name = $TaskName
        exists = $null
        query_status = [string]$Query.Status
        query_exit_code = [int]$Query.ExitCode
        query_error = [string]$Query.Error
    })
}

$ExistingQuery = Get-TaskQueryResult
if ($ExistingQuery.Status -eq "query_error") {
    Write-TaskQueryFailure $ExistingQuery
    exit 5
}
$Exists = $ExistingQuery.Status -eq "found"
$ExistingXml = if ($Exists) { [string]$ExistingQuery.Xml } else { "" }
$ExistingEnabledOverride = if ($ExistingQuery.EnabledKnown) { $ExistingQuery.Enabled } else { $null }
$Observed = Get-TaskContract -TaskXml $ExistingXml -TaskEnabledOverride $ExistingEnabledOverride
$Managed = $Exists -and $null -ne $Observed -and $Observed.OwnershipMarkerMatches

if ($Action -eq "Status") {
    $StateText = if (-not $Exists) {
        "NotFound"
    } elseif ($Observed.enabled -eq $true) {
        "Enabled"
    } elseif ($Observed.enabled -eq $false) {
        "Disabled"
    } else {
        "Unknown"
    }
    Write-Result (New-Result -RequestedAction $Action -Exists $Exists -State $StateText -Observed $Observed)
    exit 0
}

if ($Action -in @("Disable", "Enable")) {
    if (-not $Yes) {
        Write-Error "$Action requires -Yes for non-interactive confirmation." -ErrorAction Continue
        exit 2
    }
    if ($Action -eq "Enable" -and (-not $EnableNetwork)) {
        Write-Error "Enable requires -EnableNetwork because the task performs an explicit network data update." -ErrorAction Continue
        exit 2
    }
    if (-not $Exists) {
        Write-Error "The managed task does not exist; refusing to change its state." -ErrorAction Continue
        exit 3
    }
    if (-not $Managed) {
        Write-Error "A same-name unmanaged task exists; refusing to change its state." -ErrorAction Continue
        exit 3
    }
    if ($Action -eq "Enable" -and (-not (Test-TaskContractReady $Observed))) {
        Write-Error "Managed task contract drift must be repaired before Enable." -ErrorAction Continue
        exit 4
    }
    $StateSwitch = if ($Action -eq "Disable") { "/Disable" } else { "/Enable" }
    if ($null -eq $StatusFixture -and $PSCmdlet.ShouldProcess($TaskName, "$Action managed weekly minute task")) {
        $Change = Invoke-Schtasks -Values @("/Change", "/TN", $TaskName, $StateSwitch)
        if ($Change.ExitCode -ne 0) {
            throw "schtasks.exe /Change failed with exit code $($Change.ExitCode): $($Change.Stderr)"
        }
    }
    $UseAfterFixtureXml = $true
    $ChangedQuery = Get-TaskQueryResult
    if ($ChangedQuery.Status -eq "query_error") {
        Write-TaskQueryFailure $ChangedQuery
        exit 5
    }
    $ChangedExists = $ChangedQuery.Status -eq "found"
    $ChangedXml = if ($ChangedExists) { [string]$ChangedQuery.Xml } else { "" }
    $ChangedEnabledOverride = if ($ChangedQuery.EnabledKnown) { $ChangedQuery.Enabled } else { $null }
    $ChangedContract = Get-TaskContract -TaskXml $ChangedXml -TaskEnabledOverride $ChangedEnabledOverride
    $ChangedState = if (-not $ChangedExists) {
        "NotFound"
    } elseif ($ChangedContract.enabled -eq $true) {
        "Enabled"
    } elseif ($ChangedContract.enabled -eq $false) {
        "Disabled"
    } else {
        "Unknown"
    }
    $Result = New-Result `
        -RequestedAction $Action `
        -Exists $ChangedExists `
        -State $ChangedState `
        -Observed $ChangedContract `
        -RequestedTaskState $Action
    Write-Result $Result
    $StateChanged = if ($Action -eq "Disable") {
        $ChangedExists -and $ChangedContract.OwnershipMarkerMatches -and $ChangedContract.enabled -eq $false
    } else {
        $ChangedExists -and (Test-TaskContractReady $ChangedContract) -and $ChangedContract.enabled -eq $true
    }
    exit $(if ($StateChanged) { 0 } else { 4 })
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
        Write-Error "A same-name unmanaged task exists; refusing to overwrite it."
        exit 3
    }
    if ($PSCmdlet.ShouldProcess($TaskName, "Install managed weekly minute task")) {
        $TempPath = Join-Path $env:TEMP ("quantdesk-weekly-minute-" + [guid]::NewGuid().ToString("N") + ".xml")
        try {
            $Utf16LeBom = New-Object System.Text.UnicodeEncoding($false, $true)
            [System.IO.File]::WriteAllText($TempPath, (New-TaskXmlText), $Utf16LeBom)
            $Create = Invoke-Schtasks -Values @("/Create", "/TN", $TaskName, "/XML", $TempPath, "/F")
            if ($Create.ExitCode -ne 0) {
                throw "schtasks.exe /Create failed with exit code $($Create.ExitCode): $($Create.Stderr)"
            }
        } finally {
            Remove-Item -LiteralPath $TempPath -Force -ErrorAction SilentlyContinue
        }
    }
    $InstalledQuery = Get-TaskQueryResult
    if ($InstalledQuery.Status -eq "query_error") {
        Write-TaskQueryFailure $InstalledQuery
        exit 5
    }
    $Installed = $InstalledQuery.Status -eq "found"
    $InstalledXml = if ($Installed) { [string]$InstalledQuery.Xml } else { "" }
    $InstalledEnabledOverride = if ($InstalledQuery.EnabledKnown) { $InstalledQuery.Enabled } else { $null }
    $InstalledContract = Get-TaskContract -TaskXml $InstalledXml -TaskEnabledOverride $InstalledEnabledOverride
    $InstalledState = if ($InstalledContract.enabled -eq $true) { "Enabled" } elseif ($InstalledContract.enabled -eq $false) { "Disabled" } else { "Unknown" }
    $Result = New-Result -RequestedAction $Action -Exists $Installed -State $InstalledState -Observed $InstalledContract
    Write-Result $Result
    exit $(if ($Result.installed_ready) { 0 } else { 4 })
}

if ($Action -eq "Remove") {
    if ($Exists -and (-not $Managed)) {
        Write-Error "The same-name task is not managed by this installer; refusing to remove it."
        exit 3
    }
    if ($Exists -and $PSCmdlet.ShouldProcess($TaskName, "Remove managed weekly minute task")) {
        $Delete = Invoke-Schtasks -Values @("/Delete", "/TN", $TaskName, "/F")
        if ($Delete.ExitCode -ne 0) {
            throw "schtasks.exe /Delete failed with exit code $($Delete.ExitCode): $($Delete.Stderr)"
        }
    }
    Write-Result (New-Result -RequestedAction $Action)
    exit 0
}
