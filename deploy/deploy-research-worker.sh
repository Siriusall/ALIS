#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: deploy-research-worker.sh BUNDLE

Required environment:
  ALIS_RESEARCH_WORKER_SSH_TARGET, ALIS_RESEARCH_WORKER_SSH_KEY

Optional environment:
  ALIS_RESEARCH_WORKER_RELEASE_ROOT  Default: /opt/alis-research-worker/releases
  ALIS_RESEARCH_WORKER_SSH_PROXY_COMMAND  Optional SSH ProxyCommand for WireGuard-only nodes
EOF
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

scp -q "${ssh_opts[@]}" -i "$ALIS_RESEARCH_WORKER_SSH_KEY" "$bundle" "$ALIS_RESEARCH_WORKER_SSH_TARGET:$remote_archive"
old_release=$(worker_ssh 'readlink -f /opt/alis-research-worker/backend 2>/dev/null || true')
protection_code=$(printf '%s' \
  'from app.db.store import _conn; scope=_conn(); conn=scope.__enter__(); row=conn.execute("SELECT COUNT(*) AS count FROM evolution_tasks WHERE status=\x27running\x27 AND COALESCE(worker_pool,\x27control\x27)=\x27research\x27").fetchone(); print(int(dict(row).get("count") or 0)); scope.__exit__(None,None,None)' \
  | base64 | tr -d '\n')
research_running=$(worker_ssh "sudo bash -lc 'set -a; . /etc/alis/research-worker.env; set +a; cd /opt/alis-research-worker/backend; echo $protection_code | base64 -d | /opt/alis-research-worker/shared/app-venv/bin/python -'")
if [[ ! $research_running =~ ^[0-9]+$ ]]; then
  echo "research_worker_protection_probe_failed; refusing release switch" >&2
  exit 1
fi
if (( research_running > 0 )); then
  echo "research_worker_protected=$research_running; refusing release switch while research task is running" >&2
  exit 1
fi

worker_ssh "sudo bash -s" <<REMOTE
set -euo pipefail
sha256sum '$remote_archive' | awk '{print \$1}' | grep -Fx '$sha'
id -u alis-research >/dev/null 2>&1 || sudo useradd --system --home-dir /opt/alis-research-worker --shell /usr/sbin/nologin alis-research
sudo install -d -o alis-research -g alis-research -m 750 '$release_root' /opt/alis-research-worker/shared /opt/alis-research-worker/shared/runtime-cache /etc/alis
if [ ! -x /opt/alis-research-worker/shared/app-venv/bin/python ] || [ ! -x /opt/alis-research-worker/shared/app-venv/bin/pip ]; then
  sudo python3 -m venv --clear /opt/alis-research-worker/shared/app-venv
fi
sudo mkdir -p '$release_root'
sudo test ! -e '$release'
sudo install -d -o alis-research -g alis-research -m 750 '$release'
sudo tar -xzf '$remote_archive' -C '$release'
sudo chown -R alis-research:alis-research '$release'
sudo ln -sfn /opt/alis-research-worker/shared/app-venv '$release/backend/.venv'
sudo ln -sfn /opt/alis-research-worker/shared/runtime-cache '$release/backend/.cache'
sudo /opt/alis-research-worker/shared/app-venv/bin/pip install -q -r '$release/backend/requirements.txt'
sudo /opt/alis-research-worker/shared/app-venv/bin/python -m compileall -q '$release/backend/app' '$release/backend/migrations'
sudo install -o root -g root -m 644 '$release/deploy/alis-research-worker.service' /etc/systemd/system/alis-research-worker.service
sudo sed -i 's/^ALIS_BUILD_GIT_SHA=.*/ALIS_BUILD_GIT_SHA=$commit/' /etc/alis/research-worker.env
grep -q '^ALIS_BUILD_GIT_SHA=$commit$' /etc/alis/research-worker.env
sudo ln -sfn '$release/backend' /opt/alis-research-worker/backend
sudo chown -h alis-research:alis-research /opt/alis-research-worker/backend
sudo systemctl daemon-reload
REMOTE

worker_ssh "sudo systemctl restart alis-research-worker"
worker_ssh 'test "$(systemctl is-active alis-research-worker)" = active'
worker_ssh 'test "$(readlink -f /opt/alis-research-worker/backend)" != ""'

echo "release_commit=$commit"
echo "research_worker_release=$release"
echo "status=healthy"
