#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")" && pwd)"
backend_dir="$root/backend"
frontend_dir="$root/frontend"
backend_pid=""

cleanup() {
  if [[ -n "$backend_pid" ]] && kill -0 "$backend_pid" 2>/dev/null; then
    echo "Stopping backend (PID $backend_pid)..."
    kill "$backend_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if [[ ! -x "$backend_dir/.venv/bin/python" ]]; then
  echo "Backend .venv is missing. Create it with Python 3.11:"
  echo "  cd backend && python3.11 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

if [[ ! -d "$frontend_dir/node_modules" ]]; then
  echo "Frontend dependencies are missing. Run: npm --prefix frontend install"
  exit 1
fi

(
  cd "$backend_dir"
  echo "Backend: http://127.0.0.1:8000"
  exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
) &
backend_pid=$!

sleep 2

(
  cd "$frontend_dir"
  echo "Frontend: http://localhost:5173"
  exec npm run dev
)
