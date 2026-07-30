from app import main


class _Lease:
    should_start = True

    @staticmethod
    def status():
        return {"should_start": True}


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

    result = main.health()

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

    result = main.health()

    assert result["status"] == "error"
    assert result["ready"] is False
    assert "daily_update_scheduler" in result["issues"][0]
