$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$redisServer = Get-Command redis-server.exe -ErrorAction SilentlyContinue
if (-not $redisServer) {
  throw "Redis 8 is missing. Install it with: winget install --id taizod1024.redis-windows-fork --exact"
}
$redisServerPath = [string](Get-Item $redisServer.Source).Target
$redisDir = Split-Path $redisServerPath -Parent

try {
  $clientPath = Join-Path $redisDir "redis-cli.exe"
  if (Test-Path $clientPath) {
    $pong = (& $clientPath -h 127.0.0.1 -p 6379 ping 2>$null | Out-String).Trim()
    if ($pong -eq "PONG") {
      Write-Host "Redis: redis://127.0.0.1:6379/0 (already running)" -ForegroundColor Green
      exit 0
    }
  }
} catch {
  # Start a local instance below when no existing instance answers.
}

$dataDir = Join-Path $root ".runtime\redis"
New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
Start-Process -FilePath $redisServerPath -ArgumentList @(
  "--bind", "127.0.0.1",
  "--port", "6379",
  "--appendonly", "yes",
  "--dir", $dataDir
) -WorkingDirectory $dataDir -WindowStyle Hidden

for ($attempt = 1; $attempt -le 20; $attempt++) {
  Start-Sleep -Milliseconds 250
  try {
    $clientPath = Join-Path $redisDir "redis-cli.exe"
    $pong = (& $clientPath -h 127.0.0.1 -p 6379 ping 2>$null | Out-String).Trim()
    if ($pong -eq "PONG") {
      Write-Host "Redis: redis://127.0.0.1:6379/0" -ForegroundColor Green
      exit 0
    }
  } catch {}
}

throw "Redis did not become ready on 127.0.0.1:6379."
