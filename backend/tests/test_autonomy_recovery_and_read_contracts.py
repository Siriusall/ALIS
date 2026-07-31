from app.api.routes import research
from app.services import evolution_orchestrator, profit_loop


def test_ready_data_contract_enqueues_distinct_recovery_cycle(monkeypatch):
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "id": 99, **kwargs}

    monkeypatch.setattr(evolution_orchestrator, "create_evolution_task", fake_create)
    contract = {
        "ok": True,
        "counts": {"daily": 5518, "daily_basic": 5518, "stk_limit": 5195},
        "expected_rows": {"daily": 5534, "daily_basic": 5534, "stk_limit": 5518},
        "coverage": {"stk_limit": 0.941464},
    }

    result = evolution_orchestrator.enqueue_system_autonomy_recovery("20260724", contract)

    assert result["task_type"] == "system_autonomy_cycle"
    assert result["dedupe_key"].startswith("system_autonomy_recovery:v3:20260724:")
    assert result["payload"]["trigger"] == "decision_data_recovered"
    assert result["priority"] == 85


def test_recovery_cycle_rejects_unready_contract(monkeypatch):
    monkeypatch.setattr(
        evolution_orchestrator,
        "create_evolution_task",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not enqueue")),
    )
    assert evolution_orchestrator.enqueue_system_autonomy_recovery("20260724", {"ok": False})["reason"] == "decision_data_not_ready"


def test_latest_ready_date_is_recovered_when_prior_cycle_stopped_at_data_gate(monkeypatch):
    monkeypatch.setattr(
        "app.services.data_health.assess_latest_decision_data_health",
        lambda: {
            "decision_ready": True,
            "readiness": {"target_trade_date": "20260724", "decision_contract": {"ok": True, "counts": {"stk_limit": 5195}}},
        },
    )
    monkeypatch.setattr(evolution_orchestrator, "list_evolution_tasks", lambda limit=30: [{
        "id": 38, "task_type": "system_autonomy_cycle", "status": "completed",
        "result": {"steps": [{"name": "decision_data_gate", "result": {
            "decision_ready": False, "readiness": {"target_trade_date": "20260724"},
        }}]},
    }])
    monkeypatch.setattr(
        evolution_orchestrator,
        "enqueue_system_autonomy_recovery",
        lambda trade_date, contract, **kwargs: {
            "ok": True,
            "status": "queued",
            "trade_date": trade_date,
            "contract": contract,
            **kwargs,
        },
    )
    result = evolution_orchestrator.ensure_latest_ready_system_cycle()
    assert result["status"] == "queued"
    assert result["trade_date"] == "20260724"
    assert result["continuation_after"] is None


def test_latest_ready_date_continues_cycle_that_failed_to_persist_plan(monkeypatch):
    monkeypatch.setattr(
        "app.services.data_health.assess_latest_decision_data_health",
        lambda: {
            "decision_ready": True,
            "readiness": {
                "target_trade_date": "20260724",
                "decision_contract": {"ok": True},
            },
        },
    )
    monkeypatch.setattr(evolution_orchestrator, "list_evolution_tasks", lambda limit=200: [{
        "id": 39,
        "task_type": "system_autonomy_cycle",
        "status": "completed",
        "result": {"steps": [
            {
                "name": "decision_data_gate",
                "result": {
                    "decision_ready": True,
                    "readiness": {"target_trade_date": "20260724"},
                },
            },
            {
                "name": "profit_and_portfolio",
                "ok": False,
                "error": "invalid_authoritative_plan",
            },
        ]},
    }])
    monkeypatch.setattr(
        evolution_orchestrator,
        "enqueue_system_autonomy_recovery",
        lambda trade_date, contract, **kwargs: {
            "ok": True,
            "status": "queued",
            "trade_date": trade_date,
            **kwargs,
        },
    )

    result = evolution_orchestrator.ensure_latest_ready_system_cycle()

    assert result["continuation_after"] == 39


def test_latest_ready_date_does_not_duplicate_successful_cycle(monkeypatch):
    monkeypatch.setattr(
        "app.services.data_health.assess_latest_decision_data_health",
        lambda: {"decision_ready": True, "readiness": {"target_trade_date": "20260724"}},
    )
    monkeypatch.setattr(evolution_orchestrator, "list_evolution_tasks", lambda limit=30: [{
        "id": 44, "task_type": "system_autonomy_cycle", "status": "completed",
        "result": {"steps": [
            {"name": "decision_data_gate", "result": {
                "decision_ready": True, "readiness": {"target_trade_date": "20260724"},
            }},
            {"name": "profit_and_portfolio", "result": {"rebalance_plan": {
                "id": 9,
                "status": "planned",
                "persisted": True,
                "decision_contract_schema_version": "alis_unified_daily_decision_v1",
            }}},
        ]},
    }])
    result = evolution_orchestrator.ensure_latest_ready_system_cycle()
    assert result == {"ok": True, "status": "already_completed", "trade_date": "20260724", "task_id": 44}


def test_latest_ready_date_reuses_active_continuation(monkeypatch):
    monkeypatch.setattr(
        "app.services.data_health.assess_latest_decision_data_health",
        lambda: {
            "decision_ready": True,
            "readiness": {"target_trade_date": "20260724"},
        },
    )
    monkeypatch.setattr(evolution_orchestrator, "list_evolution_tasks", lambda limit=200: [{
        "id": 45,
        "task_type": "system_autonomy_cycle",
        "status": "queued",
        "payload": {"trade_date": "20260724"},
    }])

    result = evolution_orchestrator.ensure_latest_ready_system_cycle()

    assert result == {
        "ok": True,
        "status": "queued",
        "trade_date": "20260724",
        "task_id": 45,
    }


def test_latest_ready_date_reuses_legacy_active_daily_key_without_payload_date(monkeypatch):
    monkeypatch.setattr(
        "app.services.data_health.assess_latest_decision_data_health",
        lambda: {
            "decision_ready": True,
            "readiness": {"target_trade_date": "20260724"},
        },
    )
    monkeypatch.setattr(evolution_orchestrator, "list_evolution_tasks", lambda limit=200: [{
        "id": 48,
        "task_type": "system_autonomy_cycle",
        "dedupe_key": "system_autonomy_cycle:2026-07-24",
        "status": "running",
        "payload": {"trigger": "daily_scheduler"},
    }])

    result = evolution_orchestrator.ensure_latest_ready_system_cycle()

    assert result["status"] == "running"
    assert result["task_id"] == 48


def test_latest_ready_date_stops_after_bounded_incomplete_continuations(monkeypatch):
    monkeypatch.setattr(
        "app.services.data_health.assess_latest_decision_data_health",
        lambda: {
            "decision_ready": True,
            "readiness": {"target_trade_date": "20260724"},
        },
    )
    failed_step = {
        "name": "decision_data_gate",
        "result": {
            "decision_ready": True,
            "readiness": {"target_trade_date": "20260724"},
        },
    }
    monkeypatch.setattr(evolution_orchestrator, "list_evolution_tasks", lambda limit=200: [
        {
            "id": 47,
            "task_type": "system_autonomy_cycle",
            "status": "completed",
            "payload": {"trade_date": "20260724", "continuation_after_task_id": 46},
            "result": {"steps": [failed_step]},
        },
        {
            "id": 46,
            "task_type": "system_autonomy_cycle",
            "status": "completed",
            "payload": {"trade_date": "20260724", "continuation_after_task_id": 38},
            "result": {"steps": [failed_step]},
        },
    ])

    result = evolution_orchestrator.ensure_latest_ready_system_cycle()

    assert result["status"] == "recovery_retry_exhausted"
    assert result["last_task_id"] == 47


def test_existing_risk_breach_does_not_block_sell_reduce_only_plan():
    check = profit_loop._risk_execution_check(
        [{"code": "single_name_limit", "severity": "error"}],
        [{"code": "single_name_limit", "severity": "error"}],
        risk_reducing_only=True,
    )
    assert check["status"] == "warn"
    assert "允许降低风险" in check["detail"]


def test_risk_increasing_plan_remains_blocked():
    check = profit_loop._risk_execution_check(
        [{"code": "single_name_limit", "severity": "error"}],
        [{"code": "single_name_limit", "severity": "error"}],
        risk_reducing_only=False,
    )
    assert check["status"] == "block"


def test_plan_block_status_reports_actual_gate():
    assert profit_loop._plan_block_status({"execution_checks": [{"key": "risk_gate", "status": "block"}]})[0] == "blocked_by_risk"
    assert profit_loop._plan_block_status({"execution_checks": [{"key": "simulation_ledger", "status": "block"}]})[0] == "blocked_by_ledger"
    assert profit_loop._plan_block_status({"execution_checks": [{"key": "market_data", "status": "block"}]})[0] == "blocked_by_decision_data"


def test_profit_loop_get_is_read_only(monkeypatch):
    monkeypatch.setattr(research, "build_profit_loop", lambda *, persist: {"ok": True, "persist": persist})
    assert research.research_profit_loop() == {"ok": True, "persist": False}
