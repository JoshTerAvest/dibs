#Requires -Version 5.1
<#
.SYNOPSIS
    Liveness check for the dibs server. If GET /healthz fails, (re)starts the "dibs"
    Scheduled Task. Meant to run every few minutes from the "dibs-watchdog" task installed
    by install-watchdog.ps1; safe to run by hand.
.DESCRIPTION
    Why: on 2026-09-09 the "dibs" task sat in the Ready state (server not running) with no
    shutdown line in data\dibs.log, so every MCP client failed to connect at startup. The task
    only restarts on a crash it can see; this closes the gap for exits it cannot.
    Appends to data\watchdog.log only when something was wrong.
#>
param(
    [string]$TaskName = "dibs",
    [string]$Url = "http://127.0.0.1:7474/healthz"
)

$ErrorActionPreference = "Continue"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$log = Join-Path $root "data\watchdog.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

function Log([string]$msg) {
    $line = "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $msg
    Add-Content -Path $log -Value $line
    Write-Host $line
}

try {
    $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
    if ($resp.StatusCode -eq 200 -and $resp.Content -match '"ok"\s*:\s*true') { exit 0 }
    Log "healthz returned $($resp.StatusCode): $($resp.Content)"
} catch {
    Log "healthz unreachable: $($_.Exception.Message)"
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Log "no Scheduled Task '$TaskName' to restart - run scripts\install-task.ps1"
    exit 1
}
if ($task.State -eq "Running") {
    # The wrapper is alive but python is not answering: stop the wrapper so the restart is clean.
    Log "task '$TaskName' is Running but not answering - stopping it first"
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}
Log "starting task '$TaskName'"
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 10
try {
    $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
    Log "recovered: healthz $($resp.StatusCode)"
    exit 0
} catch {
    Log "still down after restart: $($_.Exception.Message)"
    exit 1
}
