#Requires -Version 5.1
<#
.SYNOPSIS
    Registers a Scheduled Task named "dibs-watchdog" that runs scripts\watchdog.ps1 every
    5 minutes (hidden), restarting the "dibs" task whenever /healthz stops answering.
.DESCRIPTION
    Idempotent: unregisters any existing "dibs-watchdog" task first. Runs as the current
    user, Limited run level, interactive logon (it has to be able to Start-ScheduledTask
    the desktop-session "dibs" task). Remove with uninstall-task.ps1 (removes both tasks).
#>
param(
    [string]$TaskName = "dibs-watchdog",
    [int]$EveryMinutes = 5
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$script = Join-Path $root "scripts\watchdog.ps1"
if (-not (Test-Path $script)) { throw "Can't find $script" }

Write-Host "Installing Scheduled Task '$TaskName' (every $EveryMinutes min)..." -ForegroundColor Cyan

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  removing existing task..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory $root

# Start 2 minutes from now and repeat indefinitely; also re-arm at every logon.
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
$logon = New-ScheduledTaskTrigger -AtLogOn
$logon.Repetition = $repeat.Repetition

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger @($repeat, $logon) `
    -Settings $settings `
    -Principal $principal `
    -Description "dibs watchdog - restarts the dibs task when http://127.0.0.1:7474/healthz stops answering." `
    | Out-Null

Write-Host "  registered." -ForegroundColor Green
Write-Host "Running one check now..." -ForegroundColor Cyan
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script
Write-Host "Done. Log: $root\data\watchdog.log (written only when something was wrong)." -ForegroundColor Green
