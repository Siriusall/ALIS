#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'HELP'
Usage: deploy-research-worker.sh BUNDLE

Required environment:
  ALIS_RESEARCH_WORKER_SSH_TARGET, ALIS_RESEARCH_WORKER_SSH_KEY

Optional environment:
  ALIS_RESEARCH_WORKER_RELEASE_ROOT  Default: /opt/alis-research-worker/releases
  ALIS_RESEARCH_WORKER_SSH_PROXY_COMMAND  Optional SSH ProxyCommand for WireGuard-only nodes
HELP
}

[[ ${1:-} != "--help" ]] || { usage; exit 0; }
bundle=${1:-}
test -f "$bundle" || { usage >&2; exit 2; }
: "${ALIS_RESEARCH_WORKER_SSH_TARGET:?required}"
: "${ALIS_RESEARCH_WORKER_SSH_KEY:?required}"

release_root=${ALIS_RESEARCH_WORKER_RELEASE_ROOT:-/opt/alis-research-worker/releases}
manifest=$(tar -xOf "$bundle" ./.alis-release-manifest)
commit=$(sed -n 's/^git_commit=//p' <<<"$manifest")
sim_head=$(sed -n 's/^sim_migration_head=//p' <<<"$manifest")
data_head=$(sed -n 's/^data_migration_head=//p' <<<"$manifest")
test "$(sed -n 's/^format=//p' <<<"$manifest")" = alis_release_bundle_v2
[[ "$commit" =~ ^[0-9a-f]{40}$ ]]
test -n "$sim_head"
test -n "$data_head"

sha=$(sha256sum "$bundle" 2>/dev/null | awk '{print $1}' || shasum -a 256 "$bundle" | awk '{print $1}')
stamp=$(date +%Y%m%d-%H%M%S)
short=${commit:0:7}
remote_archive="/tmp/alis-worker-${short}.tar.gz"
release="$release_root/${stamp}-${short}"
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=10 -o ProxyCommand=none)
if [[ -n ${ALIS_RESEARCH_WORKER_SSH_PROXY_COMMAND:-} ]]; then
  ssh_opts=(-o BatchMode=yes -o ConnectTimeout=10 -o "ProxyCommand=${ALIS_RESEARCH_WORKER_SSH_PROXY_COMMAND}")
fi
worker_ssh() { ssh "${ssh_opts[@]}" -i "$ALIS_RESEARCH_WORKER_SSH_KEY" "$ALIS_RESEARCH_WORKER_SSH_TARGET" "$@"; }

scp -O -q "${ssh_opts[@]}" -i "$ALIS_RESEARCH_WORKER_SSH_KEY" "$bundle" "$ALIS_RESEARCH_WORKER_SSH_TARGET:$remote_archive"
old_release=$(worker_ssh 'sudo readlink -f /opt/alis-research-worker/backend')
control_was_active=$(worker_ssh 'systemctl is-active alis-control-worker 2>/dev/null || true')
worker_was_active=$(worker_ssh 'systemctl is-active alis-research-worker 2>/dev/null || true')
release_finished=0
restore_worker_on_error() {
  local exit_code=$?
  if (( ! release_finished )); then
    worker_ssh 'sudo systemctl stop alis-control-worker alis-research-worker 2>/dev/null || true' || true
    worker_ssh "sudo ln -sfn '$old_release' /opt/alis-research-worker/backend; sudo chown -h alis-research:alis-research /opt/alis-research-worker/backend; sudo systemctl daemon-reload" || true
    if [[ $control_was_active == active ]]; then
      worker_ssh 'sudo systemctl restart alis-control-worker' || true
    fi
    if [[ $worker_was_active == active ]]; then
      worker_ssh 'sudo systemctl restart alis-research-worker' || true
    fi
  fi
  exit "$exit_code"
}
trap restore_worker_on_error ERR
protection_code=$(printf '%s' \
  'from app.db.store import _conn; scope=_conn(); conn=scope.__enter__(); rows=conn.execute("SELECT worker_pool AS pool, COUNT(*) AS count FROM evolution_tasks WHERE status=? GROUP BY worker_pool", ("running",)).fetchall(); counts={str(dict(row).get("pool")): int(dict(row).get("count") or 0) for row in rows}; print("control={0} research={1}".format(counts.get("control", 0), counts.get("research", 0))); scope.__exit__(None,None,None)' \
  | base64 | tr -d '\n')
running_counts=$(worker_ssh "sudo bash -lc 'set -a; . /etc/alis/control-worker.env; set +a; cd /opt/alis-research-worker/backend; echo $protection_code | base64 -d | /opt/alis-research-worker/shared/app-venv/bin/python -'")
if [[ ! $running_counts =~ ^control=[0-9]+\ research=[0-9]+$ ]]; then
  echo "worker_protection_probe_failed; refusing release switch" >&2
  exit 1
fi
control_running=${running_counts#control=}; control_running=${control_running%% research=*}
research_running=${running_counts##* research=}
if (( control_running > 0 || research_running > 0 )); then
  # Stop both claimants before returning their leases to the queue.  This
  # keeps the release switch atomic across the control and research pools.
  [[ $control_was_active == active ]] && worker_ssh 'sudo systemctl stop alis-control-worker'
  [[ $worker_was_active == active ]] && worker_ssh 'sudo systemctl stop alis-research-worker'
  worker_ssh 'test "$(systemctl is-active alis-control-worker 2>/dev/null || true)" != active; test "$(systemctl is-active alis-research-worker 2>/dev/null || true)" != active'
  pause_code=$(printf '%s' \
    'from datetime import datetime; from zoneinfo import ZoneInfo; from app.db.store import _conn; now=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(); scope=_conn(); conn=scope.__enter__(); rows=conn.execute("SELECT id FROM evolution_tasks WHERE status=? AND COALESCE(worker_pool, ?) IN (?, ?) FOR UPDATE", ("running", "control", "control", "research")).fetchall(); ids=[int(dict(row)["id"]) for row in rows]; sql="UPDATE evolution_tasks SET status=?,phase=?,message=?,lease_owner=NULL,lease_expires_at=NULL,heartbeat_at=?,next_run_at=?,updated_at=? WHERE id=? AND status=? AND COALESCE(worker_pool, ?) IN (?, ?)"; [conn.execute(sql, ("queued", "paused_for_release", "版本更新优先：任务已保留检查点并暂停，双 Worker 更新后自动恢复", now, now, now, task_id, "running", "control", "control", "research")) for task_id in ids]; scope.__exit__(None,None,None); print(len(ids))' \
    | base64 | tr -d '\n')
  paused_count=$(worker_ssh "sudo bash -lc 'set -a; . /etc/alis/control-worker.env; set +a; cd /opt/alis-research-worker/backend; echo $pause_code | base64 -d | /opt/alis-research-worker/shared/app-venv/bin/python -'")
  [[ $paused_count =~ ^[0-9]+$ ]]
  echo "worker_tasks_paused_for_release=$paused_count"
fi

worker_ssh "sudo bash -s" <<REMOTE
set -euo pipefail
sha256sum '$remote_archive' | awk '{print \$1}' | grep -Fx '$sha'
id -u alis-research >/dev/null 2>&1 || useradd --system --home-dir /opt/alis-research-worker --shell /usr/sbin/nologin alis-research
install -d -o alis-research -g alis-research -m 750 '$release_root' /opt/alis-research-worker/shared /opt/alis-research-worker/shared/runtime-cache /etc/alis
if [ ! -x /opt/alis-research-worker/shared/app-venv/bin/python ] || [ ! -x /opt/alis-research-worker/shared/app-venv/bin/pip ]; then
  python3 -m venv --clear /opt/alis-research-worker/shared/app-venv
fi
mkdir -p '$release_root'
test ! -e '$release'
install -d -o alis-research -g alis-research -m 750 '$release'
tar -xzf '$remote_archive' -C '$release'
chown -R alis-research:alis-research '$release'
ln -sfn /opt/alis-research-worker/shared/app-venv '$release/backend/.venv'
ln -sfn /opt/alis-research-worker/shared/runtime-cache '$release/backend/.cache'
/opt/alis-research-worker/shared/app-venv/bin/pip install -q -r '$release/backend/requirements.txt'
/opt/alis-research-worker/shared/app-venv/bin/python -m compileall -q '$release/backend/app' '$release/backend/migrations'
install -o root -g root -m 644 '$release/deploy/alis-research-worker.service' /etc/systemd/system/alis-research-worker.service
install -o root -g root -m 644 '$release/deploy/alis-control-worker.service' /etc/systemd/system/alis-control-worker.service
# Snapshot construction is owned by the database-local worker.  A legacy
# console worker on this host would race the durable refresh generation.
systemctl disable --now alis-console-snapshot-worker 2>/dev/null || true
sed -i 's/^ALIS_BUILD_GIT_SHA=.*/ALIS_BUILD_GIT_SHA=$commit/' /etc/alis/research-worker.env
grep -q '^ALIS_BUILD_GIT_SHA=$commit\$' /etc/alis/research-worker.env
sed -i 's/^ALIS_BUILD_GIT_SHA=.*/ALIS_BUILD_GIT_SHA=$commit/' /etc/alis/control-worker.env
grep -q '^ALIS_BUILD_GIT_SHA=$commit\$' /etc/alis/control-worker.env
grep -Eq '^ALIS_MARKET_DATASET_SOURCE=ssh://[^/]+/opt/alis-db-snapshot/shared/market-datasets$' /etc/alis/research-worker.env
grep -Eq '^ALIS_MARKET_DATASET_ROOT=/var/lib/alis-research-worker/market-datasets$' /etc/alis/research-worker.env
dataset_key=\$(sed -n 's/^ALIS_MARKET_DATASET_SSH_KEY=//p' /etc/alis/research-worker.env)
test -n "\$dataset_key"
sudo -u alis-research test -r "\$dataset_key"
ln -sfn '$release/backend' /opt/alis-research-worker/backend
chown -h alis-research:alis-research /opt/alis-research-worker/backend
systemctl daemon-reload
REMOTE

worker_ssh "sudo systemctl restart alis-control-worker"
worker_ssh "sudo systemctl restart alis-research-worker"
worker_ssh 'test "$(systemctl is-active alis-control-worker)" = active; test "$(systemctl is-active alis-research-worker)" = active'
worker_ssh "test \"\$(sudo readlink -f /opt/alis-research-worker/backend)\" = '$release/backend'"
worker_ssh "for unit in alis-control-worker alis-research-worker; do pid=\$(systemctl show \$unit -p MainPID --value); test \"\$(sudo readlink -f /proc/\$pid/cwd)\" = '$release/backend'; done"

release_finished=1
trap - ERR
echo "release_commit=$commit"
echo "worker_release=$release"
echo "status=healthy"
