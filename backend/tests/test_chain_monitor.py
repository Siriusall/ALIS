from __future__ import annotations

import importlib.util
import os
from types import SimpleNamespace
from pathlib import Path


def _monitor_module():
    path = Path(__file__).resolve().parents[2] / "deploy" / "alis-chain-monitor.py"
    spec = importlib.util.spec_from_file_location("alis_chain_monitor_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chain_monitor_signature_ignores_volatile_probe_detail() -> None:
    monitor = _monitor_module()
    first = {
        "app": {"ok": True, "code": 0, "detail": "observed_at=one"},
        "db": {"ok": True, "code": 0, "detail": "heartbeat=one"},
    }
    second = {
        "app": {"ok": True, "code": 0, "detail": "observed_at=two"},
        "db": {"ok": True, "code": 0, "detail": "heartbeat=two"},
    }

    assert monitor._signature(first) == monitor._signature(second)


def test_chain_monitor_probes_the_snapshot_and_both_research_workers() -> None:
    monitor = _monitor_module()

    app_command = monitor.TARGETS["app"][1]
    db_command = monitor.TARGETS["db"][1]
    research_command = monitor.TARGETS["research"][1]

    assert "python3 - <<'PY'" in app_command
    assert "console_snapshots" in app_command
    assert "alis-db-snapshot-worker" in db_command
    assert "snapshot_health" in db_command
    assert "alis-control-worker" in research_command
    assert "alis-research-worker" in research_command


def test_chain_monitor_emits_only_on_meaningful_transition(monkeypatch, tmp_path: Path) -> None:
    monitor = _monitor_module()
    monkeypatch.setattr(monitor, "STATE", tmp_path)
    monkeypatch.setattr(monitor, "EVENTS", tmp_path / "events.jsonl")
    calls = {"count": 0}

    def probe(_role, _spec):
        calls["count"] += 1
        return {"ok": True, "code": 0, "detail": f"volatile-{calls['count']}"}

    monkeypatch.setattr(monitor, "probe", probe)
    monkeypatch.setattr(monitor, "TARGETS", {"app": ("test", "true")})

    assert monitor.main() == 0
    assert monitor.main() == 0

    events = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(events) == 1


def test_chain_monitor_requires_explicit_key_and_pinned_hosts(monkeypatch, tmp_path: Path) -> None:
    monitor = _monitor_module()

    monkeypatch.delenv("ALIS_MONITOR_KEY_APP", raising=False)
    missing = monitor.probe("app", ("ubuntu@example", "true"))
    assert missing == {
        "ok": False,
        "detail": "monitor_key_missing:ALIS_MONITOR_KEY_APP",
        "code": 78,
    }

    key = tmp_path / "monitor-key"
    key.write_text("not-a-real-key", encoding="utf-8")
    os.chmod(key, 0o600)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("example ssh-ed25519 AAAA\n", encoding="utf-8")
    monkeypatch.setenv("ALIS_MONITOR_KEY_APP", str(key))
    monkeypatch.setenv("ALIS_MONITOR_SSH_KNOWN_HOSTS", str(known_hosts))
    original_stat = os.stat
    monkeypatch.setattr(
        monitor.os,
        "stat",
        lambda path, *args, **kwargs: SimpleNamespace(st_mode=0o100600)
        if str(path) == str(key)
        else original_stat(path, *args, **kwargs),
    )
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def fake_run(args, **_kwargs):
        calls.append(args)
        return Result()

    monkeypatch.setattr(monitor.subprocess, "run", fake_run)
    result = monitor.probe("app", ("ubuntu@example", "true"))

    assert result["ok"] is True
    assert calls
    assert "-o" in calls[0]
    assert "IdentitiesOnly=yes" in calls[0]
    assert "StrictHostKeyChecking=yes" in calls[0]
    assert f"UserKnownHostsFile={known_hosts}" in calls[0]
