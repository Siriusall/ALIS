#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")" && pwd)"
backend_dir="$root/backend"
frontend_dir="$root/frontend"
backend_pid=""
main_frontend_pid=""

# PostgreSQL is the target runtime for the main business service.  The
# PostgreSQL is now the only application database; SQLite has been removed.
export DATABASE_URL="${DATABASE_URL:-postgresql://sirius@127.0.0.1:5432/alis_sim}"
export DATA_PLATFORM_DATABASE_URL="${DATA_PLATFORM_DATABASE_URL:-postgresql://sirius@127.0.0.1:5432/alis_data}"

cleanup() {
  if [[ -n "$backend_pid" ]] && kill -0 "$backend_pid" 2>/dev/null; then
    echo "Stopping backend (PID $backend_pid)..."
    kill "$backend_pid" 2>/dev/null || true
  fi
  for pid in "$main_frontend_pid"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT INT TERM

port_busy() {
  lsof -ti tcp:"$1" >/dev/null 2>&1
}

backend_healthy() {
  curl -fsS --max-time 1 "http://127.0.0.1:$1/api/health" >/dev/null 2>&1
}

if [[ ! -x "$backend_dir/.venv/bin/python" ]]; then
  echo "Backend .venv is missing. Create it with Python 3.11:"
  echo "  cd backend && python3.11 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

if [[ ! -d "$frontend_dir/node_modules" ]]; then
  echo "Frontend dependencies are missing. Run: npm --prefix frontend install"
  exit 1
fi

if backend_healthy 8000; then
  echo "Backend already running: http://127.0.0.1:8000"
else
  (
    cd "$backend_dir"
    echo "Backend: http://127.0.0.1:8000"
    exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
  ) &
  backend_pid=$!
fi

sleep 2

if port_busy 5173; then
  echo "Frontend already occupying port 5173"
else
  (
    cd "$frontend_dir"
    echo "Frontend: http://localhost:5173"
    exec npm run dev
  ) &
  main_frontend_pid=$!
fi

if [[ -n "$main_frontend_pid" ]]; then
  wait "$main_frontend_pid"
else
  while true; do sleep 60; done
fi
