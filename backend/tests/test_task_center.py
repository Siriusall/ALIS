from datetime import datetime

from app.services import task_center


def test_task_center_unifies_modules_tasks_next_time_and_logs(monkeypatch):
    monkeypatch.setattr(task_center, "evolution_schedule_status", lambda: {
        "next_factor_cycle_at": "2026-07-27T16:00:00+08:00",
        "next_shadow_cycle_at": "2026-07-27T16:00:00+08:00",
        "next_system_cycle_at": "2026-07-27T17:00:00+08:00",
        "next_formal_cycle_at": "2026-07-31T18:00:00+08:00",
    })
    monkeypatch.setattr(task_center, "list_evolution_tasks", lambda limit=20: [{
        "id": 9, "task_type": "strategy_evolution_cycle", "status": "running", "phase": "formal_oos",
        "progress": 0.4, "message": "V5：运行窗口 2/5", "attempt": 1, "max_attempts": 3,
        "started_at": "2026-07-24T23:00:00+08:00", "heartbeat_at": "2026-07-24T23:02:00+08:00",
        "updated_at": "2026-07-24T23:02:00+08:00", "created_at": "2026-07-24T22:59:00+08:00",
        "checkpoint": {"window": 2}, "result": {},
    }])
    monkeypatch.setattr(task_center, "daily_update_status", lambda: {
        "running": False, "run_id": 88, "phase": "cooldown", "progress": 50,
        "message": "等待上游", "retry_after_seconds": 120,
        "started_at": "2026-07-24T23:01:00+08:00", "finished_at": "2026-07-24T23:01:30+08:00",
    })
    monkeypatch.setattr(task_center, "simple_health", lambda: {
        "ok": False, "level": "blocked", "summary": "目标交易日未完成",
        "target_trade_date": "20260724", "latest_decision_ready_trade_date": "20260723",
    })
    monkeypatch.setattr(task_center, "_data_runs", lambda limit: [])
    monkeypatch.setattr(task_center, "_quant_tasks", lambda limit: [])
    monkeypatch.setattr(task_center, "list_runtime_logs", lambda limit=200: [{
        "id": 1, "level": "INFO", "logger": "strategy", "message": "window completed",
        "context": {"window": 1}, "created_at": "2026-07-24T23:02:00+08:00",
    }])
    monkeypatch.setattr("app.services.system_autonomy.build_autonomy_slo", lambda: {
        "schema_version": "alis_autonomy_rolling_slo_v1", "status": "observing", "observed_sessions": 1,
    })
    monkeypatch.setattr("app.services.profit_loop.build_execution_chain_slo", lambda: {
        "schema_version": "alis_execution_chain_rolling_slo_v1", "status": "observing", "observed_due_plans": 1,
    })

    result = task_center.build_task_center(limit=20, log_limit=20)

    assert result["schema_version"] == "alis_task_center_v1"
    assert len(result["modules"]) == 10
    assert result["summary"]["running"] == 1
    strategy = next(item for item in result["modules"] if item["key"] == "strategy")
    assert strategy["status"] == "running"
    assert strategy["current_step"] == "V5：运行窗口 2/5"
    assert strategy["next_run_at"] == "2026-07-27T16:00:00+08:00"
    daily = next(item for item in result["tasks"] if item["source"] == "daily_update")
    assert daily["status"] == "waiting"
    assert daily["next_run_at"] is not None
    data_module = next(item for item in result["modules"] if item["key"] == "data")
    assert data_module["status"] in {"waiting", "blocked"}
    assert result["logs"][0]["logger"] == "strategy"
    assert result["autonomy_slo"]["status"] == "observing"
    assert result["execution_slo"]["status"] == "observing"


def test_cycle_step_uses_recorded_precise_times():
    task = {
        "id": 3, "attempt": 1, "max_attempts": 3, "started_at": "2026-07-24T17:00:00+08:00",
        "result": {"steps": [{
            "name": "factor_daily_cycle", "ok": True, "duration_seconds": 2.5,
            "started_at": "2026-07-24T17:00:05+08:00", "finished_at": "2026-07-24T17:00:07.500000+08:00",
            "result": {"status": "observing"}, "health": {"status": "observing", "reason": "waiting_samples"},
        }]},
    }
    items = task_center._normalise_cycle_steps(task, {"next_factor_cycle_at": "2026-07-25T16:00:00+08:00"})
    assert items[0]["started_at"] == "2026-07-24T17:00:05+08:00"
    assert items[0]["finished_at"] == "2026-07-24T17:00:07.500000+08:00"
    assert items[0]["time_source"] == "recorded"
    assert items[0]["status"] == "observing"


def test_latest_failure_summary_does_not_treat_superseded_failure_as_current():
    tasks = [
        {"source": "evolution", "task_type": "system_autonomy_cycle", "status": "completed"},
        {"source": "evolution", "task_type": "system_autonomy_cycle", "status": "failed"},
        {"source": "data_ingestion", "task_type": "daily", "status": "failed"},
    ]
    assert task_center._latest_failure_summary(tasks) == (1, 2)


def test_failed_waiting_and_blocked_evidence_is_not_internal_error():
    assert task_center._status_with_evidence("failed", error="上游尚未发布，冷却后复核") == "waiting"
    assert task_center._status_with_evidence("failed", error="OOS 永久硬数据未就绪，影子观察冻结") == "blocked"
    assert task_center._status_with_evidence("failed", error="deadlock detected") == "failed"


def test_activity_scope_keeps_only_latest_contract_in_current_view():
    tasks = [
        {"source": "evolution", "task_type": "factor_daily_cycle"},
        {"source": "evolution", "task_type": "factor_daily_cycle"},
        {"source": "autonomy_step", "task_type": "factor_daily_cycle", "activity_scope": "current_cycle"},
    ]
    task_center._assign_activity_scope(tasks)
    assert [item["activity_scope"] for item in tasks] == ["current", "history", "current_cycle"]


def test_evolution_runtime_tasks_expose_healthy_and_failed_components(monkeypatch):
    monkeypatch.setattr(task_center, "evolution_runtime_status", lambda: {
        "components": {
            "worker": {
                "healthy": True,
                "disabled": False,
                "heartbeat_at": "2026-07-25T10:00:00+08:00",
                "heartbeat_age_seconds": 3,
            },
            "scheduler": {
                "healthy": False,
                "disabled": False,
                "heartbeat_at": "2026-07-25T09:40:00+08:00",
                "heartbeat_age_seconds": 1203,
                "last_error": "database unavailable",
            },
        },
    })

    items = task_center._evolution_runtime_tasks()

    worker = next(item for item in items if item["task_type"] == "evolution_worker_runtime")
    scheduler = next(item for item in items if item["task_type"] == "evolution_scheduler_runtime")
    assert worker["status"] == "running"
    assert worker["message"] == "心跳正常"
    assert scheduler["status"] == "failed"
    assert scheduler["error"] == "database unavailable"
    assert scheduler["checkpoint"]["heartbeat_age_seconds"] == 1203
    assert scheduler["updated_at"] is not None
