#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/sirius/Developer/ALIS/quantification-main"
FRONTEND_DIR="$ROOT/frontend"

export PATH="/Users/sirius/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export BROWSER="none"
export VITE_API_BASE="${VITE_API_BASE:-http://127.0.0.1:8000}"

cd "$FRONTEND_DIR"
exec npm run dev -- --host 127.0.0.1 --port 5173 --strictPort
