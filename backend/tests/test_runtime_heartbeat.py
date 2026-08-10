from app.services import runtime_heartbeat
from app.services import quant_task_runner


def test_runtime_heartbeat_records_recovery_and_errors():
    runtime_heartbeat._states.clear()
    runtime_heartbeat.mark_started("worker")
    runtime_heartbeat.beat("worker", error="temporary failure")
    assert runtime_heartbeat.status()["worker"]["errors"] == 1
    assert runtime_heartbeat.status()["worker"]["alive"] is False
    runtime_heartbeat.beat("worker")
    state = runtime_heartbeat.status()["worker"]
    assert state["alive"] is True
    assert state["last_error"] == ""


def test_runtime_heartbeat_exposes_timeout():
    runtime_heartbeat._states.clear()
    runtime_heartbeat.mark_started("slow", interval_seconds=30)
    runtime_heartbeat._states["slow"]["heartbeat_at"] = "2000-01-01T00:00:00+00:00"
    state = runtime_heartbeat.status()["slow"]
    assert state["healthy"] is False
    assert state["health_reason"] == "heartbeat_timeout"


def test_auto_simulation_retry_beats_before_waiting():
    events: list[tuple[str, object]] = []

    quant_task_runner._wait_for_auto_simulation_retry(
        lambda name: events.append(("beat", name)),
        lambda seconds: events.append(("wait", seconds)),
    )

    assert events == [
        ("beat", "auto_simulation_scheduler"),
        ("wait", quant_task_runner._AUTO_SIMULATION_INTERVAL_SEC),
    ]
