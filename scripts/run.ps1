#Requires -Version 5.1
<#
.SYNOPSIS
    Runs the dibs server from the repo's .venv. Used directly and by the
    "dibs" Scheduled Task installed by install-task.ps1.
.DESCRIPTION
    Any extra arguments are passed through to `python -m dibs serve`, e.g.
    `.\scripts\run.ps1 --host 0.0.0.0 --port 7474`.
#>
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "No .venv found at $root\.venv — create it first:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "    uv venv --python 3.12" -ForegroundColor Cyan
    Write-Host "    uv sync" -ForegroundColor Cyan
    Write-Host ""
    exit 1
}

$logDir = Join-Path $root "data"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "dibs.log"
# Run through cmd so stderr (uvicorn logs there) is plain text in the log. Under PowerShell 5.1,
# `*>>` with $ErrorActionPreference=Stop turns the first stderr line into a terminating error.
$ErrorActionPreference = "Continue"
$quotedArgs = ""
if ($Args -and $Args.Count -gt 0) { $quotedArgs = ($Args | ForEach-Object { '"' + $_ + '"' }) -join " " }
& cmd.exe /c "`"$python`" -m dibs serve $quotedArgs >> `"$log`" 2>&1"
exit $LASTEXITCODE
