import threading

from app import evolution_worker_main
from app.services import evolution_orchestrator


def test_standalone_worker_initializes_and_starts(monkeypatch):
    calls = []
    stop_event = threading.Event()
    stop_event.set()

    monkeypatch.setattr(evolution_worker_main, "_verify_database_connections", lambda: calls.append("verify"))
    monkeypatch.setattr(evolution_worker_main, "recover_evolution_tasks", lambda: calls.append("recover") or 0)
    monkeypatch.setattr(evolution_worker_main, "start_evolution_worker", lambda: calls.append("worker") or True)

    evolution_worker_main.run_forever(stop_event)

    assert calls == ["verify", "recover", "worker"]


def test_disabled_runtime_component_is_healthy_by_delegation():
    evolution_orchestrator.mark_evolution_component_disabled("worker", reason="external_worker")

    status = evolution_orchestrator.evolution_runtime_status()

    assert status["components"]["worker"]["disabled"] is True
    assert status["components"]["worker"]["disabled_reason"] == "external_worker"
    assert status["components"]["worker"]["healthy"] is True
