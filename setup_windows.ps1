param(
    [switch]$Dev,
    [switch]$SkipInstall,
    [switch]$CreateShortcuts
)

$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
$VenvDir = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

function Invoke-ProjectPython {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    & $VenvPython @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed: $($Arguments -join ' ')"
    }
}

Write-Host "QuantDesk Windows setup"
Write-Host "Project root: $ProjectRoot"
Write-Host "Virtual env Python: $VenvPython"

if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $PythonCommand) {
        throw "Python was not found. Install Python 3.10+ and rerun this script."
    }
    Write-Host "Creating virtual environment: $VenvDir"
    & $PythonCommand.Source -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create .venv. Check your Python installation."
    }
}

Invoke-ProjectPython -Arguments @("--version")
if (-not $SkipInstall) {
    try {
        Invoke-ProjectPython -Arguments @("-m", "pip", "install", "--upgrade", "pip")
        Invoke-ProjectPython -Arguments @("-m", "pip", "install", "-r", (Join-Path $ProjectRoot "requirements.txt"))
    }
    catch {
        Write-Host "Dependency installation failed. Check network access and package mirrors."
        throw
    }
}
else {
    Write-Host "SkipInstall enabled: dependency installation skipped."
}

if ($Dev -and -not $SkipInstall) {
    try {
        Invoke-ProjectPython -Arguments @("-m", "pip", "install", "-r", (Join-Path $ProjectRoot "requirements-dev.txt"))
    }
    catch {
        Write-Host "Development dependency installation failed."
        throw
    }
}

foreach ($dir in @("data", "logs", "reports", "results", "config")) {
    New-Item -ItemType Directory -Path (Join-Path $ProjectRoot $dir) -Force | Out-Null
}

$DefaultConfig = Join-Path $ProjectRoot "config\default.yaml"
$LocalConfig = Join-Path $ProjectRoot "config.local.yaml"
if ((Test-Path -LiteralPath $DefaultConfig -PathType Leaf) -and -not (Test-Path -LiteralPath $LocalConfig)) {
    Write-Host "Optional: copy config\default.yaml to config.local.yaml if you want local overrides."
}

Write-Host "Setup complete. Run start_ui.cmd or QuantDesk.cmd to open the local app."
Write-Host "You can double-click create_desktop_shortcuts.cmd to create desktop icons."

if ($CreateShortcuts) {
    $ShortcutScript = Join-Path $ProjectRoot "create_desktop_shortcuts.ps1"
    if (Test-Path -LiteralPath $ShortcutScript -PathType Leaf) {
        try {
            & $ShortcutScript
        }
        catch {
            Write-Warning "Desktop shortcut creation failed: $($_.Exception.Message)"
        }
    }
    else {
        Write-Warning "Desktop shortcut script not found: $ShortcutScript"
    }
}
