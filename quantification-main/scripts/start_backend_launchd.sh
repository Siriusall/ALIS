#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/sirius/Developer/ALIS/quantification-main"
BACKEND_DIR="$ROOT/backend"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONUNBUFFERED="1"
export TUSHARE_CONCURRENCY="${TUSHARE_CONCURRENCY:-16}"
export DATABASE_URL="${DATABASE_URL:-postgresql://sirius@127.0.0.1:5432/alis_sim}"
export DATA_PLATFORM_DATABASE_URL="${DATA_PLATFORM_DATABASE_URL:-postgresql://sirius@127.0.0.1:5432/alis_data}"

cd "$BACKEND_DIR"
exec "$BACKEND_DIR/.venv/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
