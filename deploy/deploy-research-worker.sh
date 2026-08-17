#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: deploy-research-worker.sh BUNDLE

Required environment:
  ALIS_RESEARCH_WORKER_SSH_TARGET, ALIS_RESEARCH_WORKER_SSH_KEY

Optional environment:
  ALIS_RESEARCH_WORKER_RELEASE_ROOT  Default: /opt/alis-research-worker/releases
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

sha=$(shasum -a 256 "$bundle" | awk '{print $1}')
stamp=$(date +%Y%m%d-%H%M%S)
short=${commit:0:7}
remote_archive="/tmp/alis-research-${short}.tar.gz"
release="$release_root/${stamp}-${short}"
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=10 -o ProxyCommand=none)
worker_ssh() { ssh "${ssh_opts[@]}" -i "$ALIS_RESEARCH_WORKER_SSH_KEY" "$ALIS_RESEARCH_WORKER_SSH_TARGET" "$@"; }

scp -q "${ssh_opts[@]}" -i "$ALIS_RESEARCH_WORKER_SSH_KEY" "$bundle" "$ALIS_RESEARCH_WORKER_SSH_TARGET:$remote_archive"
old_release=$(worker_ssh 'readlink -f /opt/alis-research-worker/backend 2>/dev/null || true')

worker_ssh "sudo bash -s" <<REMOTE
set -euo pipefail
sha256sum '$remote_archive' | awk '{print \$1}' | grep -Fx '$sha'
id -u alis-research >/dev/null 2>&1 || sudo useradd --system --home-dir /opt/alis-research-worker --shell /usr/sbin/nologin alis-research
sudo install -d -o alis-research -g alis-research -m 750 '$release_root' /opt/alis-research-worker/shared /opt/alis-research-worker/shared/runtime-cache /etc/alis
if [ ! -x /opt/alis-research-worker/shared/app-venv/bin/python ]; then
  sudo python3 -m venv /opt/alis-research-worker/shared/app-venv
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
