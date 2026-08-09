#!/usr/bin/env bash
set -euo pipefail

release_root=/opt/alis/releases
current_link=/opt/alis/current
keep_count="${ALIS_RELEASE_KEEP_COUNT:-10}"

[[ "$keep_count" =~ ^[0-9]+$ ]] && (( keep_count >= 3 ))
test -d "$release_root"
test -L "$current_link"

current_release=$(readlink -f "$current_link")
case "$current_release" in
  "$release_root"/*) ;;
  *) echo "Refusing cleanup: current release is outside $release_root" >&2; exit 1 ;;
esac

runtime_path=$(readlink -f "$current_release/backend/.venv")
case "$runtime_path" in
  "$release_root"/*)
    echo "Refusing cleanup: shared runtime is stored inside disposable releases: $runtime_path" >&2
    exit 1
    ;;
esac
test -x "$runtime_path/bin/python"

# Runtime caches must never live under a disposable release.  Cache contents
# are non-authoritative, but a missing root can still make background jobs
# fail during a release switch.  Refuse cleanup rather than silently delete
# the cache backing the active application.
runtime_env=/etc/alis/alis.env
runtime_cache_root="${ALIS_CACHE_ROOT:-}"
if [[ -z "$runtime_cache_root" && -r "$runtime_env" ]]; then
  runtime_cache_root=$(sed -n 's/^ALIS_CACHE_ROOT=//p' "$runtime_env" | tail -n 1)
fi
test -n "$runtime_cache_root"
runtime_cache_path=$(readlink -f "$runtime_cache_root")
case "$runtime_cache_path" in
  "$release_root"/*)
    echo "Refusing cleanup: shared cache is stored inside disposable releases: $runtime_cache_path" >&2
    exit 1
    ;;
esac
test -d "$runtime_cache_path"
test -w "$runtime_cache_path"
test -e "$current_release/backend/.cache"

mapfile -t releases < <(
  find "$release_root" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
    | sort -nr \
    | awk '{print $2}'
)

declare -A keep=(["$current_release"]=1)
for release in "${releases[@]:0:keep_count}"; do
  keep["$release"]=1
done

removed=0
for release in "${releases[@]}"; do
  [[ -n "${keep[$release]:-}" ]] && continue
  case "$release" in
    "$release_root"/*) ;;
    *) echo "Refusing unexpected path: $release" >&2; exit 1 ;;
  esac
  test "$release" != "$current_release"
  rm -rf -- "$release"
  removed=$((removed + 1))
done

echo "ALIS release cleanup complete: removed=$removed kept=${#keep[@]} current=$current_release"
