#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
output=${1:-"$repo_root/dist/alis-release.tar.gz"}

git -C "$repo_root" diff --quiet
git -C "$repo_root" diff --cached --quiet
test -z "$(git -C "$repo_root" status --porcelain)"

commit=$(git -C "$repo_root" rev-parse HEAD)
staging=$(mktemp -d "${TMPDIR:-/tmp}/alis-release.XXXXXX")
cleanup() { rm -rf -- "$staging"; }
trap cleanup EXIT

npm --prefix "$repo_root/frontend" run build
git -C "$repo_root" archive "$commit" | tar -xf - -C "$staging"
mkdir -p "$staging/frontend/dist"
cp -a "$repo_root/frontend/dist/." "$staging/frontend/dist/"

cat > "$staging/.alis-release-manifest" <<EOF
format=alis_release_bundle_v2
git_commit=$commit
frontend_entry=frontend/dist/index.html
sim_migration_head=$(cd "$repo_root" && backend/.venv/bin/python backend/scripts/manage_migrations.py heads --database sim 2>/dev/null | sed -n 's/^Rev: \([^ ]*\).*/\1/p')
data_migration_head=$(cd "$repo_root" && backend/.venv/bin/python backend/scripts/manage_migrations.py heads --database data 2>/dev/null | sed -n 's/^Rev: \([^ ]*\).*/\1/p')
EOF

test -f "$staging/backend/app/main.py"
test -f "$staging/backend/scripts/manage_migrations.py"
test -f "$staging/frontend/dist/index.html"
test -n "$(sed -n 's/^sim_migration_head=//p' "$staging/.alis-release-manifest")"
test -n "$(sed -n 's/^data_migration_head=//p' "$staging/.alis-release-manifest")"
if find "$staging" -name '._*' -print -quit | grep -q .; then
  echo "Refusing release bundle containing AppleDouble metadata" >&2
  exit 1
fi
if command -v xattr >/dev/null 2>&1; then
  xattr -cr "$staging"
fi

mkdir -p "$(dirname "$output")"
COPYFILE_DISABLE=1 tar -czf "$output" -C "$staging" .
tar -tzf "$output" >/dev/null
tar -xOf "$output" ./.alis-release-manifest >/dev/null
tar -xOf "$output" ./frontend/dist/index.html >/dev/null
if gzip -dc "$output" | strings | grep -q 'LIBARCHIVE.xattr'; then
  echo "Refusing release bundle containing extended attributes" >&2
  exit 1
fi

echo "bundle=$output"
echo "commit=$commit"
echo "sha256=$(shasum -a 256 "$output" | awk '{print $1}')"
