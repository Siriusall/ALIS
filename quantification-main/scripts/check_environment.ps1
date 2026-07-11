$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $root "backend"
$frontendDir = Join-Path $root "frontend"
$venvPython = Join-Path $backendDir ".venv\Scripts\python.exe"
$frontendNodeModules = Join-Path $frontendDir "node_modules"
$backendRequirements = Join-Path $backendDir "requirements.txt"
$rootPackageLock = Join-Path $root "package-lock.json"
$allInOneCache = Join-Path $backendDir ".cache\all_in_one"
$rootEnvFile = Join-Path $root ".env"
$backendEnvFile = Join-Path $backendDir ".env"

function Write-Check {
  param(
    [string]$Name,
    [bool]$Ok,
    [string]$Detail,
    [string]$Hint = ""
  )

  $status = if ($Ok) { "OK" } else { "FAIL" }
  $color = if ($Ok) { "Green" } else { "Yellow" }
  Write-Host ("[{0}] {1}: {2}" -f $status, $Name, $Detail) -ForegroundColor $color
  if (-not $Ok -and $Hint) {
    Write-Host ("      -> {0}" -f $Hint) -ForegroundColor DarkYellow
  }
}

Write-Host "Quantification environment check" -ForegroundColor Cyan
Write-Host ("Root: {0}" -f $root) -ForegroundColor DarkCyan

$py311Ok = $false
$py311Detail = ""
try {
  $py311Detail = (py -3.11 --version 2>&1 | Out-String).Trim()
  $py311Ok = $LASTEXITCODE -eq 0 -and $py311Detail -match "Python 3\.11"
} catch {
  $py311Detail = $_.Exception.Message
}
Write-Check "Python 3.11 launcher" $py311Ok $py311Detail "Install Python 3.11, then run: npm run install:backend"

$venvOk = Test-Path $venvPython
Write-Check "Backend virtualenv" $venvOk $venvPython "Run: npm run install:backend"

if ($venvOk) {
  $fastapiOk = $false
  $fastapiDetail = ""
  try {
    $fastapiDetail = (& $venvPython -c "import fastapi, uvicorn; print('fastapi+uvicorn import ok')" 2>&1 | Out-String).Trim()
    $fastapiOk = $LASTEXITCODE -eq 0
  } catch {
    $fastapiDetail = $_.Exception.Message
  }
  Write-Check "Backend imports" $fastapiOk $fastapiDetail "Run: npm run install:backend"
}

$reqOk = Test-Path $backendRequirements
Write-Check "Backend requirements" $reqOk $backendRequirements "Missing requirements.txt will make backend setup unreproducible."

$frontendDepsOk = Test-Path $frontendNodeModules
Write-Check "Frontend dependencies" $frontendDepsOk $frontendNodeModules "Run: npm run install:frontend"

$lockOk = Test-Path $rootPackageLock
Write-Check "Root package lock" $lockOk $rootPackageLock "Run npm install once from the project root."

$wrongBackendPackage = Test-Path (Join-Path $backendDir "package.json")
$wrongBackendNodeModules = Test-Path (Join-Path $backendDir "node_modules")
Write-Check "No backend Node package" (-not $wrongBackendPackage) "backend/package.json absent" "Delete backend/package.json; backend is Python-only."
Write-Check "No backend node_modules" (-not $wrongBackendNodeModules) "backend/node_modules absent" "Delete backend/node_modules; frontend dependencies live under frontend/."

$cacheDirOk = Test-Path $allInOneCache
$cacheFileCount = if ($cacheDirOk) {
  (Get-ChildItem -Path $allInOneCache -Filter "*.json" -ErrorAction SilentlyContinue | Measure-Object).Count
} else {
  0
}
$cacheOk = $cacheDirOk -and $cacheFileCount -gt 0
$cacheDetail = if ($cacheOk) {
  "{0} ({1} files)" -f $allInOneCache, $cacheFileCount
} elseif ($cacheDirOk) {
  "{0} (0 files)" -f $allInOneCache
} else {
  $allInOneCache
}
Write-Check "All-in-one cache" $cacheOk $cacheDetail "After backend starts, run POST /api/init before trusting selection or backtest output."

$envFile = if (Test-Path $rootEnvFile) { $rootEnvFile } elseif (Test-Path $backendEnvFile) { $backendEnvFile } else { $rootEnvFile }
$envExists = Test-Path $envFile
Write-Check "Project .env" $envExists $envFile "Copy .env.example to .env and set TUSHARE_TOKEN."

$tokenConfigured = $false
$tokenDetail = "not checked"
if ($envExists) {
  $tokenLine = Get-Content $envFile | Where-Object { $_ -match '^\s*TUSHARE_TOKEN\s*=\s*.+' } | Select-Object -First 1
  $tokenConfigured = [bool]$tokenLine
  $tokenDetail = if ($tokenConfigured) { "TUSHARE_TOKEN present in $envFile" } else { "TUSHARE_TOKEN missing or empty in $envFile" }
} else {
  $tokenDetail = "no .env file"
}
Write-Check "TuShare token" $tokenConfigured $tokenDetail "Set TUSHARE_TOKEN in .env, then restart backend and run POST /api/init."

if (-not $py311Ok -or -not $venvOk -or -not $frontendDepsOk) {
  exit 1
}
