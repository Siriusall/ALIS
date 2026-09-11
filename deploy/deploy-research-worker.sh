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
remote_archive="/tmp/alis-research-${short}.tar.gz"
release="$release_root/${stamp}-${short}"
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=10 -o ProxyCommand=none)
if [[ -n ${ALIS_RESEARCH_WORKER_SSH_PROXY_COMMAND:-} ]]; then
  ssh_opts=(-o BatchMode=yes -o ConnectTimeout=10 -o "ProxyCommand=${ALIS_RESEARCH_WORKER_SSH_PROXY_COMMAND}")
fi
worker_ssh() { ssh "${ssh_opts[@]}" -i "$ALIS_RESEARCH_WORKER_SSH_KEY" "$ALIS_RESEARCH_WORKER_SSH_TARGET" "$@"; }

scp -O -q "${ssh_opts[@]}" -i "$ALIS_RESEARCH_WORKER_SSH_KEY" "$bundle" "$ALIS_RESEARCH_WORKER_SSH_TARGET:$remote_archive"
old_release=$(worker_ssh 'readlink -f /opt/alis-research-worker/backend 2>/dev/null || true')
worker_was_active=$(worker_ssh 'systemctl is-active alis-research-worker 2>/dev/null || true')
release_finished=0
restore_worker_on_error() {
  local exit_code=$?
  if (( ! release_finished )) && [[ $worker_was_active == active ]]; then
    worker_ssh 'sudo systemctl start alis-research-worker' || true
  fi
  exit "$exit_code"
}
trap restore_worker_on_error ERR
protection_code=$(printf '%s' \
  'from app.db.store import _conn; scope=_conn(); conn=scope.__enter__(); row=conn.execute("SELECT COUNT(*) AS count FROM evolution_tasks WHERE status=\x27running\x27 AND COALESCE(worker_pool,\x27control\x27)=\x27research\x27").fetchone(); print(int(dict(row).get("count") or 0)); scope.__exit__(None,None,None)' \
  | base64 | tr -d '\n')
research_running=$(worker_ssh "sudo bash -lc 'set -a; . /etc/alis/research-worker.env; set +a; cd /opt/alis-research-worker/backend; echo $protection_code | base64 -d | /opt/alis-research-worker/shared/app-venv/bin/python -'")
if [[ ! $research_running =~ ^[0-9]+$ ]]; then
  echo "research_worker_protection_probe_failed; refusing release switch" >&2
  exit 1
fi
if (( research_running > 0 )); then
  # A controlled software release has priority over resumable research work.
  # Stop the only research claimant first, then atomically return its tasks to
  # the queue without changing attempts, checkpoints, results, or governance.
  worker_ssh 'sudo systemctl stop alis-research-worker'
  worker_ssh 'test "$(systemctl is-active alis-research-worker 2>/dev/null || true)" != active'
  pause_code=$(printf '%s' \
    'from datetime import datetime; from zoneinfo import ZoneInfo; from app.db.store import _conn; now=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(); scope=_conn(); conn=scope.__enter__(); rows=conn.execute("SELECT id FROM evolution_tasks WHERE status=\x27running\x27 AND COALESCE(worker_pool,\x27control\x27)=\x27research\x27 FOR UPDATE").fetchall(); ids=[int(dict(row)["id"]) for row in rows]; [conn.execute("UPDATE evolution_tasks SET status=\x27queued\x27,phase=\x27paused_for_release\x27,message=\x27版本更新优先：研究任务已保留检查点并暂停，Worker 更新后自动恢复\x27,lease_owner=NULL,lease_expires_at=NULL,heartbeat_at=?,next_run_at=?,updated_at=? WHERE id=? AND status=\x27running\x27 AND COALESCE(worker_pool,\x27control\x27)=\x27research\x27",(now,now,now,task_id)) for task_id in ids]; scope.__exit__(None,None,None); print(len(ids))' \
    | base64 | tr -d '\n')
  paused_count=$(worker_ssh "sudo bash -lc 'set -a; . /etc/alis/research-worker.env; set +a; cd /opt/alis-research-worker/backend; echo $pause_code | base64 -d | /opt/alis-research-worker/shared/app-venv/bin/python -'")
  [[ $paused_count =~ ^[0-9]+$ ]]
  echo "research_tasks_paused_for_release=$paused_count"
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
sed -i 's/^ALIS_BUILD_GIT_SHA=.*/ALIS_BUILD_GIT_SHA=$commit/' /etc/alis/research-worker.env
grep -q '^ALIS_BUILD_GIT_SHA=$commit\$' /etc/alis/research-worker.env
grep -Eq '^ALIS_MARKET_DATASET_SOURCE=ssh://[^/]+/opt/alis-db-snapshot/shared/market-datasets$' /etc/alis/research-worker.env
grep -Eq '^ALIS_MARKET_DATASET_ROOT=/var/lib/alis-research-worker/market-datasets$' /etc/alis/research-worker.env
dataset_key=\$(sed -n 's/^ALIS_MARKET_DATASET_SSH_KEY=//p' /etc/alis/research-worker.env)
test -n "\$dataset_key"
sudo -u alis-research test -r "\$dataset_key"
ln -sfn '$release/backend' /opt/alis-research-worker/backend
chown -h alis-research:alis-research /opt/alis-research-worker/backend
systemctl daemon-reload
REMOTE

worker_ssh "sudo systemctl restart alis-research-worker"
worker_ssh 'test "$(systemctl is-active alis-research-worker)" = active'
worker_ssh 'test "$(readlink -f /opt/alis-research-worker/backend)" != ""'

release_finished=1
trap - ERR
echo "release_commit=$commit"
echo "research_worker_release=$release"
echo "status=healthy"
