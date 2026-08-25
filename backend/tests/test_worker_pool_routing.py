from contextlib import contextmanager

from app.services import evolution_orchestrator as evolution


def test_default_worker_pool_routes_only_long_research_to_research_pool():
    assert evolution._default_worker_pool("strategy_environment_backtest") == "research"
    assert evolution._default_worker_pool("strategy_controller_oos") == "research"
    assert evolution._default_worker_pool("plan_model_autonomy") == "research"
    assert evolution._default_worker_pool("research_maintenance") == "research"
    assert evolution._default_worker_pool("factor_daily_cycle") == "research"
    assert evolution._default_worker_pool("strategy_shadow_cycle") == "control"
    assert evolution._default_worker_pool("system_autonomy_cycle") == "control"
    assert evolution._default_worker_pool("system_release_governance") == "api_local"


def test_claim_query_is_scoped_to_current_worker_pool(monkeypatch):
    statements = []

    class Result:
        def fetchone(self):
            return None

    class Connection:
        _is_postgres = True

        def execute(self, sql, params=()):
            statements.append((sql, params))
            return Result()

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(evolution, "_conn", connection)
    monkeypatch.setattr(evolution, "WORKER_POOL", "research")
    monkeypatch.setattr(evolution, "_defer_long_research_claims", lambda _now: False)

    assert evolution._claim_next_task() is None
    select_sql, params = statements[0]
    assert "worker_pool=?" in select_sql
    assert params[1] == "research"


def test_schedule_status_exposes_worker_pool(monkeypatch):
    monkeypatch.setattr(evolution, "WORKER_POOL", "research")
    status = evolution.evolution_schedule_status()
    assert status["worker_pool"] == "research"
    assert status["budgets"]["worker_pools"] == ["api_local", "control", "research"]


def test_research_maintenance_is_persisted_for_research_pool(monkeypatch):
    captured = {}

    def create_task(**kwargs):
        captured.update(kwargs)
        return {"id": 812, "status": "queued"}

    monkeypatch.setattr(evolution, "create_evolution_task", create_task)

    result = evolution.enqueue_research_maintenance_task(
        "20260825", max_candidates=50,
    )

    assert result["status"] == "delegated"
    assert result["worker_pool"] == "research"
    assert captured["task_type"] == "research_maintenance"
    assert captured["worker_pool"] == "research"
    assert captured["dedupe_key"] == "research_maintenance:daily:v1:20260825"
    assert captured["payload"]["max_candidates"] == 50


def test_research_worker_executes_maintenance_task(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.services.auto_pipeline.run_daily_maintenance",
        lambda **kwargs: calls.append(kwargs) or {"ok": True, "status": "completed"},
    )
    monkeypatch.setattr(
        evolution,
        "_run_with_heartbeat_pulse",
        lambda _task_id, **kwargs: kwargs["runner"](),
    )

    result = evolution.execute_evolution_task({
        "id": 812,
        "task_type": "research_maintenance",
        "payload": {"max_candidates": 37},
    })

    assert result["ok"] is True
    assert calls == [{"max_candidates": 37}]


def test_system_autonomy_factor_entry_reuses_research_daily_task(monkeypatch):
    captured = {}

    def create_task(**kwargs):
        captured.update(kwargs)
        return {"id": 1871, "status": "completed"}

    monkeypatch.setattr(evolution, "create_evolution_task", create_task)

    result = evolution.enqueue_factor_daily_task("20260825")

    assert result["status"] == "completed"
    assert result["worker_pool"] == "research"
    assert captured["task_type"] == "factor_daily_cycle"
    assert captured["worker_pool"] == "research"
    assert captured["dedupe_key"] == "factor_daily_cycle:20260825"
    assert captured["payload"]["delegated_from"] == "system_autonomy_cycle"
