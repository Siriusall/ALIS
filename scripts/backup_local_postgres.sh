#!/usr/bin/env bash
# Create a portable local backup of both ALIS PostgreSQL databases.
# Usage: ./scripts/backup_local_postgres.sh [destination-directory]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

: "${DATABASE_URL:?DATABASE_URL must be configured}"
: "${DATA_PLATFORM_DATABASE_URL:?DATA_PLATFORM_DATABASE_URL must be configured}"

DESTINATION="${1:-${ALIS_BACKUP_ROOT:-$ROOT/.cache/backups}/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$DESTINATION"

# A custom dump is often smaller than the live DB, but it is unsafe to rely
# on compression when the machine is already nearly full.  Reserve 150% of
# the two source DB sizes before creating any partial backup files.
db_bytes() {
  psql "$1" -Atc "SELECT pg_database_size(current_database());"
}
available_bytes() {
  df -Pk "$DESTINATION" | awk 'NR==2 {print $4 * 1024}'
}
sim_bytes="$(db_bytes "$DATABASE_URL")"
data_bytes="$(db_bytes "$DATA_PLATFORM_DATABASE_URL")"
required_bytes=$(( (sim_bytes + data_bytes) * 3 / 2 ))
free_bytes="$(available_bytes)"
if (( free_bytes < required_bytes )); then
  echo "Insufficient disk for safe backup: need at least ${required_bytes} bytes free, have ${free_bytes}." >&2
  echo "Choose an external drive with ALIS_BACKUP_ROOT before retrying." >&2
  exit 3
fi

backup_one() {
  local name="$1"
  local url="$2"
  pg_dump --format=custom --no-owner --no-privileges --file "$DESTINATION/$name.dump" "$url"
  shasum -a 256 "$DESTINATION/$name.dump" > "$DESTINATION/$name.dump.sha256"
}

backup_one alis_sim "$DATABASE_URL"
backup_one alis_data "$DATA_PLATFORM_DATABASE_URL"
cat > "$DESTINATION/manifest.json" <<EOF
{"schema_version":"alis_local_postgres_backup_v1","created_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","databases":["alis_sim","alis_data"],"restore_script":"scripts/restore_local_postgres.sh"}
EOF
echo "ALIS backup completed: $DESTINATION"
