#!/usr/bin/env python3
"""Cross-node ALIS chain monitor.

The monitor is independent of the browser and of a single systemd
``active`` bit. It checks API liveness/readiness, the database-local snapshot
worker's durable heartbeat/page state, and both research-worker processes.
Results are persisted so the alert worker can notify on state transitions.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE = Path(os.environ.get("ALIS_MONITOR_STATE", "/var/lib/alis-chain-monitor"))
EVENTS = STATE / "events.jsonl"


def _app_probe() -> str:
    return r'''
set -eu
if systemctl cat alis-console-snapshot-worker >/dev/null 2>&1; then
    test "$(systemctl is-active alis-console-snapshot-worker 2>/dev/null || true)" != active
fi
python3 - <<'PY'
import json, urllib.error, urllib.request

def get(path):
    req = urllib.request.Request(
        "http://127.0.0.1:8000/api/" + path,
        headers={"User-Agent": "alis-chain-monitor/2"},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {"error": str(exc)}
        return exc.code, body

checks = {}
live_status, live = get("live")
checks["live"] = {"status": live_status, "alive": bool(live.get("alive"))}
health_status, health = get("health")
dependencies = health.get("dependencies") or {}
snapshot = dependencies.get("console_snapshots") or {}
checks["health"] = {
    "status": health_status,
    "ready": bool(health.get("ready")),
    "issues": list(health.get("issues") or [])[:8],
    "snapshot": {
        "ok": bool(snapshot.get("ok")),
        "healthy": bool(snapshot.get("healthy")),
        "status": snapshot.get("status"),
        "reason": snapshot.get("reason"),
    },
}
failures = []
if live_status != 200 or not checks["live"]["alive"]:
    failures.append("api_liveness")
if health_status >= 500 or not checks["health"]["ready"]:
    failures.append("api_readiness")
if snapshot and not checks["health"]["snapshot"]["ok"]:
    failures.append("snapshot_dependency")
print(json.dumps({"checks": checks, "failures": failures}, ensure_ascii=False))
raise SystemExit(1 if failures else 0)
PY
'''


def _db_probe() -> str:
    return r'''
set -eu
# The monitor SSH key is intentionally restricted to the deployment account.
# The database worker env contains credentials and is root-readable only, so
# keep the privileged portion inside a non-interactive sudo root shell rather
# than weakening the env file permissions or copying secrets to the monitor.
sudo -n bash -s <<'ALIS_DB_PROBE'
set -eu
systemctl is-enabled --quiet alis-db-snapshot-worker
test "$(systemctl is-active alis-db-snapshot-worker)" = active
set -a
. /etc/alis/db-snapshot-worker.env
set +a
cd /opt/alis-db-snapshot/backend
PYTHONPATH=/opt/alis-db-snapshot/backend \
  /opt/alis-db-snapshot/shared/app-venv/bin/python - <<'PY'
import json
from app.services.console_snapshots import snapshot_health

value = snapshot_health()
print(json.dumps(value, ensure_ascii=False))
if not value.get("ok"):
    raise SystemExit(1)
PY
ALIS_DB_PROBE
'''


def _research_probe() -> str:
    return r'''
set -eu
current="$(sudo -n readlink -f /opt/alis-research-worker/backend)"
test -n "$current"
manifest_commit="$(sed -n 's/^git_commit=//p' "$current/.alis-release-manifest")"
test -n "$manifest_commit"
for unit in alis-control-worker alis-research-worker; do
  test "$(sudo -n systemctl is-active "$unit")" = active
  pid="$(sudo -n systemctl show "$unit" -p MainPID --value)"
  test "$pid" != 0
  test -d "/proc/$pid"
  test "$(sudo -n readlink -f "/proc/$pid/cwd")" = "$current"
  env_file="/etc/alis/${unit#alis-}.env"
  expected_commit="$(sudo -n sed -n 's/^ALIS_BUILD_GIT_SHA=//p' "$env_file")"
  test -n "$expected_commit"
  test "$expected_commit" = "$manifest_commit"
done
if sudo -n systemctl cat alis-console-snapshot-worker >/dev/null 2>&1; then
  test "$(sudo -n systemctl is-active alis-console-snapshot-worker 2>/dev/null || true)" != active
fi
'''


TARGETS = {
    "app": (
        os.environ.get("ALIS_APP_MONITOR_TARGET", "ubuntu@124.221.86.123"),
        _app_probe(),
    ),
    "db": (
        os.environ.get("ALIS_DB_MONITOR_TARGET", "ubuntu@122.51.159.195"),
        _db_probe(),
    ),
    "research": (
        os.environ.get("ALIS_RESEARCH_MONITOR_TARGET", "ubuntu@124.223.225.117"),
        _research_probe(),
    ),
}


def probe(role: str, spec: tuple[str, str]) -> dict[str, Any]:
    host, command = spec
    try:
        key = os.environ.get("ALIS_MONITOR_KEY_" + role.upper())
        known_hosts = os.path.expanduser(
            os.environ.get(
                "ALIS_MONITOR_SSH_KNOWN_HOSTS",
                "/etc/alis/chain-monitor/known_hosts",
            )
        )
        # Do not silently fall back to the service user's agent or ~/.ssh.
        # A monitor that runs with an unrelated operator key can report a
        # partial view of production, and TOFU host-key acceptance makes a
        # transient DNS/route mistake indistinguishable from the real node.
        if not key:
            return {
                "ok": False,
                "detail": f"monitor_key_missing:ALIS_MONITOR_KEY_{role.upper()}",
                "code": 78,
            }
        key = os.path.expanduser(key)
        if not os.path.isfile(key):
            return {
                "ok": False,
                "detail": f"monitor_key_unreadable:{key}",
                "code": 78,
            }
        try:
            if os.stat(key).st_mode & 0o077:
                return {
                    "ok": False,
                    "detail": f"monitor_key_permissions:{key}",
                    "code": 78,
                }
        except OSError as exc:
            return {
                "ok": False,
                "detail": f"monitor_key_stat_failed:{type(exc).__name__}",
                "code": 78,
            }
        if not os.path.isfile(known_hosts):
            return {
                "ok": False,
                "detail": f"monitor_known_hosts_missing:{known_hosts}",
                "code": 78,
            }
        args = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "ConnectionAttempts=1", "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=1", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", f"UserKnownHostsFile={known_hosts}", "-o", "ProxyCommand=none",
            "-i", key,
        ]
        result = subprocess.run(
            args + [host, command],
            text=True,
            capture_output=True,
            timeout=30,
        )
        output = (result.stdout or result.stderr or "").strip()
        return {
            "ok": result.returncode == 0,
            "detail": output[-2000:],
            "code": result.returncode,
        }
    except Exception as exc:  # noqa: BLE001 - monitor must report its own failure
        return {"ok": False, "detail": str(exc)[:1000], "code": -1}


def _signature(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Keep volatile command output out of state-transition detection."""
    return {
        role: {
            "ok": bool(value.get("ok")),
            "code": int(value.get("code") or 0),
        }
        for role, value in sorted(results.items())
    }


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    results = {role: probe(role, spec) for role, spec in TARGETS.items()}
    now = datetime.now(timezone.utc).isoformat()
    current = {
        "observed_at": now,
        "results": results,
        "signature": _signature(results),
        "healthy": all(bool(item.get("ok")) for item in results.values()),
    }
    state_file = STATE / "state.json"
    previous = _load_json(state_file)
    if not previous or previous.get("signature") != current["signature"]:
        event = {
            "observed_at": now,
            "previous_signature": (previous or {}).get("signature"),
            "signature": current["signature"],
            "healthy": current["healthy"],
            "results": results,
        }
        with EVENTS.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    temporary = state_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(state_file)
    print(json.dumps(current, ensure_ascii=False))
    return 0 if current["healthy"] else 2


if __name__ == "__main__":
    sys.exit(main())
