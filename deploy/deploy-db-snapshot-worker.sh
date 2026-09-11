#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: deploy-db-snapshot-worker.sh BUNDLE

Required environment:
  ALIS_DB_SNAPSHOT_SSH_TARGET
  ALIS_DB_SNAPSHOT_SSH_KEY
  ALIS_OLD_SNAPSHOT_WORKER_DISABLED=1

The database host must already contain /etc/alis/db-snapshot-worker.env with
localhost DATABASE_URL and DATA_PLATFORM_DATABASE_URL values. Apply the sim
migration before enabling this worker.
EOF
}

[[ ${1:-} != "--help" ]] || { usage; exit 0; }
bundle=${1:-}
test -f "$bundle" || { usage >&2; exit 2; }
: "${ALIS_DB_SNAPSHOT_SSH_TARGET:?required}"
: "${ALIS_DB_SNAPSHOT_SSH_KEY:?required}"
test "${ALIS_OLD_SNAPSHOT_WORKER_DISABLED:-}" = 1 || {
  echo "refusing deployment until the old console snapshot worker is disabled" >&2
  exit 3
}

manifest=$(tar -xOf "$bundle" ./.alis-release-manifest)
commit=$(sed -n 's/^git_commit=//p' <<<"$manifest")
sim_head=$(sed -n 's/^sim_migration_head=//p' <<<"$manifest")
test "$(sed -n 's/^format=//p' <<<"$manifest")" = alis_release_bundle_v2
[[ "$commit" =~ ^[0-9a-f]{40}$ ]]
test -n "$sim_head"

sha=$(sha256sum "$bundle" 2>/dev/null | awk '{print $1}' || shasum -a 256 "$bundle" | awk '{print $1}')
stamp=$(date +%Y%m%d-%H%M%S)
short=${commit:0:7}
release_root=${ALIS_DB_SNAPSHOT_RELEASE_ROOT:-/opt/alis-db-snapshot/releases}
release="$release_root/${stamp}-${short}"
remote_archive="/tmp/alis-db-snapshot-${short}.tar.gz"
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=10)
db_ssh() { ssh "${ssh_opts[@]}" -i "$ALIS_DB_SNAPSHOT_SSH_KEY" "$ALIS_DB_SNAPSHOT_SSH_TARGET" "$@"; }

scp -q "${ssh_opts[@]}" -i "$ALIS_DB_SNAPSHOT_SSH_KEY" \
  "$bundle" "$ALIS_DB_SNAPSHOT_SSH_TARGET:$remote_archive"

db_ssh "sudo bash -s" <<REMOTE
set -euo pipefail
sha256sum '$remote_archive' | awk '{print \$1}' | grep -Fx '$sha'
test -f /etc/alis/db-snapshot-worker.env
grep -Eq '^DATABASE_URL=postgresql://[^@]+@(127\\.0\\.0\\.1|localhost):5432/' /etc/alis/db-snapshot-worker.env
grep -Eq '^DATA_PLATFORM_DATABASE_URL=postgresql://[^@]+@(127\\.0\\.0\\.1|localhost):5432/' /etc/alis/db-snapshot-worker.env
id -u alis-snapshot >/dev/null 2>&1 || useradd --system --home-dir /opt/alis-db-snapshot --shell /usr/sbin/nologin alis-snapshot
install -d -o alis-snapshot -g alis-snapshot -m 750 '$release_root' /opt/alis-db-snapshot/shared /etc/alis
if [ ! -x /opt/alis-db-snapshot/shared/app-venv/bin/python ]; then
  python3 -m venv /opt/alis-db-snapshot/shared/app-venv
fi
install -d -o alis-snapshot -g alis-snapshot -m 750 '$release'
tar -xzf '$remote_archive' -C '$release'
chown -R alis-snapshot:alis-snapshot '$release'
rm -rf '$release/backend/.venv' && ln -sfn /opt/alis-db-snapshot/shared/app-venv '$release/backend/.venv'
/opt/alis-db-snapshot/shared/app-venv/bin/pip install -q -r '$release/backend/requirements.txt'
set -a
. /etc/alis/db-snapshot-worker.env
set +a
PYTHONPATH='$release/backend' /opt/alis-db-snapshot/shared/app-venv/bin/python - <<'PY'
from app.services.system_release_center import SIM_SCHEMA_HEAD, database_revisions
current = database_revisions()
if current.get("sim") != SIM_SCHEMA_HEAD:
    raise SystemExit(f"sim migration mismatch: {current.get('sim')} != {SIM_SCHEMA_HEAD}")
PY
install -o root -g root -m 644 '$release/deploy/alis-db-snapshot-worker.service' /etc/systemd/system/alis-db-snapshot-worker.service
install -o root -g root -m 755 '$release/deploy/alis-db-egress-guard.sh' /usr/local/sbin/alis-db-egress-guard
install -o root -g root -m 644 '$release/deploy/alis-db-egress-guard.service' /etc/systemd/system/alis-db-egress-guard.service
install -o root -g root -m 644 '$release/deploy/alis-db-egress-guard-account.service' /etc/systemd/system/alis-db-egress-guard-account.service
install -o root -g root -m 644 '$release/deploy/alis-db-egress-guard-account.timer' /etc/systemd/system/alis-db-egress-guard-account.timer
install -o root -g root -m 755 '$release/deploy/alis-market-dataset-read' /usr/local/sbin/alis-market-dataset-read
ln -sfn '$release/backend' /opt/alis-db-snapshot/backend
chown -h alis-snapshot:alis-snapshot /opt/alis-db-snapshot/backend
systemctl daemon-reload
systemctl enable alis-db-egress-guard
systemctl restart alis-db-egress-guard
systemctl enable --now alis-db-egress-guard-account.timer
systemctl enable alis-db-snapshot-worker
# enable --now does not restart an already running Python process after the
# release symlink changes; it would keep old readers and their caches alive.
systemctl restart alis-db-snapshot-worker
test "\$(systemctl is-active alis-db-snapshot-worker)" = active
REMOTE

echo "release_commit=$commit"
echo "db_snapshot_release=$release"
echo "status=healthy"
