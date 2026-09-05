#Requires -Version 5.1
<#
.SYNOPSIS
    Registers a Scheduled Task named "dibs" that runs scripts\run.ps1 at logon
    (hidden window), then starts it.
.DESCRIPTION
    The task runs as the current interactive user at logon — NOT as a service.
    This is required: screenshots and mouse/keyboard input only work in the
    interactive desktop session (Session 1+). A service (LogonType
    ServiceAccount) runs in the non-interactive Session 0 and cannot see the
    screen or send input at all, so it is not an option here.

    Idempotent: unregisters any existing "dibs" task first, so re-running
    this script is safe after a config or path change.
#>
param(
    [string]$TaskName = "dibs"
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$runScript = Join-Path $root "scripts\run.ps1"

if (-not (Test-Path $runScript)) {
    throw "Can't find $runScript"
}

Write-Host "Installing Scheduled Task '$TaskName'..." -ForegroundColor Cyan

# Idempotent: drop any existing registration first.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  removing existing task..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runScript`"" `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

# Interactive logon + Limited run level: this task runs *in* the desktop
# session the user is logged into, not in a background/service session.
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "dibs computer-use hub — runs in the interactive session (required for screenshots/input)." `
    | Out-Null

Write-Host "  registered." -ForegroundColor Green

# A dibs server started by hand (e.g. `uv run dibs serve`) would keep the port; stop it so the task owns it.
$busy = Get-NetTCPConnection -State Listen -LocalPort 7474 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($procId in $busy) {
    $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $procId" -ErrorAction SilentlyContinue).CommandLine
    if ($cmd -and $cmd -like "*dibs*serve*") {
        Write-Host "  stopping hand-started dibs server (pid $procId) so the task can take the port..."
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
}
Write-Host "Starting task..." -ForegroundColor Cyan
Start-ScheduledTask -TaskName $TaskName

Start-Sleep -Seconds 2
$state = (Get-ScheduledTask -TaskName $TaskName).State
Write-Host "  task state: $state"

# Best-effort read of host/port from config.yaml so the printed URL is right.
$host_ = "127.0.0.1"
$port = 7474
$configPath = Join-Path $root "config.yaml"
if (Test-Path $configPath) {
    $cfg = Get-Content $configPath -Raw
    if ($cfg -match "(?m)^\s*host:\s*(\S+)") { $host_ = $Matches[1].Trim('"''') }
    if ($cfg -match "(?m)^\s*port:\s*(\d+)") { $port = $Matches[1] }
}

Write-Host ""
Write-Host "dibs dashboard: http://${host_}:${port}/" -ForegroundColor Green
Write-Host ""
Write-Host "Note: this task runs in your interactive logon session (Session 1+), not as a" -ForegroundColor DarkGray
Write-Host "background service — that's required for screenshots and mouse/keyboard input" -ForegroundColor DarkGray
Write-Host "to work at all. It starts at logon and stays hidden. Uninstall with" -ForegroundColor DarkGray
Write-Host "scripts\uninstall-task.ps1." -ForegroundColor DarkGray
