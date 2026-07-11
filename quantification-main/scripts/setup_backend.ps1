$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $root "backend"
$venvPython = Join-Path $backendDir ".venv\Scripts\python.exe"

if (-not (Test-Path $backendDir)) {
  throw "backend directory not found: $backendDir"
}

try {
  $pythonVersion = py -3.11 --version 2>&1
} catch {
  throw "Python 3.11 is required. Install Python 3.11, then rerun: npm run install:backend"
}

Write-Host "Using $pythonVersion" -ForegroundColor Cyan
Set-Location $backendDir

if (-not (Test-Path $venvPython)) {
  Write-Host "Creating backend virtual environment with Python 3.11..." -ForegroundColor Yellow
  py -3.11 -m venv .venv
}

Write-Host "Installing backend dependencies..." -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
  throw "pip upgrade failed with exit code $LASTEXITCODE"
}
& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
  throw "backend dependency installation failed with exit code $LASTEXITCODE"
}

Write-Host "Backend environment is ready: $venvPython" -ForegroundColor Green
