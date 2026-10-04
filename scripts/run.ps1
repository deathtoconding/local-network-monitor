# Local Network Monitor - quick start for Windows PowerShell.
#
#   .\scripts\run.ps1              # dashboard on http://127.0.0.1:8000
#   .\scripts\run.ps1 -Once        # single collection cycle, printed, then exit
#   .\scripts\run.ps1 -CheckConfig # validate config.yaml
#   .\scripts\run.ps1 -ApiOnly     # serve existing data without collecting
#
# The script creates .venv and installs the runtime requirements on first run.
[CmdletBinding()]
param(
    [switch]$Once,
    [switch]$CheckConfig,
    [switch]$ApiOnly,
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Creating virtual environment in .venv ..." -ForegroundColor Cyan
    py -3 -m venv .venv
    & $python -m pip install --upgrade pip
    & $python -m pip install -r requirements.txt
}

if ($CheckConfig) {
    & $python -m network_monitor --check-config
    exit $LASTEXITCODE
}

$arguments = @()
if ($Once) { $arguments += "--once" }
if ($ApiOnly) { $arguments += "--api-only" }
$arguments += @("--port", "$Port")

& $python -m network_monitor @arguments
exit $LASTEXITCODE
