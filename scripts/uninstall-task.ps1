#Requires -Version 5.1
<#
.SYNOPSIS
    Stops and removes the "dibs" Scheduled Task installed by install-task.ps1, plus the
    "dibs-watchdog" task from install-watchdog.ps1 if present.
#>
param(
    [string]$TaskName = "dibs"
)

$ErrorActionPreference = "Stop"

$wd = Get-ScheduledTask -TaskName "$TaskName-watchdog" -ErrorAction SilentlyContinue
if ($wd) {
    Write-Host "Removing '$TaskName-watchdog'..." -ForegroundColor Cyan
    Unregister-ScheduledTask -TaskName "$TaskName-watchdog" -Confirm:$false
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "No '$TaskName' task installed — nothing to do." -ForegroundColor Yellow
    exit 0
}

if ($existing.State -eq "Running") {
    Write-Host "Stopping '$TaskName'..." -ForegroundColor Cyan
    Stop-ScheduledTask -TaskName $TaskName
}

Write-Host "Removing '$TaskName'..." -ForegroundColor Cyan
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false

Write-Host "Done. dibs will no longer start at logon." -ForegroundColor Green
