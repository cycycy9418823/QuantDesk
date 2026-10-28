param(
    [ValidateSet('Plan', 'Install', 'Remove')][string]$Action = 'Plan',
    [Parameter(Mandatory = $true)][string]$ProjectRoot,
    [string]$LogRoot,
    [string]$PythonPath,
    [string]$StartupFolder,
    [ValidateRange(1, 65535)][int]$Port = 8000,
    [switch]$EnableSelectedQuotes
)
$ErrorActionPreference = 'Stop'
function Get-QuantViewerLinkHash([string]$Path) {
    $quantViewerHasher = [Security.Cryptography.SHA256]::Create()
    try { return [Convert]::ToBase64String($quantViewerHasher.ComputeHash([IO.File]::ReadAllBytes($Path))) }
    finally { $quantViewerHasher.Dispose() }
}
$quantViewerRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$quantViewerEntry = Join-Path $quantViewerRoot 'scripts\ensure_research_viewer.py'
$quantViewerHelperExists = Test-Path -LiteralPath $quantViewerEntry -PathType Leaf
if (-not $quantViewerHelperExists -and $Action -ne 'Plan') { throw 'Viewer start helper is missing' }
if (-not $LogRoot) { $LogRoot = Join-Path $env:LOCALAPPDATA 'QuantDesk\viewer-start' }
$quantViewerLogs = [IO.Path]::GetFullPath($LogRoot)
if ($quantViewerLogs.TrimEnd('\') -eq $quantViewerRoot.TrimEnd('\') -or
    $quantViewerLogs.StartsWith($quantViewerRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Logs must remain outside the project' }
if (-not $PythonPath) { $PythonPath = $env:QUANTDESK_PYTHON }
if (-not $PythonPath) { $PythonPath = Join-Path $quantViewerRoot '.venv\Scripts\python.exe' }
$quantViewerPython = (Resolve-Path -LiteralPath $PythonPath).Path
$quantViewerPythonw = Join-Path (Split-Path -Parent $quantViewerPython) 'pythonw.exe'
if (Test-Path -LiteralPath $quantViewerPythonw -PathType Leaf) { $quantViewerPython = $quantViewerPythonw }
if (-not $StartupFolder) { $StartupFolder = [Environment]::GetFolderPath('Startup') }
if (-not $StartupFolder) { throw 'Current-user Startup folder is unavailable' }
$quantViewerStartup = [IO.Path]::GetFullPath($StartupFolder)
$quantViewerShortcut = Join-Path $quantViewerStartup 'QuantDesk ReadOnly Viewer.lnk'
$quantViewerArgs = '-B -X utf8 "' + $quantViewerEntry + '" --project-root "' + $quantViewerRoot + '" --log-root "' + $quantViewerLogs + '" --port ' + $Port
if ($EnableSelectedQuotes) { $quantViewerArgs += ' --enable-selected-quotes' }
$quantViewerPlan = [ordered]@{action=$Action;shortcut=$quantViewerShortcut;target=$quantViewerPython;arguments=$quantViewerArgs;project_root=$quantViewerRoot;log_root=$quantViewerLogs;helper_exists=$quantViewerHelperExists;read_only=$true;selected_quote_refresh=[bool]$EnableSelectedQuotes;scheduled_tasks_changed=$false}
if ($Action -eq 'Plan') { $quantViewerPlan | ConvertTo-Json; exit 0 }
$quantViewerShell = New-Object -ComObject WScript.Shell
if (Test-Path -LiteralPath $quantViewerShortcut) {
    $quantViewerExisting = $quantViewerShell.CreateShortcut($quantViewerShortcut)
    if ($quantViewerExisting.TargetPath -ne $quantViewerPython -or $quantViewerExisting.Arguments -ne $quantViewerArgs -or $quantViewerExisting.WorkingDirectory -ne $quantViewerRoot) { throw 'An unrelated Startup shortcut exists; it was preserved' }
    if ($Action -eq 'Install') { $quantViewerPlan['status'] = 'already_installed'; $quantViewerPlan | ConvertTo-Json; exit 0 }
}
if ($Action -eq 'Remove') {
    if (Test-Path -LiteralPath $quantViewerShortcut) { Remove-Item -LiteralPath $quantViewerShortcut }
    $quantViewerPlan['status'] = 'removed'
} else {
    if (-not (Test-Path -LiteralPath $quantViewerStartup -PathType Container)) { throw 'Startup folder is missing' }
    $quantViewerTemp = Join-Path $quantViewerStartup ('quantdesk-viewer-' + [guid]::NewGuid().ToString('N') + '.lnk')
    try {
        $quantViewerLink = $quantViewerShell.CreateShortcut($quantViewerTemp)
        $quantViewerLink.TargetPath = $quantViewerPython
        $quantViewerLink.Arguments = $quantViewerArgs
        $quantViewerLink.WorkingDirectory = $quantViewerRoot
        $quantViewerLink.Description = 'QuantDesk local read-only viewer; no data or account updates'
        $quantViewerLink.WindowStyle = 7
        $quantViewerLink.Save()
        $quantViewerReadback = $quantViewerShell.CreateShortcut($quantViewerTemp)
        if ($quantViewerReadback.TargetPath -ne $quantViewerPython -or $quantViewerReadback.Arguments -ne $quantViewerArgs -or $quantViewerReadback.WorkingDirectory -ne $quantViewerRoot) {
            throw 'Startup shortcut readback failed'
        }
        $quantViewerPreparedHash = Get-QuantViewerLinkHash $quantViewerTemp
        # Same-volume rename refuses replacement, including an entry appearing
        # between preparation and publication. Do not overwrite a foreign link.
        [IO.File]::Move($quantViewerTemp, $quantViewerShortcut)
        if ((Get-QuantViewerLinkHash $quantViewerShortcut) -ne $quantViewerPreparedHash) {
            throw 'Published shortcut changed; preserve it for inspection'
        }
        $quantViewerPlan['status'] = 'installed_verified'
    } finally {
        if (Test-Path -LiteralPath $quantViewerTemp) { Remove-Item -LiteralPath $quantViewerTemp }
    }
}
$quantViewerPlan | ConvertTo-Json
