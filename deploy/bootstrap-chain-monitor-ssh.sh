#!/usr/bin/env bash
set -euo pipefail

# Provision the application-host keys used by alis-chain-monitor.  This script
# is intentionally run by an operator, not by a systemd unit or a release
# bundle: it uses the operator's already-authorized deployment keys only to
# install three dedicated, restricted public keys.

usage() {
  cat <<'HELP'
Usage: bootstrap-chain-monitor-ssh.sh

Required environment:
  ALIS_APP_SSH_TARGET, ALIS_APP_SSH_KEY
  ALIS_DB_SNAPSHOT_SSH_TARGET, ALIS_DB_SNAPSHOT_SSH_KEY
  ALIS_RESEARCH_WORKER_SSH_TARGET, ALIS_RESEARCH_WORKER_SSH_KEY

Optional environment:
  ALIS_MONITOR_KNOWN_HOSTS_FILE       Verified operator known_hosts file.
                                      Default: ~/.ssh/known_hosts
  ALIS_APP_MONITOR_TARGET             Default: ubuntu@124.221.86.123
  ALIS_DB_MONITOR_TARGET              Default: ubuntu@122.51.159.195
  ALIS_RESEARCH_MONITOR_TARGET        Default: ubuntu@124.223.225.117
  ALIS_MONITOR_AUTHORIZED_FROM        Source IP/CIDR allowed for monitor keys.
                                      Default: route source detected on the app
                                      host for each target.

The supplied known_hosts file must already contain verified keys for all three
monitor targets. This script never uses ssh-keyscan or StrictHostKeyChecking=
accept-new. Private keys are generated only on the application host under
/etc/alis/chain-monitor/keys and never enter this repository or this machine.
HELP
}

[[ ${1:-} != --help ]] || { usage; exit 0; }

: "${ALIS_APP_SSH_TARGET:?required}"
: "${ALIS_APP_SSH_KEY:?required}"
: "${ALIS_DB_SNAPSHOT_SSH_TARGET:?required}"
: "${ALIS_DB_SNAPSHOT_SSH_KEY:?required}"
: "${ALIS_RESEARCH_WORKER_SSH_TARGET:?required}"
: "${ALIS_RESEARCH_WORKER_SSH_KEY:?required}"

app_monitor_target=${ALIS_APP_MONITOR_TARGET:-ubuntu@124.221.86.123}
db_monitor_target=${ALIS_DB_MONITOR_TARGET:-ubuntu@122.51.159.195}
research_monitor_target=${ALIS_RESEARCH_MONITOR_TARGET:-ubuntu@124.223.225.117}
known_hosts_source=${ALIS_MONITOR_KNOWN_HOSTS_FILE:-"${HOME}/.ssh/known_hosts"}

test -f "$known_hosts_source" || {
  echo "ALIS_MONITOR_KNOWN_HOSTS_FILE is missing: $known_hosts_source" >&2
  exit 2
}
for tool in ssh scp ssh-keygen base64 sort stat; do
  command -v "$tool" >/dev/null || {
    echo "missing required command: $tool" >&2
    exit 2
  }
done
for key in "$ALIS_APP_SSH_KEY" "$ALIS_DB_SNAPSHOT_SSH_KEY" "$ALIS_RESEARCH_WORKER_SSH_KEY"; do
  test -f "$key" || { echo "deployment SSH key is missing: $key" >&2; exit 2; }
done

monitor_user() {
  local target=$1
  [[ "$target" =~ ^[^@[:space:]]+@[^[:space:]]+$ ]] || {
    echo "monitor target must be user@host: $target" >&2
    exit 2
  }
  printf '%s' "${target%@*}"
}

monitor_host() {
  local target=$1 host
  host=${target#*@}
  host=${host#\[}
  host=${host%\]}
  printf '%s' "$host"
}

for target in "$app_monitor_target" "$db_monitor_target" "$research_monitor_target"; do
  monitor_user "$target" >/dev/null
done

authorized_from_override=${ALIS_MONITOR_AUTHORIZED_FROM:-}
if [[ -n "$authorized_from_override" ]]; then
  [[ "$authorized_from_override" =~ ^[A-Za-z0-9.:,/_*-]+$ ]] || {
    echo "ALIS_MONITOR_AUTHORIZED_FROM contains unsupported characters" >&2
    exit 2
  }
fi

work_dir=$(mktemp -d "${TMPDIR:-/tmp}/alis-chain-monitor.XXXXXX")
trap 'rm -rf "$work_dir"' EXIT
pinned_known_hosts="$work_dir/known_hosts"

extract_pinned_host() {
  local host=$1
  local extracted="$work_dir/known-hosts-$host"
  ssh-keygen -F "$host" -f "$known_hosts_source" \
    | sed '/^#/d' >"$extracted" || true
  test -s "$extracted" || {
    echo "verified known_hosts has no entry for monitor target: $host" >&2
    exit 2
  }
  cat "$extracted" >>"$pinned_known_hosts"
}

extract_pinned_host "$(monitor_host "$app_monitor_target")"
extract_pinned_host "$(monitor_host "$db_monitor_target")"
extract_pinned_host "$(monitor_host "$research_monitor_target")"
sort -u "$pinned_known_hosts" -o "$pinned_known_hosts"
chmod 600 "$pinned_known_hosts"

for target in "$app_monitor_target" "$db_monitor_target" "$research_monitor_target"; do
  ssh-keygen -F "$(monitor_host "$target")" -f "$pinned_known_hosts" >/dev/null || {
    echo "pinned known_hosts extraction failed for $target" >&2
    exit 2
  }
done

ssh_opts=(
  -F /dev/null
  -o BatchMode=yes
  -o ConnectTimeout=10
  -o ConnectionAttempts=1
  -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=$known_hosts_source"
  -o GlobalKnownHostsFile=/dev/null
  -o IdentitiesOnly=yes
  -o ProxyCommand=none
)

app_ssh() {
  ssh "${ssh_opts[@]}" -i "$ALIS_APP_SSH_KEY" "$ALIS_APP_SSH_TARGET" "$@"
}

node_ssh() {
  local target=$1 key=$2
  shift 2
  ssh "${ssh_opts[@]}" -i "$key" "$target" "$@"
}

remote_stamp="$(date +%s)-$$"
remote_known_hosts="/tmp/alis-chain-monitor-known-hosts-$remote_stamp"
remote_env="/tmp/alis-chain-monitor-env-$remote_stamp"

# Pin exactly the three keys the monitor is permitted to trust.  We do not
# copy the operator's complete known_hosts database onto production.
scp -O -q "${ssh_opts[@]}" -i "$ALIS_APP_SSH_KEY" \
  "$pinned_known_hosts" "$ALIS_APP_SSH_TARGET:$remote_known_hosts"
app_ssh "sudo bash -s -- '$remote_known_hosts'" <<'REMOTE'
set -euo pipefail
source_file=$1
install -d -o root -g root -m 700 /etc/alis/chain-monitor /etc/alis/chain-monitor/keys
install -o root -g root -m 644 "$source_file" /etc/alis/chain-monitor/known_hosts
rm -f "$source_file"
REMOTE

ensure_monitor_key() {
  local role=$1
  app_ssh "sudo bash -s -- '$role'" <<'REMOTE'
set -euo pipefail
role=$1
case "$role" in app|db|research) ;; *) exit 64 ;; esac
key="/etc/alis/chain-monitor/keys/$role"
install -d -o root -g root -m 700 /etc/alis/chain-monitor/keys
if [[ ! -f "$key" ]]; then
  umask 077
  ssh-keygen -q -t ed25519 -N '' -C "alis-chain-monitor-$role" -f "$key"
fi
if [[ ! -f "$key.pub" ]]; then
  ssh-keygen -y -f "$key" >"$key.pub"
fi
chown root:root "$key" "$key.pub"
chmod 600 "$key"
chmod 644 "$key.pub"
ssh-keygen -y -f "$key" >/dev/null
cat "$key.pub"
REMOTE
}

install_restricted_key() {
  local role=$1 monitor_target=$2 deployment_target=$3 deployment_key=$4 authorized_from=$5 public_key line encoded user
  public_key=$(ensure_monitor_key "$role")
  [[ "$public_key" =~ ^ssh-ed25519[[:space:]] ]] || {
    echo "unexpected public key generated for monitor role: $role" >&2
    exit 3
  }
  user=$(monitor_user "$monitor_target")
  line="from=\"$authorized_from\",restrict $public_key alis-chain-monitor-$role"
  encoded=$(printf '%s' "$line" | base64 | tr -d '\n')
  node_ssh "$deployment_target" "$deployment_key" "sudo bash -s -- '$encoded' '$user'" <<'REMOTE'
set -euo pipefail
encoded=$1
user=$2
home=$(getent passwd "$user" | awk -F: '{print $6}')
test -n "$home"
line=$(printf '%s' "$encoded" | base64 -d)
ssh_dir="$home/.ssh"
authorized_keys="$ssh_dir/authorized_keys"
install -d -o "$user" -g "$user" -m 700 "$ssh_dir"
touch "$authorized_keys"
chown "$user:$user" "$authorized_keys"
chmod 600 "$authorized_keys"
grep -Fqx -- "$line" "$authorized_keys" || printf '%s\n' "$line" >>"$authorized_keys"
REMOTE
}

route_source_for() {
  local target=$1 host source
  if [[ -n "$authorized_from_override" ]]; then
    printf '%s' "$authorized_from_override"
    return
  fi
  host=$(monitor_host "$target")
  source=$(app_ssh "ip -4 route get '$host' | sed -n 's/.* src \\([^ ]*\\).*/\\1/p' | head -n 1")
  [[ "$source" =~ ^[0-9.]+$ ]] || {
    echo "could not determine app-host route source for $target" >&2
    exit 2
  }
  printf '%s' "$source"
}

app_authorized_from=$(route_source_for "$app_monitor_target")
db_authorized_from=$(route_source_for "$db_monitor_target")
research_authorized_from=$(route_source_for "$research_monitor_target")
install_restricted_key app "$app_monitor_target" "$ALIS_APP_SSH_TARGET" "$ALIS_APP_SSH_KEY" "$app_authorized_from"
install_restricted_key db "$db_monitor_target" "$ALIS_DB_SNAPSHOT_SSH_TARGET" "$ALIS_DB_SNAPSHOT_SSH_KEY" "$db_authorized_from"
install_restricted_key research "$research_monitor_target" "$ALIS_RESEARCH_WORKER_SSH_TARGET" "$ALIS_RESEARCH_WORKER_SSH_KEY" "$research_authorized_from"

# This file contains targets and file paths only.  Private key material stays
# on the app host and deployment credentials never appear here.
local_env="$work_dir/chain-monitor.env"
{
  printf 'ALIS_APP_MONITOR_TARGET=%s\n' "$app_monitor_target"
  printf 'ALIS_DB_MONITOR_TARGET=%s\n' "$db_monitor_target"
  printf 'ALIS_RESEARCH_MONITOR_TARGET=%s\n' "$research_monitor_target"
  printf 'ALIS_MONITOR_KEY_APP=/etc/alis/chain-monitor/keys/app\n'
  printf 'ALIS_MONITOR_KEY_DB=/etc/alis/chain-monitor/keys/db\n'
  printf 'ALIS_MONITOR_KEY_RESEARCH=/etc/alis/chain-monitor/keys/research\n'
  printf 'ALIS_MONITOR_SSH_KNOWN_HOSTS=/etc/alis/chain-monitor/known_hosts\n'
} >"$local_env"
chmod 600 "$local_env"
scp -O -q "${ssh_opts[@]}" -i "$ALIS_APP_SSH_KEY" \
  "$local_env" "$ALIS_APP_SSH_TARGET:$remote_env"
app_ssh "sudo bash -s -- '$remote_env'" <<'REMOTE'
set -euo pipefail
source_file=$1
install -d -o root -g root -m 700 /etc/alis/chain-monitor
install -o root -g root -m 600 "$source_file" /etc/alis/chain-monitor.env
rm -f "$source_file"
REMOTE

# Verify from the same root account and pinned config that systemd will use.
app_ssh "sudo bash -s" <<'REMOTE'
set -euo pipefail
. /etc/alis/chain-monitor.env
for role in APP DB RESEARCH; do
  target_var="ALIS_${role}_MONITOR_TARGET"
  key_var="ALIS_MONITOR_KEY_${role}"
  target=${!target_var}
  key=${!key_var}
  ssh -F /dev/null -o BatchMode=yes -o ConnectTimeout=10 -o ConnectionAttempts=1 \
    -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null \
    -o "UserKnownHostsFile=$ALIS_MONITOR_SSH_KNOWN_HOSTS" -o IdentitiesOnly=yes \
    -o ProxyCommand=none -i "$key" "$target" true
done
REMOTE

echo "chain_monitor_credentials=bootstrapped"
echo "pinned_hosts=$(wc -l < "$pinned_known_hosts")"
echo "next=deploy/deploy-app.sh BUNDLE"
