[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [ValidateSet("Plan", "Status", "Install", "Remove")]
    [string]$Action = "Plan",
    [string]$TaskName = "QuantDesk Weekly Daily Wide Pool",
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
$Runner = Join-Path $Root "scripts\run_weekly_wide_pool_maintenance.cmd"
$Marker = "QuantDesk managed weekly-daily-wide-pool task; schema=1"
$Execute = if ($env:ComSpec) { $env:ComSpec } else { "C:\Windows\System32\cmd.exe" }
$Arguments = '/d /s /c ""{0}" --daily-only --enable-network --apply --lock-timeout-seconds 21600"' -f $Runner
$CurrentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$StatusFixture = $null

if (-not [string]::IsNullOrWhiteSpace($StatusFixturePath)) {
    if ($Action -ne "Status") {
        throw "StatusFixturePath is valid only with -Action Status."
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

function Get-TaskContract {
    param([string]$TaskXml)
    if ([string]::IsNullOrWhiteSpace($TaskXml)) {
        return $null
    }
    try {
        [xml]$Document = $TaskXml
        $Ns = New-Object System.Xml.XmlNamespaceManager($Document.NameTable)
        $Ns.AddNamespace("task", "http://schemas.microsoft.com/windows/2004/02/mit/task")
        $Exec = $Document.SelectSingleNode("/task:Task/task:Actions/task:Exec", $Ns)
        $DescriptionNode = $Document.SelectSingleNode("/task:Task/task:RegistrationInfo/task:Description", $Ns)
        $Trigger = $Document.SelectSingleNode("/task:Task/task:Triggers/task:CalendarTrigger", $Ns)
        $LogonNode = $Document.SelectSingleNode("/task:Task/task:Principals/task:Principal/task:LogonType", $Ns)
        if ($null -eq $Exec -or $null -eq $Trigger) {
            throw "Task XML is missing Exec or CalendarTrigger."
        }
        $ObservedExecute = [string]$Exec.SelectSingleNode("task:Command", $Ns).InnerText
        $ObservedArguments = [string]$Exec.SelectSingleNode("task:Arguments", $Ns).InnerText
        $ObservedRoot = [string]$Exec.SelectSingleNode("task:WorkingDirectory", $Ns).InnerText
        $ObservedDescription = if ($null -ne $DescriptionNode) { [string]$DescriptionNode.InnerText } else { "" }
        $StartNode = $Trigger.SelectSingleNode("task:StartBoundary", $Ns)
        $ObservedStart = if ($null -ne $StartNode) { [string]$StartNode.InnerText } else { "" }
        $DayNodes = $Trigger.SelectNodes("task:ScheduleByWeek/task:DaysOfWeek/*", $Ns)
        $ObservedDays = @($DayNodes | ForEach-Object { $_.LocalName })
        $ObservedLogon = if ($null -ne $LogonNode) { [string]$LogonNode.InnerText } else { "" }
        $ExecuteMatches = (
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
            $ObservedDays.Count -eq 1 -and
            $ObservedDays[0] -eq "Saturday" -and
            $ObservedStart -match 'T09:30:00$'
        )
        $LogonMatches = $ObservedLogon -eq "S4U"
        return [pscustomobject]@{
            execute = $ObservedExecute
            arguments = $ObservedArguments
            working_directory = $ObservedRoot
            description = $ObservedDescription
            start_boundary = $ObservedStart
            days = $ObservedDays
            logon_type = $ObservedLogon
            OwnershipMarkerMatches = [bool]$OwnerMatches
            ActionMatches = [bool]($ExecuteMatches -and $ArgumentsMatch -and $RootMatches)
            TriggerMatches = [bool]$TriggerMatches
            LogonMatches = [bool]$LogonMatches
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
            OwnershipMarkerMatches = $false
            ActionMatches = $false
            TriggerMatches = $false
            LogonMatches = $false
            error = "task_contract_parse_failed:$($_.Exception.Message)"
        }
    }
}

function New-Result {
    param(
        [string]$RequestedAction,
        [bool]$Exists = $false,
        [string]$State = "",
        [object]$Observed = $null
    )
    $HasObserved = $null -ne $Observed
    $Managed = $HasObserved -and [bool]$Observed.OwnershipMarkerMatches
    $ActionMatches = if ($HasObserved) { [bool]$Observed.ActionMatches } else { $null }
    $TriggerMatches = if ($HasObserved) { [bool]$Observed.TriggerMatches } else { $null }
    $LogonMatches = if ($HasObserved) { [bool]$Observed.LogonMatches } else { $null }
    $InstalledReady = (
        $Exists -and $Managed -and $ActionMatches -and $TriggerMatches -and $LogonMatches
    )
    return [ordered]@{
        schema_version = 1
        action = $RequestedAction.ToLowerInvariant()
        task_name = $TaskName
        installed = [bool]$Exists
        exists = [bool]$Exists
        managed = [bool]$Managed
        installed_ready = [bool]$InstalledReady
        state = $State
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

$ExistingXml = Get-TaskXml
$Exists = -not [string]::IsNullOrWhiteSpace($ExistingXml)
$Observed = Get-TaskContract -TaskXml $ExistingXml
$Managed = $Exists -and $null -ne $Observed -and $Observed.OwnershipMarkerMatches

if ($Action -eq "Status") {
    $StateText = ""
    if ($Exists) {
        if ($null -ne $StatusFixture) {
            $StateText = [string]$StatusFixture.state
        } else {
            $Query = Invoke-Schtasks -Values @("/Query", "/TN", $TaskName, "/FO", "LIST", "/V")
            if ($Query.ExitCode -eq 0) {
                $StateText = $Query.Stdout
            }
        }
    }
    Write-Result (New-Result -RequestedAction $Action -Exists $Exists -State $StateText -Observed $Observed)
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
    $InstalledXml = Get-TaskXml
    $Installed = -not [string]::IsNullOrWhiteSpace($InstalledXml)
    $InstalledContract = Get-TaskContract -TaskXml $InstalledXml
    $Result = New-Result -RequestedAction $Action -Exists $Installed -Observed $InstalledContract
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
