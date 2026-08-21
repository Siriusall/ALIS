from datetime import datetime
import json

from app.services import task_center


def test_status_maps_timeout_and_unhealthy_to_failed():
    assert task_center._status("timed_out") == "failed"
    assert task_center._status("timeout") == "failed"
    assert task_center._status("unhealthy") == "failed"


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
    monkeypatch.setattr(task_center, "daily_update_status", lambda **_kwargs: {
        "running": False, "run_id": 88, "phase": "cooldown", "progress": 50,
        "message": "等待上游", "retry_after_seconds": 120,
        "started_at": "2026-07-24T23:01:00+08:00", "finished_at": "2026-07-24T23:01:30+08:00",
    })
    monkeypatch.setattr(task_center, "simple_health", lambda **_kwargs: {
        "ok": False, "level": "blocked", "summary": "目标交易日未完成",
        "target_trade_date": "20260724", "latest_decision_ready_trade_date": "20260723",
    })
    monkeypatch.setattr(task_center, "_data_runs", lambda limit: [])
    monkeypatch.setattr(task_center, "_quant_tasks", lambda limit: [])
    monkeypatch.setattr(
        task_center,
        "get_plan_model_data_backfill_status",
        lambda: {"status": "not_started"},
    )
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

    assert result["schema_version"] == "alis_task_center_v2"
    assert len(result["modules"]) == 10
    assert result["summary"]["running"] == 1
    strategy = next(item for item in result["modules"] if item["key"] == "strategy")
    assert strategy["status"] == "running"
    assert strategy["current_step"] == "V5：运行窗口 2/5"
    assert strategy["next_run_at"] == "2026-07-27T16:00:00+08:00"
    daily = next(item for item in result["tasks"] if item["source"] == "daily_update")
    assert daily["status"] == "blocked"
    assert daily["raw_status"] == "cooldown"
    assert daily["retrying"] is True
    assert daily["failure_category"] == "upstream_not_ready"
    assert daily["retry_after_seconds"] == 120
    assert daily["next_retry_at"] is not None
    assert daily["next_run_at"] is not None
    data_module = next(item for item in result["modules"] if item["key"] == "data")
    assert data_module["status"] in {"waiting", "blocked"}
    assert result["logs"][0]["logger"] == "strategy"
    assert result["autonomy_slo"]["status"] == "observing"
    assert result["execution_slo"]["status"] == "observing"


def test_compact_task_center_uses_bounded_data_status_and_health_reads(monkeypatch):
    calls = []

    monkeypatch.setattr(task_center, "evolution_schedule_status", lambda: {})
    monkeypatch.setattr(task_center, "list_evolution_tasks", lambda limit=20: [])
    monkeypatch.setattr(
        task_center,
        "daily_update_status",
        lambda **kwargs: calls.append(("daily", kwargs)) or {"running": False, "phase": "idle"},
    )
    monkeypatch.setattr(
        task_center,
        "simple_health",
        lambda **kwargs: calls.append(("health", kwargs)) or {"ok": False, "level": "blocked"},
    )
    monkeypatch.setattr(task_center, "get_plan_model_data_backfill_status", lambda: {"status": "not_started"})
    monkeypatch.setattr(task_center, "_data_runs", lambda limit: [])
    monkeypatch.setattr(task_center, "_quant_tasks", lambda limit: [])
    monkeypatch.setattr(task_center, "list_runtime_logs", lambda limit=200: [])

    task_center.build_task_center(
        limit=20,
        log_limit=20,
        include_slo=False,
        include_cycle_details=False,
        compact_cycle=True,
    )

    assert calls == [("daily", {"compact": True}), ("health", {"compact": True})]


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


def test_failed_retryable_data_evidence_remains_blocked_with_raw_failure_preserved():
    assert task_center._status_with_evidence("failed", error="上游尚未发布，冷却后复核") == "blocked"
    assert task_center._status_with_evidence(
        "failed",
        error="冻结执行数据未就绪",
        failure_category="execution_slice_blocked",
    ) == "blocked"
    assert task_center._status_with_evidence("failed", error="OOS 永久硬数据未就绪，影子观察冻结") == "blocked"
    assert task_center._status_with_evidence("failed", error="deadlock detected") == "failed"


def test_historical_pit_evidence_wait_is_displayed_as_blocked_research_gate():
    assert task_center._status_with_evidence(
        "retry_wait",
        phase="waiting_for_historical_pit_evidence",
    ) == "blocked"


def test_retryable_data_evidence_cannot_be_projected_as_completed():
    # A producer can finish its bookkeeping step while the source slice is
    # still absent.  The task center must fail closed rather than show green.
    for status in ("completed", "success", "ready"):
        assert task_center._status_with_evidence(
            status,
            message="执行切片未就绪",
            failure_category="execution_slice_blocked",
        ) == "blocked"

    item = task_center._normalise_cycle_steps(
        {
            "id": 12,
            "started_at": "2026-08-13T15:00:00+08:00",
            "result": {
                "steps": [{
                    "name": "hard_data_gate",
                    "ok": True,
                    "result": {
                        "status": "completed",
                        "failure_category": "execution_slice_blocked",
                    },
                    "health": {
                        "status": "completed",
                        "reason": "执行切片未就绪",
                    },
                }],
            },
        },
        {},
    )[0]
    assert item["status"] == "blocked"
    assert item["raw_status"] == "completed"

    observing_item = task_center._normalise_cycle_steps(
        {
            "id": 13,
            "started_at": "2026-08-13T15:00:00+08:00",
            "result": {
                "steps": [{
                    "name": "hard_data_gate",
                    "ok": True,
                    "result": {
                        "status": "observing",
                        "failure_category": "upstream_not_ready",
                    },
                    "health": {
                        "status": "completed",
                        "reason": "上游尚未发布完整切片",
                    },
                }],
            },
        },
        {},
    )[0]
    assert observing_item["status"] == "blocked"


def test_non_repairable_historical_observation_evidence_is_not_labeled_retrying():
    metadata = task_center._retry_metadata({
        "failure_category": "historical_observation_evidence_missing",
        "retry_after_seconds": 60,
    }, error="historical PIT observation is non_repairable")

    assert metadata["failure_category"] == "historical_observation_evidence_missing"
    assert metadata["retrying"] is False


def test_data_ingestion_retryable_failure_keeps_retry_evidence_and_blocks_module(monkeypatch):
    finished_at = "2026-08-13T15:16:00+08:00"

    class _Result:
        def fetchall(self):
            return [{
                "id": 71,
                "job_name": "data_platform.daily_update",
                "source_code": "system",
                "status": "failed",
                "started_at": "2026-08-13T15:15:00+08:00",
                "finished_at": finished_at,
                "rows_read": 3,
                "rows_written": 3,
                "error": "20260813 注册防御ETF执行切片未就绪：510300.SH:missing_open",
                "meta_json": json.dumps({
                    "trade_date": "20260813",
                    "failure_category": "execution_slice_blocked",
                    "retry_after_seconds": 60,
                }),
            }]

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, *_args, **_kwargs):
            return _Result()

    monkeypatch.setattr(task_center, "data_conn", lambda: _Conn())

    item = task_center._data_runs(1)[0]

    assert item["status"] == "blocked"
    assert item["raw_status"] == "failed"
    assert item["failure_category"] == "execution_slice_blocked"
    assert item["retry_after_seconds"] == 60
    assert item["next_retry_at"] == "2026-08-13T15:17:00+08:00"
    assert item["retrying"] is True


def test_retryable_data_block_is_counted_as_current_business_blocker():
    tasks = [{
        "source": "data_ingestion",
        "task_type": "data_platform.daily_update",
        "status": "blocked",
        "raw_status": "failed",
        "retrying": True,
        "failure_category": "upstream_not_ready",
    }]

    unresolved, historical = task_center._latest_failure_summary(tasks)
    _business, _runtime, summary = task_center._business_task_views(tasks)

    # It is not an internal code error, but it must be visible as a current
    # business block until a subsequent complete data contract supersedes it.
    assert unresolved == 0
    assert historical == 0
    assert summary["blocked"] == 1
    assert summary["active"] == 1


def test_daily_task_keeps_failed_raw_state_and_precise_retry_evidence():
    generated = datetime.fromisoformat("2026-08-13T15:20:00+08:00")
    item = task_center._daily_task({
        "running": False,
        "run_id": 98,
        "phase": "failed",
        "message": "更新失败：ETF 执行切片未就绪",
        "error": "510300.SH:missing_open",
        "failure_category": "execution_slice_blocked",
        "retry_after_seconds": 60,
        "finished_at": "2026-08-13T15:19:00+08:00",
    }, generated)

    assert item["status"] == "blocked"
    assert item["raw_status"] == "failed"
    assert item["failure_category"] == "execution_slice_blocked"
    assert item["next_retry_at"] == "2026-08-13T15:21:00+08:00"
    assert item["next_run_at"] == item["next_retry_at"]
    assert "保持数据消费阻断" in item["next_step"]


def test_historical_incompatible_controller_observation_is_not_current_incident():
    task = {
        "task_type": "controller_candidate_observation", "status": "failed",
        "attempt": 1, "max_attempts": 3,
        "phase": "historical_contract_incompatible",
        "error": "冻结策略包缺少因子绑定：momentum_60",
    }

    assert task_center._is_terminal_research_contract_failure(task) is True
    normalised = task_center._normalise_evolution(task, {})
    assert normalised["activity_scope"] == "history"


def test_historical_data_gate_wait_is_not_current_incident():
    task = {
        "task_type": "strategy_shadow_cycle", "status": "failed",
        "attempt": 3, "max_attempts": 3,
        "phase": "historical_data_gate_waiting",
        "error": "历史影子任务受当时决策/OOS 数据门阻断，保留等待证据",
    }

    assert task_center._is_terminal_research_contract_failure(task) is True
    normalised = task_center._normalise_evolution(task, {})
    assert normalised["activity_scope"] == "history"
    assert normalised["status"] == "blocked"
    assert normalised["history_reason"] == "formal_oos_or_historical_contract_wait"


def test_current_shadow_data_wait_stays_current_incident():
    task = {
        "task_type": "strategy_shadow_cycle", "status": "failed",
        "attempt": 3, "max_attempts": 3,
        "phase": "waiting_for_decision_data",
        "error": "blocked_by_decision_data: latest close incomplete",
    }

    assert task_center._is_terminal_research_contract_failure(task) is False
    assert "activity_scope" not in task_center._normalise_evolution(task, {})


def test_historical_oos_retry_wait_is_history_scoped():
    task = {
        "task_type": "strategy_evolution_cycle", "status": "retry_wait",
        "attempt": 0, "max_attempts": 3,
        "phase": "waiting_for_historical_pit_evidence",
        "message": "正式 OOS 被历史点时不可用数据阻断",
        "result": {"status": "blocked_by_oos_hard_data"},
    }

    assert task_center._is_terminal_research_contract_failure(task) is True
    normalised = task_center._normalise_evolution(task, {})
    assert normalised["activity_scope"] == "history"
    assert normalised["status"] == "blocked"


def test_historical_cycle_gate_is_not_current_data_module_blocker():
    items = task_center._normalise_cycle_steps({
        "id": 19,
        "started_at": "2026-08-16T17:00:00+08:00",
        "result": {"steps": [{
            "name": "hard_data_gate",
            "ok": False,
            "result": {
                "status": "blocked",
                "eligible_sessions": 11,
                "required_sessions": 1537,
                "non_repairable_dates": ["20260701"],
                "repair_dates": [],
            },
        }]},
    }, {})

    assert items[0]["status"] == "blocked"
    assert items[0]["progress"] == 11 / 1537
    assert items[0]["activity_scope"] == "history"
    assert items[0]["history_reason"] == "formal_oos_or_sealed_holdout_only"
    assert "不影响当前决策与隔离 Shadow" in items[0]["next_step"]


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


def test_background_runtime_tasks_expose_shared_heartbeat_contract(monkeypatch):
    monkeypatch.setattr(task_center, "runtime_heartbeat_status", lambda: {
        "redis_stream_worker": {
            "healthy": True, "heartbeat_at": "2026-07-25T10:00:00+08:00",
            "heartbeat_age_seconds": 2, "errors": 1, "started": "2026-07-25T09:00:00+08:00",
        },
        "daily_update_scheduler": {
            "healthy": False, "heartbeat_at": "2026-07-25T09:00:00+08:00",
            "heartbeat_age_seconds": 1200, "health_reason": "heartbeat_timeout", "started": "2026-07-24T09:00:00+08:00",
        },
    })
    items = task_center._background_runtime_tasks()
    worker = next(item for item in items if item["task_type"] == "redis_stream_worker_runtime")
    daily = next(item for item in items if item["task_type"] == "daily_update_scheduler_runtime")
    assert worker["status"] == "running"
    assert worker["checkpoint"]["errors"] == 1
    assert daily["status"] == "failed"
    assert daily["error"] == "heartbeat_timeout"


def test_model_data_backfill_is_visible_as_registered_checklist(monkeypatch):
    monkeypatch.setattr(
        task_center,
        "get_plan_model_data_backfill_status",
        lambda: {
            "status": "running",
            "raw_status": "running",
            "work_order_id": "work-1",
            "phase": "moneyflow",
            "progress": 0.75,
            "attempt": 1,
            "max_attempts": 5,
            "updated_at": "2026-07-29T20:13:23",
            "checklist": [{
                "label": "资金持续模型",
                "status": "running",
                "covered_raw_sessions": 1559,
                "target_raw_sessions": 1607,
            }],
            "intervention": {"current": "系统自行处理，无需人工操作"},
        },
    )

    result = task_center._model_data_backfill_task()

    assert result is not None
    assert result["task_type"] == "plan_model_data_backfill"
    assert result["status"] == "running"
    assert result["progress"] == 0.75
    assert "1,559/1,607" in result["current_step"]
    assert result["checkpoint"]["work_order_id"] == "work-1"


def test_task_center_route_degrades_when_aggregate_read_fails(monkeypatch):
    from app.api.routes import model_strategy

    def _fail(**kwargs):
        raise TimeoutError("connection timeout expired")

    monkeypatch.setattr(model_strategy, "build_task_center", _fail)

    result = model_strategy.model_strategy_task_center()

    assert result["ok"] is False
    assert result["status"] == "degraded"
    assert result["data_available"] is False
    assert result["component"] == "task_center"
    assert result["error"]["code"] == "database_query_timeout"
