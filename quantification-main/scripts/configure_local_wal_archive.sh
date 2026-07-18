#!/usr/bin/env bash
# Print or apply the PostgreSQL WAL archive settings needed for PITR.
# This is intentionally opt-in: WAL archiving consumes disk and requires an
# explicit retention policy. Usage: ./scripts/configure_local_wal_archive.sh --apply
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$ROOT/.env" ]]; then set -a; source "$ROOT/.env"; set +a; fi
: "${DATA_PLATFORM_DATABASE_URL:?DATA_PLATFORM_DATABASE_URL must be configured}"
ARCHIVE_DIR="${ALIS_WAL_ARCHIVE_ROOT:-$ROOT/.cache/wal-archive}"

if [[ "${1:-}" != "--apply" ]]; then
  echo "WAL archive is not enabled automatically. To enable local PITR, run:"
  echo "  ALIS_WAL_ARCHIVE_ROOT='$ARCHIVE_DIR' $0 --apply"
  exit 0
fi

mkdir -p "$ARCHIVE_DIR"
psql "$DATA_PLATFORM_DATABASE_URL" -v ON_ERROR_STOP=1 <<SQL
ALTER SYSTEM SET archive_mode = 'on';
ALTER SYSTEM SET archive_command = 'test ! -f $ARCHIVE_DIR/%f && cp %p $ARCHIVE_DIR/%f';
ALTER SYSTEM SET wal_level = 'replica';
SELECT pg_reload_conf();
SQL
echo "WAL archive configured. Restart PostgreSQL once, then verify with: SHOW archive_mode;"
