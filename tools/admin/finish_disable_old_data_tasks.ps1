# Elevated finish: disable remaining old data writers and set Pipeline to S4U.
$ErrorActionPreference = "Stop"
$LogPath = "E:\codex\claude\deploy_finish.log"
New-Item -ItemType Directory -Force -Path (Split-Path $LogPath) | Out-Null
function Write-Log([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:ss"), $msg
    Add-Content -Path $LogPath -Value $line -Encoding utf8
    Write-Output $msg
}
try {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) { throw "need elevated PowerShell" }

    $Old = @(
        "QuantDesk Universe Baseline Refresh",
        "QuantDesk Daily Data Refresh",
        "QuantDesk Daily Data Chain",
        "QuantDesk Full A Daily Reconciliation"
    )
    foreach ($name in $Old) {
        $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if (-not $t) { Write-Log ("skip missing {0}" -f $name); continue }
        if ($t.State -eq "Running") {
            Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
        }
        Disable-ScheduledTask -TaskName $name | Out-Null
        $chk = Get-ScheduledTask -TaskName $name
        if ($chk.State -ne "Disabled") { throw "failed to disable $name state=$($chk.State)" }
        Write-Log ("disabled {0}" -f $name)
    }

    $TaskName = "QuantDesk Data Close Pipeline"
    $Root = "E:\codex\quant_new"
    $Runner = Join-Path $Root "scripts\run_data_close_pipeline.py"
    $Py = Join-Path $Root ".venv\Scripts\python.exe"
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $act = New-ScheduledTaskAction -Execute $Py -Argument ('-B "{0}" --enable-network --trigger scheduled --enable-alert-network --alert-push' -f $Runner) -WorkingDirectory $Root
    $trgMain = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '3:40PM'
    $trgRetry = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '6:30PM'
    $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 4) -MultipleInstances IgnoreNew
    $prn = New-ScheduledTaskPrincipal -UserId $me -LogonType S4U -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger @($trgMain, $trgRetry) -Principal $prn -Settings $set -Force | Out-Null
    Enable-ScheduledTask -TaskName $TaskName | Out-Null
    $pipe = Get-ScheduledTask -TaskName $TaskName
    Write-Log ("pipeline state={0} logon={1}" -f $pipe.State, $pipe.Principal.LogonType)

    Get-ScheduledTask | Where-Object { $_.TaskName -match "QuantDesk" } | ForEach-Object {
        Write-Log ("{0,-48} {1}" -f $_.TaskName, $_.State)
    }
    Write-Log "deploy_ok=true"
} catch {
    Write-Log ("deploy_failed: {0}" -f $_.Exception.Message)
    throw
}
