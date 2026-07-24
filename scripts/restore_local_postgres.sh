#!/usr/bin/env bash
# Restore an ALIS local backup.  The target databases must already exist.
# Usage: ./scripts/restore_local_postgres.sh /path/to/backup [--clean]
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 /path/to/backup [--clean]" >&2
  exit 2
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi
: "${DATABASE_URL:?DATABASE_URL must be configured}"
: "${DATA_PLATFORM_DATABASE_URL:?DATA_PLATFORM_DATABASE_URL must be configured}"

SOURCE="$1"
CLEAN_FLAG=""
[[ "${2:-}" == "--clean" ]] && CLEAN_FLAG="--clean --if-exists"
[[ -f "$SOURCE/alis_sim.dump" && -f "$SOURCE/alis_data.dump" ]] || { echo "Backup files missing" >&2; exit 2; }

verify_one() {
  local file="$1"
  [[ -f "$file.sha256" ]] && (cd "$(dirname "$file")" && shasum -a 256 -c "$(basename "$file").sha256")
}
restore_one() {
  local name="$1"
  local url="$2"
  verify_one "$SOURCE/$name.dump"
  # --clean is deliberately opt-in because it drops existing objects.
  pg_restore --dbname "$url" --no-owner --no-privileges $CLEAN_FLAG "$SOURCE/$name.dump"
}

restore_one alis_sim "$DATABASE_URL"
restore_one alis_data "$DATA_PLATFORM_DATABASE_URL"
echo "ALIS restore completed from: $SOURCE"
