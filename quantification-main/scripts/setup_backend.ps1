$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $root "backend"
$venvPython = Join-Path $backendDir ".venv\Scripts\python.exe"

if (-not (Test-Path $backendDir)) {
  throw "backend directory not found: $backendDir"
}

Set-Location $backendDir

if (-not (Test-Path $venvPython)) {
  Write-Host "Creating backend virtual environment with Python 3.11..." -ForegroundColor Yellow
  $uv = Get-Command uv -ErrorAction SilentlyContinue
  if ($uv) {
    & $uv.Source venv --python 3.11 .venv
  } else {
    try {
      py -3.11 -m venv .venv
    } catch {
      throw "Python 3.11 or uv is required. Install one, then rerun: npm run install:backend"
    }
  }
}

Write-Host "Installing backend dependencies..." -ForegroundColor Cyan
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) {
  & $uv.Source pip install --python $venvPython -r requirements.txt
} else {
  & $venvPython -m ensurepip --upgrade
  & $venvPython -m pip install --upgrade pip
  & $venvPython -m pip install -r requirements.txt
}
if ($LASTEXITCODE -ne 0) {
  throw "backend dependency installation failed with exit code $LASTEXITCODE"
}

Write-Host "Backend environment is ready: $venvPython" -ForegroundColor Green
