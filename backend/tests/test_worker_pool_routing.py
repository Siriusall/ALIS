from contextlib import contextmanager

from app.services import evolution_orchestrator as evolution


def test_default_worker_pool_routes_only_long_research_to_research_pool():
    assert evolution._default_worker_pool("strategy_environment_backtest") == "research"
    assert evolution._default_worker_pool("strategy_controller_oos") == "research"
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
