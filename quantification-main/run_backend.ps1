$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
if (-not $root) { $root = Get-Location }

$backendDir = Join-Path $root "backend"
$venvPython = Join-Path $backendDir ".venv\Scripts\python.exe"

if (-not (Test-Path $backendDir)) {
  throw "backend directory not found: $backendDir"
}

if (-not (Test-Path $venvPython)) {
  Write-Host "Backend .venv is missing. Run: npm run install:backend" -ForegroundColor Yellow
  throw "Python 3.11 backend environment is not ready."
}

& (Join-Path $root "scripts\start_redis.ps1")

Set-Location $backendDir
Write-Host "Backend: http://127.0.0.1:8000" -ForegroundColor Green
& $venvPython -m uvicorn app.main:app --host 127.0.0.1 --port 8000
