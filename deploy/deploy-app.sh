#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'HELP'
Usage: deploy-app.sh BUNDLE

Required environment:
  ALIS_APP_SSH_TARGET, ALIS_APP_SSH_KEY

Optional environment:
  ALIS_CHAIN_MONITOR_ENV_FILE  Upload an already provisioned monitor env file
                               before installing the monitor unit.
HELP
}

bundle=${1:-}
test -f "$bundle" || { usage >&2; exit 2; }
: "${ALIS_APP_SSH_TARGET:?required}"
: "${ALIS_APP_SSH_KEY:?required}"

app_root=/opt/alis/releases
manifest=$(tar -xOf "$bundle" ./.alis-release-manifest)
commit=$(sed -n 's/^git_commit=//p' <<<"$manifest")
sim_head=$(sed -n 's/^sim_migration_head=//p' <<<"$manifest")
data_head=$(sed -n 's/^data_migration_head=//p' <<<"$manifest")
test "$(sed -n 's/^format=//p' <<<"$manifest")" = alis_release_bundle_v2
[[ "$commit" =~ ^[0-9a-f]{40}$ ]]

sha=$(sha256sum "$bundle" 2>/dev/null | awk '{print $1}' || shasum -a 256 "$bundle" | awk '{print $1}')
stamp=$(date +%Y%m%d-%H%M%S)
short=${commit:0:7}
remote_archive="/tmp/alis-app-${short}.tar.gz"
app_release="$app_root/${stamp}-${short}"
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=10 -o ProxyCommand=none)

app_ssh() { ssh "${ssh_opts[@]}" -i "$ALIS_APP_SSH_KEY" "$ALIS_APP_SSH_TARGET" "$@"; }

scp -O -q "${ssh_opts[@]}" -i "$ALIS_APP_SSH_KEY" "$bundle" "$ALIS_APP_SSH_TARGET:$remote_archive"

if [[ -n ${ALIS_CHAIN_MONITOR_ENV_FILE:-} ]]; then
  test -f "$ALIS_CHAIN_MONITOR_ENV_FILE" || {
    echo "ALIS_CHAIN_MONITOR_ENV_FILE is missing: $ALIS_CHAIN_MONITOR_ENV_FILE" >&2
    exit 2
  }
  monitor_env_archive="/tmp/alis-chain-monitor-env-${short}"
  scp -O -q "${ssh_opts[@]}" -i "$ALIS_APP_SSH_KEY" \
    "$ALIS_CHAIN_MONITOR_ENV_FILE" "$ALIS_APP_SSH_TARGET:$monitor_env_archive"
  app_ssh "sudo install -o root -g root -m 600 '$monitor_env_archive' /etc/alis/chain-monitor.env; sudo rm -f '$monitor_env_archive'"
fi

# Pass release metadata as positional arguments and the operation as stdin.
# This avoids nested shell quoting for paths, SQL, awk, and substitutions.
app_ssh "sudo bash -s -- '$remote_archive' '$app_release' '$sha' '$sim_head' '$data_head' '$commit'" <<'REMOTE_APP_INSTALL'
set -euo pipefail
remote_archive=$1
app_release=$2
sha=$3
sim_head=$4
data_head=$5
commit=$6
test "$(sha256sum "$remote_archive" | awk '{print $1}')" = "$sha"
test ! -e "$app_release"
mkdir -p "$app_release"
tar -xzf "$remote_archive" -C "$app_release"
test -f "$app_release/frontend/dist/index.html"
test -f "$app_release/backend/scripts/manage_migrations.py"
! find "$app_release" -name '._*' -print -quit | grep -q .
ln -s /opt/alis/shared/app-venv "$app_release/backend/.venv"
ln -s /opt/alis/shared/runtime-cache "$app_release/backend/.cache"
chown -R alis:alis "$app_release"
chmod 755 "$app_release"
find "$app_release/frontend/dist" -type d -exec chmod 755 {} +
find "$app_release/frontend/dist" -type f -exec chmod 644 {} +
sudo -u www-data test -r "$app_release/frontend/dist/index.html"
/opt/alis/shared/app-venv/bin/pip install -q -r "$app_release/backend/requirements.txt"
/opt/alis/shared/app-venv/bin/python -m compileall -q "$app_release/backend/app" "$app_release/backend/migrations"
install -d -o root -g root -m 755 /usr/local/libexec /usr/local/sbin
install -o root -g root -m 755 "$app_release/deploy/alis-chain-monitor-config-check" /usr/local/sbin/alis-chain-monitor-config-check
install -o root -g root -m 755 "$app_release/deploy/alis-chain-monitor.py" /usr/local/libexec/alis-chain-monitor.py
install -o root -g root -m 644 "$app_release/deploy/alis-chain-monitor.service" /etc/systemd/system/alis-chain-monitor.service
install -o root -g root -m 644 "$app_release/deploy/alis-chain-monitor.timer" /etc/systemd/system/alis-chain-monitor.timer
install -o root -g root -m 644 "$app_release/deploy/alis-alert-worker.service" /etc/systemd/system/alis-alert-worker.service
test -f /etc/alis/chain-monitor.env || { echo 'missing /etc/alis/chain-monitor.env; run deploy/bootstrap-chain-monitor-ssh.sh first' >&2; exit 78; }
/usr/local/sbin/alis-chain-monitor-config-check
systemctl daemon-reload
systemctl enable --now alis-chain-monitor.timer
systemctl enable --now alis-alert-worker
test "$(systemctl is-active alis-chain-monitor.timer)" = active
test "$(systemctl is-active alis-alert-worker)" = active
systemctl disable --now alis-console-snapshot-worker 2>/dev/null || true
set -a
source /etc/alis/alis.env
set +a
cd "$app_release/backend"
.venv/bin/python scripts/manage_migrations.py upgrade
test "$(psql "$DATABASE_URL" -Atc 'select version_num from alembic_version')" = "$sim_head"
test "$(psql "$DATA_PLATFORM_DATABASE_URL" -Atc 'select version_num from alembic_version')" = "$data_head"
nginx -t
sed -i "s/^ALIS_BUILD_GIT_SHA=.*/ALIS_BUILD_GIT_SHA=$commit/" /etc/alis/alis.env
grep -q "^ALIS_BUILD_GIT_SHA=$commit\$" /etc/alis/alis.env
ln -sfn "$app_release" /opt/alis/current
systemctl restart alis-api
for n in $(seq 1 40); do curl -fsS --max-time 5 http://127.0.0.1:8000/api/live >/dev/null && exit 0; sleep 2; done
exit 1
REMOTE_APP_INSTALL

governance_payload=$(printf '%s' \
  'from app.services.system_release_center import run_release_governance_cycle,release_status; run_release_governance_cycle(actor="production_release_v2"); s=release_status(); a=s.get("active_release") or {}; c=((a.get("manifest") or {}).get("components") or {}).get("code.backend") or {}; assert s.get("ok") and not s.get("drift") and c.get("version") == "'"$commit"'", s' \
  | base64 | tr -d '\n')
app_ssh "sudo bash -s -- '$governance_payload'" <<'REMOTE_GOVERNANCE'
set -euo pipefail
set -a
source /etc/alis/alis.env
set +a
cd /opt/alis/current/backend
printf '%s' "$1" | base64 -d | .venv/bin/python -
REMOTE_GOVERNANCE

# Run the freshly installed monitor once after governance. This is an explicit
# release gate for the snapshot worker, database heartbeat, and research workers.
app_ssh 'sudo systemctl start alis-chain-monitor.service'
app_ssh 'test "$(systemctl is-active alis-chain-monitor.timer)" = active'
app_ssh 'test "$(systemctl show alis-chain-monitor.service -p ExecMainStatus --value)" = 0'

echo "app_release=$app_release"
echo "status=healthy"
