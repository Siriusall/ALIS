#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: deploy-production-release.sh BUNDLE

Required environment:
  ALIS_APP_SSH_TARGET, ALIS_APP_SSH_KEY
  ALIS_WORKER_SSH_TARGET, ALIS_WORKER_SSH_KEY

Optional environment:
  ALIS_PUBLIC_BASE_URL       Public URL checked after switching.
  ALIS_APP_RELEASE_ROOT      Default: /opt/alis/releases
  ALIS_WORKER_RELEASE_ROOT   Default: /opt/alis-worker/releases
EOF
}

[[ ${1:-} != "--help" ]] || { usage; exit 0; }
bundle=${1:-}
test -f "$bundle" || { usage >&2; exit 2; }
: "${ALIS_APP_SSH_TARGET:?required}"
: "${ALIS_APP_SSH_KEY:?required}"
: "${ALIS_WORKER_SSH_TARGET:?required}"
: "${ALIS_WORKER_SSH_KEY:?required}"

app_root=${ALIS_APP_RELEASE_ROOT:-/opt/alis/releases}
worker_root=${ALIS_WORKER_RELEASE_ROOT:-/opt/alis-worker/releases}
public_base=${ALIS_PUBLIC_BASE_URL:-}
manifest=$(tar -xOf "$bundle" ./.alis-release-manifest)
commit=$(sed -n 's/^git_commit=//p' <<<"$manifest")
sim_head=$(sed -n 's/^sim_migration_head=//p' <<<"$manifest")
data_head=$(sed -n 's/^data_migration_head=//p' <<<"$manifest")
test "$(sed -n 's/^format=//p' <<<"$manifest")" = alis_release_bundle_v2
[[ "$commit" =~ ^[0-9a-f]{40}$ ]]
test -n "$sim_head"
test -n "$data_head"
tar -xOf "$bundle" ./frontend/dist/index.html >/dev/null
if tar -tzf "$bundle" | grep -E '(^|/)\._' >/dev/null; then
  echo "Refusing bundle containing AppleDouble metadata" >&2
  exit 1
fi

sha=$(shasum -a 256 "$bundle" | awk '{print $1}')
stamp=$(date +%Y%m%d-%H%M%S)
short=${commit:0:7}
remote_archive="/tmp/alis-${short}.tar.gz"
app_release="$app_root/${stamp}-${short}"
worker_release="$worker_root/${stamp}-${short}"
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=10 -o ProxyCommand=none)

app_ssh() { ssh "${ssh_opts[@]}" -i "$ALIS_APP_SSH_KEY" "$ALIS_APP_SSH_TARGET" "$@"; }
worker_ssh() { ssh "${ssh_opts[@]}" -i "$ALIS_WORKER_SSH_KEY" "$ALIS_WORKER_SSH_TARGET" "$@"; }

scp -q "${ssh_opts[@]}" -i "$ALIS_APP_SSH_KEY" "$bundle" "$ALIS_APP_SSH_TARGET:$remote_archive"
scp -q "${ssh_opts[@]}" -i "$ALIS_WORKER_SSH_KEY" "$bundle" "$ALIS_WORKER_SSH_TARGET:$remote_archive"

old_app=$(app_ssh 'sudo readlink -f /opt/alis/current')
old_worker=$(worker_ssh 'readlink -f /opt/alis-worker/backend')
switched=0
rollback() {
  local exit_code=$?
  if (( switched )); then
    echo "Release validation failed; restoring previous application and worker" >&2
    worker_ssh "systemctl stop alis-evolution-worker; ln -sfn '$old_worker' /opt/alis-worker/backend; systemctl start alis-evolution-worker" || true
    app_ssh "sudo bash -lc 'ln -sfn \"$old_app\" /opt/alis/current; systemctl restart alis-api'" || true
  fi
  exit "$exit_code"
}
trap rollback ERR

app_ssh "sudo bash -lc 'set -euo pipefail
  test \"\$(sha256sum $remote_archive | awk \"{print \\\$1}\")\" = $sha
  test ! -e $app_release
  mkdir -p $app_release
  tar -xzf $remote_archive -C $app_release
  test -f $app_release/frontend/dist/index.html
  test -f $app_release/backend/scripts/manage_migrations.py
  ! find $app_release -name \"._*\" -print -quit | grep -q .
  ln -s /opt/alis/shared/app-venv $app_release/backend/.venv
  ln -s /opt/alis/shared/runtime-cache $app_release/backend/.cache
  chown -R alis:alis $app_release
  chmod 755 $app_release
  find $app_release/frontend/dist -type d -exec chmod 755 {} +
  find $app_release/frontend/dist -type f -exec chmod 644 {} +
  sudo -u www-data test -r $app_release/frontend/dist/index.html
  /opt/alis/shared/app-venv/bin/pip install -q -r $app_release/backend/requirements.txt
  /opt/alis/shared/app-venv/bin/python -m compileall -q $app_release/backend/app $app_release/backend/migrations
  sed -i \"s/^ALIS_BUILD_GIT_SHA=.*/ALIS_BUILD_GIT_SHA=$commit/\" /etc/alis/alis.env
  grep -q \"^ALIS_BUILD_GIT_SHA=$commit\$\" /etc/alis/alis.env
  set -a; source /etc/alis/alis.env; set +a
  cd $app_release/backend
  .venv/bin/python scripts/manage_migrations.py upgrade
  test \"\$(psql \"\$DATABASE_URL\" -Atc \"select version_num from alembic_version\")\" = $sim_head
  test \"\$(psql \"\$DATA_PLATFORM_DATABASE_URL\" -Atc \"select version_num from alembic_version\")\" = $data_head
  nginx -t'"

worker_ssh "bash -lc 'set -euo pipefail
  test \"\$(sha256sum $remote_archive | awk \"{print \\\$1}\")\" = $sha
  test ! -e $worker_release
  staging=\$(mktemp -d $worker_root/.deploy.XXXXXX)
  tar -xzf $remote_archive -C \"\$staging\"
  mkdir -p $worker_release
  cp -a \"\$staging/backend/.\" $worker_release/
  rm -rf -- \"\$staging\"
  venv=\$(readlink -f /opt/alis-worker/backend/.venv)
  ln -s \"\$venv\" $worker_release/.venv
  ln -s /opt/alis-worker/shared/runtime-cache $worker_release/.cache
  \"\$venv/bin/pip\" install -q -r $worker_release/requirements.txt
  \"\$venv/bin/python\" -m compileall -q $worker_release/app $worker_release/migrations
  sed -i \"s/^ALIS_BUILD_GIT_SHA=.*/ALIS_BUILD_GIT_SHA=$commit/\" /etc/alis/worker.env
  grep -q \"^ALIS_BUILD_GIT_SHA=$commit\$\" /etc/alis/worker.env'"

worker_ssh "systemctl stop alis-evolution-worker; ln -sfn '$worker_release' /opt/alis-worker/backend"
app_ssh "sudo bash -lc 'ln -sfn \"$app_release\" /opt/alis/current; systemctl restart alis-api'"
switched=1
worker_ssh 'systemctl start alis-evolution-worker'

app_ssh "sudo bash -lc 'for n in \$(seq 1 40); do curl -fsS --max-time 5 http://127.0.0.1:8000/api/ready >/dev/null && exit 0; sleep 2; done; exit 1'"
app_ssh 'sudo -u www-data test -r /opt/alis/current/frontend/dist/index.html'
worker_ssh 'test "$(systemctl is-active alis-evolution-worker)" = active'
governance_payload=$(printf '%s' \
  'from app.services.system_release_center import run_release_governance_cycle,release_status; run_release_governance_cycle(actor="production_release_v2"); s=release_status(); a=s.get("active_release") or {}; c=((a.get("manifest") or {}).get("components") or {}).get("code.backend") or {}; assert s.get("ok") and not s.get("drift") and c.get("version") == "'"$commit"'", s' \
  | base64 | tr -d '\n')
app_ssh "sudo bash -lc 'set -a; source /etc/alis/alis.env; set +a; cd /opt/alis/current/backend; echo $governance_payload | base64 -d | .venv/bin/python -'"
if [[ -n "$public_base" ]]; then
  curl -fsS --max-time 20 "$public_base/" >/dev/null
  curl -fsS --max-time 20 "$public_base/api/live" >/dev/null
  curl -fsS --max-time 20 "$public_base/api/ready" >/dev/null
fi

switched=0
trap - ERR
echo "release_commit=$commit"
echo "app_release=$app_release"
echo "worker_release=$worker_release"
echo "status=healthy"
