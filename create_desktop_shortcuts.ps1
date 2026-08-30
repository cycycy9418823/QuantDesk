$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Desktop = [Environment]::GetFolderPath("Desktop")
$Shell = New-Object -ComObject WScript.Shell

function Resolve-ShortcutIcon {
    $iconCandidates = @(
        (Join-Path $ProjectRoot "app\icon.ico"),
        (Join-Path $ProjectRoot "assets\icon.ico"),
        (Join-Path $ProjectRoot "icon.ico")
    )
    foreach ($icon in $iconCandidates) {
        if (Test-Path -LiteralPath $icon -PathType Leaf) {
            return $icon
        }
    }
    return $null
}

function New-UnicodeText {
    param([Parameter(Mandatory = $true)][int[]]$CodePoints)
    return -join ($CodePoints | ForEach-Object { [char]$_ })
}

function New-QuantDeskShortcut {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$TargetScript,
        [Parameter(Mandatory = $true)][string]$Description,
        [string]$IconPath
    )

    $targetPath = Join-Path $ProjectRoot $TargetScript
    if (-not (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
        throw "Target script not found: $targetPath"
    }

    $shortcutPath = Join-Path $Desktop "$Name.lnk"
    try {
        $shortcut = $Shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $targetPath
        $shortcut.WorkingDirectory = $ProjectRoot
        $shortcut.Description = $Description
        if ($IconPath) {
            $shortcut.IconLocation = $IconPath
        }
        $shortcut.Save()
    }
    catch {
        throw "Failed to create shortcut '$shortcutPath': $($_.Exception.Message)"
    }

    Write-Host "Created: $shortcutPath"
    Write-Host "Target script: $targetPath"
    Write-Host "WorkingDirectory: $ProjectRoot"
}

Write-Host "QuantDesk desktop shortcut setup"
Write-Host "Project root: $ProjectRoot"
Write-Host "Desktop: $Desktop"

$iconPath = Resolve-ShortcutIcon
if ($iconPath) {
    Write-Host "Icon: $iconPath"
}
else {
    Write-Host "Icon: default cmd.exe icon"
}

$localDescription = "QuantDesk " + (New-UnicodeText -CodePoints @(0x672C, 0x5730, 0x91CF, 0x5316, 0x8F85, 0x52A9, 0x51B3, 0x7B56, 0x5DE5, 0x5177))

New-QuantDeskShortcut -Name "QuantDesk" -TargetScript "start_ui.cmd" -Description $localDescription -IconPath $iconPath

Write-Host "Desktop shortcuts are ready."
Write-Host "Retired sample launchers are intentionally excluded from desktop shortcuts."
Write-Host "You can now double-click the desktop icon to start QuantDesk."
