#!/usr/bin/env python3
import json, os, subprocess, time
from datetime import datetime, timezone
from pathlib import Path

STATE = Path(os.environ.get('ALIS_MONITOR_STATE', '/var/lib/alis-chain-monitor'))
EVENTS = STATE / 'events.jsonl'
TARGETS = {
    'app': ('ubuntu@124.221.86.123', "python3 - <<'PY'\nimport json,urllib.request\nchecks=['ready','live','console/snapshots','trading-ready?detail=compact']\nfail=[]\nfor path in checks:\n try:\n  r=urllib.request.urlopen('http://127.0.0.1:8000/api/'+path,timeout=12); b=r.read(); print(path,r.status,len(b))\n  if r.status>=500: fail.append(path)\n except Exception as e:\n  # protected business endpoints are still reachable if they return 401/403; transport failures are not\n  if 'HTTP Error 401' in str(e) or 'HTTP Error 403' in str(e): print(path,'protected')\n  else: fail.append(path); print(path,e)\nraise SystemExit(1 if fail else 0)\nPY"),
    'db': ('ubuntu@122.51.159.195', "systemctl is-active alis-market-dataset-publish.timer && systemctl is-active alis-db-snapshot-worker"),
    'research': ('ubuntu@124.223.225.117', "test \"$(systemctl is-active alis-research-worker)\" = active && test \"$(systemctl show alis-research-worker -p MainPID --value)\" != 0"),
}

def probe(role, spec):
    host, command = spec
    try:
        key = os.environ.get('ALIS_MONITOR_KEY_' + role.upper())
        args = ['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','-o','StrictHostKeyChecking=accept-new']
        if key: args += ['-i', key]
        p = subprocess.run(args + [host,command], text=True, capture_output=True, timeout=15)
        if p.returncode == 0:
            return {'ok': True, 'detail': p.stdout.strip()[-1000:]}
        return {'ok': False, 'detail': (p.stderr or p.stdout).strip()[-1000:], 'code': p.returncode}
    except Exception as exc:
        return {'ok': False, 'detail': str(exc)}

def main():
    STATE.mkdir(parents=True, exist_ok=True)
    results = {role: probe(role, spec) for role, spec in TARGETS.items()}
    now = datetime.now(timezone.utc).isoformat()
    current = {'observed_at': now, 'results': results}
    state_file = STATE / 'state.json'
    previous = json.loads(state_file.read_text()) if state_file.exists() else None
    if not previous or previous.get('results') != results:
        with EVENTS.open('a') as f:
            f.write(json.dumps(current, ensure_ascii=True) + '\n')
    state_file.write_text(json.dumps(current, ensure_ascii=True, indent=2))
    if not all(v['ok'] for v in results.values()):
        raise SystemExit(2)

if __name__ == '__main__':
    main()
