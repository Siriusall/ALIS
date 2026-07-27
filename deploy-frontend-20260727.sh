#!/usr/bin/env bash
set -euo pipefail

current=/opt/alis/current
archive=/root/alis-frontend-20260727.tar.gz
target="$current/frontend/dist"
staging="$current/frontend/dist.new-20260727-1415"
backup="$current/frontend/dist.backup-20260727-1415"
extract_dir=/tmp/alis-frontend-20260727

test -d "$current/frontend"
test -f "$archive"
test ! -e "$staging"
test ! -e "$backup"

mkdir -p "$extract_dir"
tar -xzf "$archive" -C "$extract_dir"
test -f "$extract_dir/dist/index.html"
mv "$extract_dir/dist" "$staging"

if [ -e "$target" ]; then
  mv "$target" "$backup"
fi

if ! mv "$staging" "$target"; then
  if [ -e "$backup" ]; then
    mv "$backup" "$target"
  fi
  exit 1
fi

if ! nginx -t; then
  mv "$target" "$staging.failed"
  if [ -e "$backup" ]; then
    mv "$backup" "$target"
  fi
  exit 1
fi

find "$target" -maxdepth 2 -type f -printf '%P %s bytes\n' | sort
curl -fsS http://127.0.0.1/api/health
