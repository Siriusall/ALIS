#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'HELP'
Usage: deploy-app.sh BUNDLE

Required environment:
  ALIS_APP_SSH_TARGET, ALIS_APP_SSH_KEY
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
  set -a; source /etc/alis/alis.env; set +a
  cd $app_release/backend
  .venv/bin/python scripts/manage_migrations.py upgrade
  test \"\$(psql \"\$DATABASE_URL\" -Atc \"select version_num from alembic_version\")\" = $sim_head
  test \"\$(psql \"\$DATA_PLATFORM_DATABASE_URL\" -Atc \"select version_num from alembic_version\")\" = $data_head
  nginx -t
  sed -i \"s/^ALIS_BUILD_GIT_SHA=.*/ALIS_BUILD_GIT_SHA=$commit/\" /etc/alis/alis.env
  grep -q \"^ALIS_BUILD_GIT_SHA=$commit\\$\" /etc/alis/alis.env
  ln -sfn \"$app_release\" /opt/alis/current
  systemctl restart alis-api
  # A newly switched process intentionally reports release drift until the
  # governance cycle below registers it. Probe liveness here, then validate
  # full readiness after governance instead of creating a circular failure.
  for n in \$(seq 1 40); do curl -fsS --max-time 5 http://127.0.0.1:8000/api/live >/dev/null && exit 0; sleep 2; done; exit 1
'"

governance_payload=$(printf '%s' \
  'from app.services.system_release_center import run_release_governance_cycle,release_status; run_release_governance_cycle(actor="production_release_v2"); s=release_status(); a=s.get("active_release") or {}; c=((a.get("manifest") or {}).get("components") or {}).get("code.backend") or {}; assert s.get("ok") and not s.get("drift") and c.get("version") == "'"$commit"'", s' \
  | base64 | tr -d '\n')
app_ssh "sudo bash -lc 'set -a; source /etc/alis/alis.env; set +a; cd /opt/alis/current/backend; echo $governance_payload | base64 -d | .venv/bin/python -'"

echo "app_release=$app_release"
echo "status=healthy"
