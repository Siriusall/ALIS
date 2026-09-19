from app import main
import json


class _Lease:
    should_start = True

    @staticmethod
    def status():
        return {"should_start": True}


def _payload(response):
    if isinstance(response, dict):
        return response
    return json.loads(response.body.decode("utf-8"))


def _healthy_dependencies(monkeypatch):
    monkeypatch.setattr(main, "primary_database_health", lambda: {"status": "ok"})
    monkeypatch.setattr(main, "data_platform_database_health", lambda: {"status": "ok"})
    monkeypatch.setattr(main, "evolution_runtime_status", lambda: {"ok": True})
    monkeypatch.setattr(main, "_background_runtime_lease", _Lease())
    monkeypatch.setattr(main, "_startup_components", {})
    monkeypatch.setattr(main, "discover_release_change", lambda: {"restart_required": False})
    monkeypatch.setattr(
        main,
        "database_revisions",
        lambda: {"sim": main.SIM_SCHEMA_HEAD, "data": main.DATA_SCHEMA_HEAD},
    )


def test_health_is_not_ready_when_redis_is_unavailable(monkeypatch):
    _healthy_dependencies(monkeypatch)
    monkeypatch.setattr(main, "redis_health", lambda: {"available": False})
    monkeypatch.setattr(main, "runtime_heartbeat_status", lambda: {})

    result = _payload(main.health())

    assert result["status"] == "error"
    assert result["ready"] is False
    assert result["degraded"] is True


def test_health_is_not_ready_when_background_heartbeat_is_stale(monkeypatch):
    _healthy_dependencies(monkeypatch)
    monkeypatch.setattr(main, "redis_health", lambda: {"available": True})
    monkeypatch.setattr(
        main,
        "runtime_heartbeat_status",
        lambda: {"daily_update_scheduler": {"healthy": False}},
    )

    result = _payload(main.health())

    assert result["status"] == "error"
    assert result["ready"] is False
    assert "daily_update_scheduler" in result["issues"][0]


def test_health_is_not_ready_when_background_heartbeat_evidence_is_missing(monkeypatch):
    _healthy_dependencies(monkeypatch)
    monkeypatch.setattr(main, "redis_health", lambda: {"available": True})
    monkeypatch.setattr(main, "runtime_heartbeat_status", lambda: {})
    result = _payload(main.health())
    assert result["ready"] is False
    assert any("heartbeat evidence is missing" in item for item in result["issues"])


def test_health_is_not_ready_when_runtime_schema_revision_is_stale(monkeypatch):
    _healthy_dependencies(monkeypatch)
    monkeypatch.setattr(main, "redis_health", lambda: {"available": True})
    monkeypatch.setattr(main, "runtime_heartbeat_status", lambda: {"scheduler": {"healthy": True}})
    monkeypatch.setattr(
        main,
        "database_revisions",
        lambda: {"sim": main.SIM_SCHEMA_HEAD, "data": "data_0002_release_center"},
    )

    result = _payload(main.readiness())

    assert result["ready"] is False
    assert result["dependencies"]["database_schema_revisions"]["at_head"] is False
    assert any("migration revision mismatch" in item for item in result["issues"])


def test_health_keeps_current_runtime_ready_while_new_repository_is_staged(monkeypatch):
    _healthy_dependencies(monkeypatch)
    monkeypatch.setattr(main, "redis_health", lambda: {"available": True})
    monkeypatch.setattr(main, "runtime_heartbeat_status", lambda: {"scheduler": {"healthy": True}})
    monkeypatch.setattr(main, "discover_release_change", lambda: {
        "restart_required": True,
        "runtime_commit": "old", "repository_commit": "new",
    })

    result = _payload(main.readiness())

    assert result["ready"] is True
    assert result["dependencies"]["release_runtime"]["restart_required"] is True
    assert result["advisories"][0]["code"] == "deployment_pending_restart"
    assert result["advisories"][0]["blocking"] is False


def test_health_exposes_snapshot_worker_as_a_non_blocking_dependency(monkeypatch):
    from app.services import console_snapshots

    _healthy_dependencies(monkeypatch)
    monkeypatch.setattr(main, "redis_health", lambda: {"available": True})
    monkeypatch.setattr(main, "runtime_heartbeat_status", lambda: {"scheduler": {"healthy": True}})
    monkeypatch.setattr(console_snapshots, "snapshot_health", lambda: {
        "healthy": False,
        "status": "worker_unavailable",
        "reason": "snapshot_worker_heartbeat_expired",
        "pages": [],
    })

    result = _payload(main.health())

    assert result["ready"] is True
    assert result["degraded"] is True
    assert result["dependencies"]["console_snapshots"]["status"] == "worker_unavailable"
    assert any(item["code"] == "console_snapshot_worker_unhealthy" for item in result["advisories"])


def test_health_is_not_ready_when_active_release_manifest_drifts(monkeypatch):
    _healthy_dependencies(monkeypatch)
    monkeypatch.setattr(main, "redis_health", lambda: {"available": True})
    monkeypatch.setattr(main, "runtime_heartbeat_status", lambda: {"scheduler": {"healthy": True}})
    monkeypatch.setattr(main, "discover_release_change", lambda: {
        "changed": True,
        "restart_required": False,
        "active_release_id": 67,
        "active_manifest_hash": "old",
        "observed_manifest_hash": "new",
    })

    result = _payload(main.readiness())

    assert result["ready"] is False
    assert any("Active system release does not match" in item for item in result["issues"])
