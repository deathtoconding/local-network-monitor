# Developer tasks for Windows PowerShell - the same commands CI runs.
#
#   .\scripts\dev.ps1 test
#   .\scripts\dev.ps1 check
#   .\scripts\dev.ps1 run
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("setup", "test", "cov", "lint", "format", "check", "run", "once", "build", "audit", "help")]
    [string]$Task = "help",

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

# Import the package from the working tree even before an editable install.
$env:PYTHONPATH = Join-Path $RepoRoot "src"

function Ensure-Venv {
    if (-not (Test-Path $Python)) {
        Write-Host "Creating virtual environment in .venv ..." -ForegroundColor Cyan
        py -3 -m venv .venv
        & $Python -m pip install --upgrade pip
        & $Python -m pip install -r requirements-dev.txt
    }
}

function Invoke-Step([string]$Label, [scriptblock]$Action) {
    Write-Host "==> $Label" -ForegroundColor Cyan
    & $Action
    if ($LASTEXITCODE -ne 0) { throw "$Label failed (exit $LASTEXITCODE)" }
}

switch ($Task) {
    "setup"  { Ensure-Venv; Invoke-Step "install dev requirements" { & $Python -m pip install -r requirements-dev.txt } }
    "test"   { Ensure-Venv; Invoke-Step "pytest" { & $Python -m pytest @Rest } }
    "cov"    { Ensure-Venv; Invoke-Step "pytest + coverage" { & $Python -m pytest --cov=network_monitor --cov-report=term-missing --cov-fail-under=85 } }
    "lint"   {
        Ensure-Venv
        Invoke-Step "ruff check" { & $Python -m ruff check src tests }
        Invoke-Step "ruff format --check" { & $Python -m ruff format --check src tests }
    }
    "format" {
        Ensure-Venv
        Invoke-Step "ruff --fix" { & $Python -m ruff check src tests --fix }
        Invoke-Step "ruff format" { & $Python -m ruff format src tests }
    }
    "check"  {
        Ensure-Venv
        Invoke-Step "ruff check" { & $Python -m ruff check src tests }
        Invoke-Step "ruff format --check" { & $Python -m ruff format --check src tests }
        Invoke-Step "pytest + coverage" { & $Python -m pytest --cov=network_monitor --cov-fail-under=85 }
        Invoke-Step "config validation" { & $Python -m network_monitor --check-config }
    }
    "run"    { Ensure-Venv; Invoke-Step "monitor" { & $Python -m network_monitor @Rest } }
    "once"   { Ensure-Venv; Invoke-Step "single cycle" { & $Python -m network_monitor --once } }
    "build"  {
        Ensure-Venv
        Invoke-Step "build" { & $Python -m build }
        Invoke-Step "twine check" { & $Python -m twine check dist/* }
    }
    "audit"  { Ensure-Venv; Invoke-Step "pip-audit" { & $Python -m pip_audit } }
    default  {
        Write-Host "Usage: .\scripts\dev.ps1 <task>" -ForegroundColor Cyan
        Write-Host "  setup   Create .venv and install dev requirements"
        Write-Host "  test    Run the test suite"
        Write-Host "  cov     Tests with coverage (CI floor 85%)"
        Write-Host "  lint    ruff check + format check"
        Write-Host "  format  Apply ruff fixes and formatting"
        Write-Host "  check   Everything CI runs before packaging"
        Write-Host "  run     Start the dashboard"
        Write-Host "  once    Run one collection cycle"
        Write-Host "  build   Build wheel/sdist and verify metadata"
        Write-Host "  audit   Dependency vulnerability scan"
    }
}
