$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
if (-not $root) { $root = Get-Location }

$backendDir = Join-Path $root "backend"
$frontendDir = Join-Path $root "frontend"
$venvPython = Join-Path $backendDir ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
  Write-Host "Backend .venv is missing. Run: npm run install:backend" -ForegroundColor Yellow
  throw "Python 3.11 backend environment is not ready."
}

if (-not (Test-Path (Join-Path $frontendDir "node_modules"))) {
  Write-Host "Frontend dependencies are missing. Run: npm run install:frontend" -ForegroundColor Yellow
  throw "Frontend node_modules is not ready."
}

Start-Process powershell -ArgumentList @(
  "-NoExit",
  "-ExecutionPolicy",
  "Bypass",
  "-Command",
  "Set-Location '$backendDir'; Write-Host 'Backend: http://127.0.0.1:8000' -ForegroundColor Green; .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
)

Start-Sleep -Seconds 2

Start-Process powershell -ArgumentList @(
  "-NoExit",
  "-ExecutionPolicy",
  "Bypass",
  "-Command",
  "Set-Location '$frontendDir'; Write-Host 'Frontend: http://localhost:5173' -ForegroundColor Green; npm run dev"
)

Write-Host "Started backend and frontend windows. Use POST /api/init when you want to refresh market data caches." -ForegroundColor Cyan
