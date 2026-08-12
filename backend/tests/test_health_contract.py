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
