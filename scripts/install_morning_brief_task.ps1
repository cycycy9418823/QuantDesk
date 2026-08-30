[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [ValidateSet("Plan", "Status", "Install", "Disable", "Remove")]
    [string]$Action = "Plan",
    [string]$TaskName = "QuantDesk Morning Brief",
    [string]$StatusFixturePath = "",
    [switch]$EnableNetwork,
    [switch]$Yes,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
if ($Yes) { $ConfirmPreference = "None" }
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Runner = Join-Path $Root "scripts\run_morning_brief.cmd"
$Marker = "QuantDesk managed morning-brief task; schema=1"
$Execute = if ($env:ComSpec) { $env:ComSpec } else { "C:\Windows\System32\cmd.exe" }
$Arguments = '/d /s /c ""{0}" --enable-network --push"' -f $Runner
$CurrentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$StatusFixture = $null

if (-not [string]::IsNullOrWhiteSpace($StatusFixturePath)) {
    if ($Action -ne "Status") { throw "StatusFixturePath is valid only with -Action Status." }
    $ResolvedFixture = (Resolve-Path -LiteralPath $StatusFixturePath).Path
    $StatusFixture = Get-Content -Raw -Encoding UTF8 -LiteralPath $ResolvedFixture | ConvertFrom-Json
    if ($null -eq $StatusFixture.PSObject.Properties["task_xml"] -or $null -eq $StatusFixture.PSObject.Properties["state"]) {
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
    if ([string]::IsNullOrWhiteSpace($Text) -or -not [System.IO.Path]::IsPathRooted($Text)) { return "" }
    try { $Text = [System.IO.Path]::GetFullPath($Text) } catch { return "" }
    return $Text.TrimEnd('\').ToLowerInvariant()
}

function Get-UserEnvPresence {
    if ($null -ne $StatusFixture -and $null -ne $StatusFixture.PSObject.Properties["serverchan_user_env_present"]) {
        return [bool]$StatusFixture.serverchan_user_env_present
    }
    # Prefer the canonical name; accept the legacy name so an existing machine
    # keeps working (the python runner reads the same fallback). The value is
    # only tested for presence and is never echoed anywhere.
    $Canonical = [Environment]::GetEnvironmentVariable("QUANTDESK_SERVERCHAN_SENDKEY", "User")
    if (-not [string]::IsNullOrWhiteSpace($Canonical)) {
        return $true
    }
    $Legacy = [Environment]::GetEnvironmentVariable("SERVERCHAN_SEND_KEY", "User")
    return -not [string]::IsNullOrWhiteSpace($Legacy)
}

function Invoke-Schtasks {
    param([string[]]$Values)
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
    if ($null -ne $StatusFixture) { return [string]$StatusFixture.task_xml }
    $Result = Invoke-Schtasks -Values @("/Query", "/TN", $TaskName, "/XML", "ONE")
    if ($Result.ExitCode -ne 0) { return "" }
    return $Result.Stdout
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
        $OwnerMatches = [string]::Equals($ObservedDescription, $Marker, [System.StringComparison]::Ordinal)
        $ActionMatches = (
            (ConvertTo-WindowsPathKey $ObservedExecute) -eq (ConvertTo-WindowsPathKey $Execute) -and
            [string]::Equals($ObservedArguments.Trim(), $Arguments.Trim(), [System.StringComparison]::OrdinalIgnoreCase) -and
            (ConvertTo-WindowsPathKey $ObservedRoot) -eq (ConvertTo-WindowsPathKey $Root)
        )
        $TriggerMatches = $DaysMatch -and $ObservedStart -match 'T08:35:00$'
        $SettingsMatches = (
            $Logon -eq "S4U" -and $RunLevel -eq "LeastPrivilege" -and
            $Multiple -eq "IgnoreNew" -and $StartWhenAvailable -eq "true" -and $Limit -eq "PT15M"
        )
        return [pscustomobject]@{
            execute = $ObservedExecute; arguments = $ObservedArguments; working_directory = $ObservedRoot
            description = $ObservedDescription; start_boundary = $ObservedStart; days = $ObservedDays
            logon_type = $Logon; run_level = $RunLevel; multiple_instances = $Multiple
            start_when_available = $StartWhenAvailable; execution_time_limit = $Limit
            OwnershipMarkerMatches = [bool]$OwnerMatches; ActionMatches = [bool]$ActionMatches
            TriggerMatches = [bool]$TriggerMatches; SettingsMatches = [bool]$SettingsMatches; error = ""
        }
    } catch {
        return [pscustomobject]@{
            execute = ""; arguments = ""; working_directory = ""; description = ""; start_boundary = ""
            days = @(); logon_type = ""; run_level = ""; multiple_instances = ""
            start_when_available = ""; execution_time_limit = ""; OwnershipMarkerMatches = $false
            ActionMatches = $false; TriggerMatches = $false; SettingsMatches = $false
            error = "task_contract_parse_failed:$($_.Exception.GetType().Name)"
        }
    }
}

function Get-NextBoundary {
    $Candidate = (Get-Date).Date.AddHours(8).AddMinutes(35)
    if ($Candidate -le (Get-Date)) { $Candidate = $Candidate.AddDays(1) }
    while ($Candidate.DayOfWeek -eq "Saturday" -or $Candidate.DayOfWeek -eq "Sunday") { $Candidate = $Candidate.AddDays(1) }
    return $Candidate.ToString("yyyy-MM-ddTHH:mm:ss")
}

function ConvertTo-StateCode {
    # Never surface raw scheduler stdout: a same-name task registered by someone
    # else controls that text, and echoing it back would leak whatever it holds
    # (including a key pasted into a description). Map to a closed vocabulary and
    # drop anything unrecognised.
    param([string]$State)
    $Token = ([string]$State).Trim()
    if ([string]::IsNullOrWhiteSpace($Token)) { return "not_queried" }
    $First = ($Token -split '\s+')[0].ToLowerInvariant()
    switch ($First) {
        "ready"    { return "ready" }
        "running"  { return "running" }
        "disabled" { return "disabled" }
        "queued"   { return "queued" }
        default    { return "unknown" }
    }
}

function New-Result {
    param([string]$RequestedAction, [bool]$Exists = $false, [string]$State = "", [object]$Observed = $null, [object]$EnvPresent = $null)
    $HasObserved = $null -ne $Observed
    $Managed = $HasObserved -and [bool]$Observed.OwnershipMarkerMatches
    $ActionMatches = if ($HasObserved) { [bool]$Observed.ActionMatches } else { $null }
    $TriggerMatches = if ($HasObserved) { [bool]$Observed.TriggerMatches } else { $null }
    $SettingsMatches = if ($HasObserved) { [bool]$Observed.SettingsMatches } else { $null }
    $InstalledReady = $Exists -and $Managed -and $ActionMatches -and $TriggerMatches -and $SettingsMatches -and ($EnvPresent -eq $true)
    # Only comparison verdicts are published. The expected and observed action
    # details stay internal: they are what the contract booleans are computed
    # from, and reprinting them is how an untrusted task definition would get a
    # channel into our own output.
    return [ordered]@{
        schema_version = 1; action = $RequestedAction.ToLowerInvariant(); task_name = $TaskName
        installed = [bool]$Exists; exists = [bool]$Exists; managed = [bool]$Managed
        installed_ready = [bool]$InstalledReady
        state_code = ConvertTo-StateCode -State $State
        status_source = if ($RequestedAction -eq "Status") { if ($null -ne $StatusFixture) { "fixture" } else { "system" } } else { $null }
        network_authorized = [bool]$EnableNetwork
        serverchan_user_env_present = $EnvPresent
        contract_parse_failed = if ($HasObserved) { -not [string]::IsNullOrWhiteSpace([string]$Observed.error) } else { $null }
        action_contract_matches = $ActionMatches; trigger_contract_matches = $TriggerMatches
        settings_contract_matches = $SettingsMatches
        triggers = @([ordered]@{ kind = "weekly"; days = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"); at = "08:35" })
        settings = [ordered]@{ start_when_available = $true; multiple_instances = "IgnoreNew"; execution_time_limit = "PT15M"; run_level = "LeastPrivilege" }
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
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable><AllowHardTerminate>true</AllowHardTerminate><ExecutionTimeLimit>PT15M</ExecutionTimeLimit><Priority>7</Priority></Settings>
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
    $StateText = ""
    if ($Exists) {
        if ($null -ne $StatusFixture) { $StateText = [string]$StatusFixture.state }
        else {
            $Query = Invoke-Schtasks -Values @("/Query", "/TN", $TaskName, "/FO", "LIST", "/V")
            if ($Query.ExitCode -eq 0) { $StateText = $Query.Stdout.Trim() }
        }
    }
    Write-Result (New-Result -RequestedAction $Action -Exists $Exists -State $StateText -Observed $Observed -EnvPresent (Get-UserEnvPresence))
    exit 0
}

if ($Action -eq "Install") {
    if (-not $Yes) { Write-Error "Install requires -Yes for non-interactive confirmation."; exit 2 }
    if (-not $EnableNetwork) { Write-Error "Install requires -EnableNetwork for the external delivery task."; exit 2 }
    $EnvPresent = Get-UserEnvPresence
    if (-not $EnvPresent) { Write-Error "Install requires the user-scope ServerChan environment variable to be present."; exit 2 }
    if ($Exists -and (-not $Managed)) { Write-Error "A same-name unmanaged task exists; refusing to overwrite it."; exit 3 }
    if ($PSCmdlet.ShouldProcess($TaskName, "Install managed morning brief task")) {
        $TempPath = Join-Path $env:TEMP ("quantdesk-morning-brief-" + [guid]::NewGuid().ToString("N") + ".xml")
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
    $Result = New-Result -RequestedAction $Action -Exists $Installed -Observed $InstalledContract -EnvPresent $EnvPresent
    if ($Result.installed_ready) { $Result.registration_authorized = $true }
    Write-Result $Result
    exit $(if ($Result.installed_ready) { 0 } else { 4 })
}

if ($Action -eq "Disable") {
    if (-not $Yes) { Write-Error "Disable requires -Yes for non-interactive confirmation."; exit 2 }
    if ($Exists -and (-not $Managed)) { Write-Error "A same-name unmanaged task exists; refusing to disable it."; exit 3 }
    if ($Exists -and $PSCmdlet.ShouldProcess($TaskName, "Disable managed morning brief task")) {
        $Change = Invoke-Schtasks -Values @("/Change", "/TN", $TaskName, "/Disable")
        if ($Change.ExitCode -ne 0) { throw "schtasks.exe /Change failed with exit code $($Change.ExitCode)." }
    }
    Write-Result (New-Result -RequestedAction $Action -Exists $Exists -State "Disabled" -Observed $Observed -EnvPresent $null)
    exit 0
}

if ($Action -eq "Remove") {
    if (-not $Yes) { Write-Error "Remove requires -Yes for non-interactive confirmation."; exit 2 }
    if ($Exists -and (-not $Managed)) { Write-Error "A same-name unmanaged task exists; refusing to remove it."; exit 3 }
    if ($Exists -and $PSCmdlet.ShouldProcess($TaskName, "Remove managed morning brief task")) {
        $Delete = Invoke-Schtasks -Values @("/Delete", "/TN", $TaskName, "/F")
        if ($Delete.ExitCode -ne 0) { throw "schtasks.exe /Delete failed with exit code $($Delete.ExitCode)." }
    }
    Write-Result (New-Result -RequestedAction $Action)
    exit 0
}
