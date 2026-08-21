from contextlib import contextmanager
from datetime import datetime, timedelta
import time
import pytest

from app.services import evolution_orchestrator as evolution


def test_long_research_claims_are_deferred_after_trade_day_cutoff(monkeypatch):
    monkeypatch.setattr(evolution, "RESEARCH_CUTOFF_HOUR", 10)
    monkeypatch.setattr(evolution, "RESEARCH_CUTOFF_MINUTE", 30)
    monkeypatch.setattr(evolution, "RESEARCH_RESUME_HOUR", 16)
    monkeypatch.setattr(evolution, "RESEARCH_RESUME_MINUTE", 30)
    monkeypatch.setattr(evolution, "_is_scheduled_trade_day", lambda _value: True)

    assert evolution._defer_long_research_claims(datetime(2026, 8, 12, 10, 29)) is False
    assert evolution._defer_long_research_claims(datetime(2026, 8, 12, 10, 30)) is True
    assert evolution._defer_long_research_claims(datetime(2026, 8, 12, 16, 29)) is True
    assert evolution._defer_long_research_claims(datetime(2026, 8, 12, 16, 30)) is False


def test_long_research_claims_continue_on_non_trade_days(monkeypatch):
    monkeypatch.setattr(evolution, "_is_scheduled_trade_day", lambda _value: False)

    assert evolution._defer_long_research_claims(datetime(2026, 8, 15, 18, 0)) is False


def test_superseded_task_reconciliation_reads_autocommit_then_writes(monkeypatch):
    """Broad reconciliation scans must not hold a writable transaction open."""

    events = []

    class Result:
        rowcount = 1

        def __init__(self, rows=()):
            self.rows = list(rows)

        def fetchall(self):
            return self.rows

    queued = {
        "id": 11,
        "task_type": "strategy_evolution_cycle",
        "status": "queued",
        "payload_json": '{"trigger":"weekly_scheduler"}',
    }

    class ReadConnection:
        def execute(self, sql, params=()):  # noqa: ANN001
            events.append(("read", "execute", str(sql)))
            if "LIMIT ?" in str(sql):
                return Result([queued])
            return Result([queued])

    class WriteConnection:
        def execute(self, sql, params=()):  # noqa: ANN001
            events.append(("write", "execute", str(sql)))
            assert str(sql).lstrip().upper().startswith("UPDATE")
            return Result()

    @contextmanager
    def read_connection():
        events.append(("read", "enter"))
        yield ReadConnection()
        events.append(("read", "exit"))

    @contextmanager
    def write_connection():
        events.append(("write", "enter"))
        yield WriteConnection()
        events.append(("write", "exit"))

    monkeypatch.setattr(evolution, "_read_conn", read_connection)
    monkeypatch.setattr(evolution, "_conn", write_connection)
    monkeypatch.setattr(evolution, "V10_RESEARCH_ENABLED", True)

    result = evolution.reconcile_superseded_evolution_tasks(limit=10)

    assert result["count"] == 1
    assert result["quarantined"] == [
        {
            "id": 11,
            "reason": "旧 weekly_scheduler 合同已由 V10 事件驱动研究替代，保留证据但禁止执行",
        }
    ]
    # Both SELECTs finish and the read-only connection closes before the
    # writable transaction is opened for the conditional UPDATE.
    assert [item[:2] for item in events] == [
        ("read", "enter"),
        ("read", "execute"),
        ("read", "execute"),
        ("read", "exit"),
        ("write", "enter"),
        ("write", "execute"),
        ("write", "exit"),
    ]


def test_checkpoint_recovery_claim_does_not_consume_failure_attempt(monkeypatch):
    statements = []

    class Result:
        rowcount = 1

        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        _is_postgres = True

        def execute(self, sql, params=()):
            statements.append(sql)
            if "SELECT * FROM evolution_tasks" in sql:
                return Result({"id": 407, "phase": "recovered"})
            return Result()

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(evolution, "_conn", connection)
    monkeypatch.setattr(evolution, "get_evolution_task", lambda task_id: {"id": task_id})
    monkeypatch.setattr(evolution, "_defer_long_research_claims", lambda _now: False)

    assert evolution._claim_next_task() == {"id": 407}
    claim_sql = next(sql for sql in statements if "UPDATE evolution_tasks" in sql)
    assert "CASE WHEN phase='recovered' THEN 0 ELSE 1 END" in claim_sql


@pytest.fixture(autouse=True)
def _bind_test_factor_contract(monkeypatch):
    """Replay is stubbed here; satisfy the production factor gate explicitly."""
    monkeypatch.setattr(evolution, "_strategy_cycle_factor_contract_issues", lambda _targets: [])


def test_long_task_pulse_renews_lease_without_rewriting_checkpoint(monkeypatch):
    updates = []
    runtime_beats = []
    monkeypatch.setattr(evolution, "HEARTBEAT_PULSE_SECONDS", 0.01)
    monkeypatch.setattr(evolution, "_runtime_heartbeat", lambda component: runtime_beats.append(component))
    monkeypatch.setattr(evolution, "_ensure_task_active", lambda _task_id: None)
    monkeypatch.setattr(evolution, "get_evolution_task", lambda _task_id: {
        "phase": "formal_oos",
        "message": "窗口重放",
        "checkpoint": {"windows": [{"index": 1}]},
    })
    monkeypatch.setattr(
        evolution,
        "_update_active_task",
        lambda task_id, **fields: updates.append({"task_id": task_id, **fields}) or True,
    )

    result = evolution._run_with_heartbeat_pulse(
        9,
        phase="formal_oos",
        message="窗口重放",
        runner=lambda: (time.sleep(0.04), {"ok": True})[1],
    )

    assert result == {"ok": True}
    assert runtime_beats
    assert updates
    assert all("checkpoint" not in item and "progress" not in item for item in updates)
    assert all(item["expected_lease_owner"] == evolution.WORKER_ID for item in updates)


def test_shadow_daily_dispatch_is_idempotent_by_date_and_current_data_contract(monkeypatch):
    calls = []
    monkeypatch.setattr(
        evolution,
        "create_evolution_task",
        lambda **kwargs: calls.append(kwargs) or {"id": 91, "status": "queued", "phase": "queued", "progress": 0},
    )
    result = evolution.enqueue_strategy_shadow_daily_task("2026-08-10")

    assert result["execution_mode"] == "persistent_incremental_task"
    assert calls[0]["task_type"] == "strategy_shadow_cycle"
    assert calls[0]["payload"]["max_new_sessions"] == 1
    assert calls[0]["payload"]["data_contract"] == "current_experiment_gate_and_candidate_frozen_formal_evidence_v1"
    assert calls[0]["payload"]["observation_contract"] == "alis_strategy_shadow_incremental_task_v5"
    assert calls[0]["dedupe_key"] == "strategy_shadow_daily:v5:20260810"


def test_plan_model_autonomy_dispatches_to_research_worker(monkeypatch):
    calls = []
    monkeypatch.setattr(
        evolution,
        "create_evolution_task",
        lambda **kwargs: calls.append(kwargs) or {
            "id": 92,
            "status": "queued",
            "phase": "queued",
            "progress": 0,
            "message": "等待研究 Worker",
            "worker_pool": kwargs.get("worker_pool"),
        },
    )

    result = evolution.enqueue_plan_model_autonomy_task("2026-08-10")

    assert result["ok"] is True
    assert result["status"] == "delegated"
    assert result["execution_mode"] == "persistent_research_task"
    assert result["worker_pool"] == "research"
    assert result["task_id"] == 92
    assert calls[0]["task_type"] == "plan_model_autonomy"
    assert calls[0]["worker_pool"] == "research"
    assert calls[0]["dedupe_key"] == "plan_model_autonomy:daily:v1:20260810"
    assert calls[0]["payload"]["production_change_forbidden"] is True


def test_explicit_plan_model_training_dispatches_to_research_worker(monkeypatch):
    calls = []
    monkeypatch.setattr(
        evolution,
        "create_evolution_task",
        lambda **kwargs: calls.append(kwargs) or {
            "id": 93,
            "status": "queued",
            "phase": "queued",
            "progress": 0,
            "message": "等待研究 Worker",
            "worker_pool": kwargs.get("worker_pool"),
        },
    )
    monkeypatch.setattr(evolution, "_now", lambda: datetime(2026, 8, 10, 17, 0))

    result = evolution.enqueue_plan_model_training_task(
        "medium_horizon_lambdarank_v1"
    )

    assert result["status"] == "delegated"
    assert result["mode"] == "single_model_training"
    assert result["worker_pool"] == "research"
    assert calls[0]["task_type"] == "plan_model_autonomy"
    assert calls[0]["worker_pool"] == "research"
    assert calls[0]["dedupe_key"] == (
        "plan_model_training:single:v1:medium_horizon_lambdarank_v1:2026-08-10"
    )
    assert calls[0]["payload"]["target_model_key"] == "medium_horizon_lambdarank_v1"
    assert calls[0]["payload"]["production_change_forbidden"] is True


def test_all_plan_model_training_dispatches_to_research_worker(monkeypatch):
    calls = []
    monkeypatch.setattr(
        evolution,
        "create_evolution_task",
        lambda **kwargs: calls.append(kwargs) or {
            "id": 94,
            "status": "queued",
            "phase": "queued",
            "progress": 0,
            "message": "等待研究 Worker",
            "worker_pool": kwargs.get("worker_pool"),
        },
    )
    monkeypatch.setattr(evolution, "_now", lambda: datetime(2026, 8, 10, 17, 0))

    result = evolution.enqueue_all_plan_model_training_task()

    assert result["status"] == "delegated"
    assert result["mode"] == "all_trainable_models"
    assert result["worker_pool"] == "research"
    assert calls[0]["task_type"] == "plan_model_autonomy"
    assert calls[0]["worker_pool"] == "research"
    assert calls[0]["dedupe_key"] == "plan_model_training:all:v1:2026-08-10"
    assert calls[0]["payload"]["train_all"] is True
    assert calls[0]["payload"]["production_change_forbidden"] is True


def test_plan_model_autonomy_holds_worker_until_training_is_terminal(monkeypatch):
    from app.db import store
    from app.services import plan_model_auto_optimizer

    statuses = iter([
        {
            "id": 65,
            "status": "running",
            "phase": "training",
            "progress": 0.55,
            "message": "training",
            "metrics": {"model_key": "medium_horizon_lambdarank_v1"},
        },
        {
            "id": 65,
            "status": "completed",
            "phase": "completed",
            "progress": 1.0,
            "message": "completed",
            "model_version_id": 95,
            "metrics": {"model_key": "medium_horizon_lambdarank_v1"},
        },
    ])
    heartbeats = []
    monkeypatch.setattr(
        plan_model_auto_optimizer,
        "run_auto_optimization_once",
        lambda force=False: {
            "ok": True,
            "started": [{"run_id": 65, "started": True}],
        },
    )
    monkeypatch.setattr(store, "get_model_train_run", lambda _run_id: next(statuses))
    monkeypatch.setattr(evolution, "_heartbeat", lambda task_id, **values: heartbeats.append((task_id, values)))
    monkeypatch.setattr(evolution.time, "sleep", lambda _seconds: None)

    result = evolution.execute_evolution_task({
        "id": 947,
        "task_type": "plan_model_autonomy",
        "payload": {},
        "checkpoint": {},
    })

    assert result["resource_lock_contract"] == "single_worker_held_until_model_train_terminal_v1"
    assert result["completed_training_runs"] == [{
        "run_id": 65,
        "status": "completed",
        "model_version_id": 95,
        "error": None,
    }]
    assert any("单模型资源锁定中" in values["message"] for _, values in heartbeats)


def test_plan_model_autonomy_dispatches_frozen_targeted_candidate(monkeypatch):
    from app.db import store
    from app.services import plan_model_control

    requested = []
    monkeypatch.setattr(
        plan_model_control,
        "request_plan_model_training",
        lambda model_key, *, training_context: requested.append({
            "model_key": model_key,
            "training_context": training_context,
        }) or {"ok": True, "started": True, "run_id": 77},
    )
    monkeypatch.setattr(store, "get_model_train_run", lambda _run_id: {
        "id": 77,
        "status": "completed",
        "model_version_id": 273,
        "metrics": {"model_key": "event_impact_assessor_v2"},
    })
    monkeypatch.setattr(evolution, "_heartbeat", lambda *_args, **_kwargs: None)

    result = evolution.execute_evolution_task({
        "id": 948,
        "task_type": "plan_model_autonomy",
        "payload": {
            "trigger": "event_head_feature_subsets_v1",
            "target_model_key": "event_impact_assessor_v2",
            "source_model_version_id": 272,
            "source_audit_contract": "audit-v1",
            "model_candidate_spec": {
                "event_head_feature_subset_contract": "subsets-v1",
                "event_head_feature_subsets": {"positive_impact": ["a"]},
            },
        },
        "checkpoint": {},
    })

    assert result["mode"] == "targeted_model_candidate"
    assert result["completed_training_runs"][0]["model_version_id"] == 273
    assert requested[0]["model_key"] == "event_impact_assessor_v2"
    context = requested[0]["training_context"]
    assert context["source_model_version_id"] == 272
    assert context["production_change_forbidden"] is True
    assert context["model_candidate_spec"][
        "event_head_feature_subset_contract"
    ] == "subsets-v1"


def test_plan_model_autonomy_dispatches_explicit_single_model_training(monkeypatch):
    from app.services import plan_model_control

    requested = []
    monkeypatch.setattr(
        plan_model_control,
        "request_plan_model_training",
        lambda model_key, *, training_context: requested.append((model_key, training_context))
        or {"ok": True, "started": True, "run_id": 78, "model_key": model_key},
    )
    monkeypatch.setattr(
        evolution,
        "_wait_for_model_train_runs",
        lambda _task_id, run_ids, *, phase_prefix: [
            {"run_id": run_id, "status": "completed", "model_version_id": 274, "error": None}
            for run_id in run_ids
        ],
    )
    monkeypatch.setattr(evolution, "_heartbeat", lambda *_args, **_kwargs: None)

    result = evolution.execute_evolution_task({
        "id": 950,
        "task_type": "plan_model_autonomy",
        "payload": {
            "trigger": "api_plan_model_train",
            "target_model_key": "medium_horizon_lambdarank_v1",
            "production_change_forbidden": True,
        },
        "checkpoint": {},
    })

    assert result["mode"] == "targeted_model_training"
    assert result["completed_training_runs"] == [{
        "run_id": 78,
        "status": "completed",
        "model_version_id": 274,
        "error": None,
    }]
    assert requested[0][0] == "medium_horizon_lambdarank_v1"
    assert requested[0][1]["requested_by"] == "evolution_explicit_plan_model_training"
    assert requested[0][1]["production_change_forbidden"] is True


def test_plan_model_autonomy_serializes_all_trainable_model_runs(monkeypatch):
    from app.services import plan_model_control

    events = []
    monkeypatch.setattr(
        plan_model_control,
        "model_family_status",
        lambda: {"items": [
            {"model_key": "medium_horizon_lambdarank_v1", "status": "trainable"},
            {"model_key": "leader_lambdarank_v1", "status": "candidate"},
            {"model_key": "capital_persistence_multitask_v1", "status": "trainable"},
        ]},
    )

    def request(model_key, *, training_context):
        run_id = 81 if model_key == "medium_horizon_lambdarank_v1" else 82
        events.append(("request", model_key, training_context))
        return {"ok": True, "started": True, "run_id": run_id, "model_key": model_key}

    def wait(_task_id, run_ids, *, phase_prefix):
        events.append(("wait", list(run_ids), phase_prefix))
        return [
            {"run_id": run_id, "status": "completed", "model_version_id": run_id + 200, "error": None}
            for run_id in run_ids
        ]

    monkeypatch.setattr(plan_model_control, "request_plan_model_training", request)
    monkeypatch.setattr(evolution, "_wait_for_model_train_runs", wait)
    monkeypatch.setattr(evolution, "_heartbeat", lambda *_args, **_kwargs: None)

    result = evolution.execute_evolution_task({
        "id": 951,
        "task_type": "plan_model_autonomy",
        "payload": {"trigger": "api_plan_model_train_all", "train_all": True},
        "checkpoint": {},
    })

    assert result["mode"] == "all_trainable_models"
    assert result["started_count"] == 2
    assert result["skipped"] == [{
        "model_key": "leader_lambdarank_v1", "reason": "status_candidate"
    }]
    # The first run reaches its terminal state before the second starts; no
    # redundant second wait is issued after those terminal observations.
    assert [event[:2] for event in events] == [
        ("request", "medium_horizon_lambdarank_v1"),
        ("wait", [81]),
        ("request", "capital_persistence_multitask_v1"),
        ("wait", [82]),
    ]
    assert result["completed_training_runs"] == [
        {"run_id": 81, "status": "completed", "model_version_id": 281, "error": None},
        {"run_id": 82, "status": "completed", "model_version_id": 282, "error": None},
    ]
    assert all(
        event[2]["production_change_forbidden"] is True
        for event in events
        if event[0] == "request"
    )


def test_hsmm_shadow_training_runs_inside_persistent_evolution_task(monkeypatch):
    from app.services import hsmm_shadow

    captured = {}
    monkeypatch.setattr(
        hsmm_shadow,
        "train_hsmm_shadow",
        lambda **kwargs: captured.update(kwargs) or {
            "ok": True,
            "status": "shadow_failed",
            "model_version_id": 263,
        },
    )
    monkeypatch.setattr(evolution, "_heartbeat", lambda *_args, **_kwargs: None)

    result = evolution.execute_evolution_task({
        "id": 949,
        "task_type": "hsmm_shadow_training",
        "payload": {"refresh_api": False, "force": False},
        "checkpoint": {},
    })

    assert result["model_version_id"] == 263
    assert captured == {"refresh_api": False, "force": False}


def test_formal_trial_count_is_scoped_to_same_lineage_cohort():
    candidate = {
        "config": {
            "champion_reference": "champion-1",
            "lineage": {
                "root_model_version_id": 50,
                "hypothesis_family": "factor_combination_distillation",
            },
        },
    }
    versions = [
        {
            "id": 1,
            "config": candidate["config"],
            "metrics": {"formal_oos": {"status": "passed"}},
        },
        {
            "id": 2,
            "config": {
                "champion_reference": "champion-1",
                "lineage": {
                    "root_model_version_id": 50,
                    "hypothesis_family": "unrelated_family",
                },
            },
            "metrics": {"formal_oos": {"status": "failed"}},
        },
        {"id": 3, "config": candidate["config"], "metrics": {}},
    ]

    cohort = evolution._formal_trial_cohort(candidate, versions)

    assert cohort["trial_count"] == 1
    assert cohort["model_version_ids"] == [1]


def test_strategy_diagnosis_prioritizes_market_exposure():
    result = evolution.diagnose_strategy_results([
        {
            "role": "challenger", "summary": {"aggregate_cost_after_return": .02, "aggregate_benchmark_return": .42},
            "failure_attribution": {"primary_cause": "market_exposure", "confidence": "high"},
        },
        {"role": "baseline", "summary": {"aggregate_cost_after_return": .01}},
    ])
    assert result["primary_gap"] == "market_exposure"
    assert result["recommended_next_candidate"] == "dynamic_market_exposure_with_etf_fallback_v1"


def test_strategy_diagnosis_does_not_infer_market_exposure_from_benchmark_gap_alone():
    result = evolution.diagnose_strategy_results([
        {"role": "challenger", "summary": {"aggregate_cost_after_return": .02, "aggregate_benchmark_return": .42}},
    ])
    assert result["primary_gap"] == "unknown"
    assert result["recommended_next_candidate"] == "no_automatic_candidate_without_replay_evidence"


def test_strategy_diagnosis_routes_selection_allocation_residual_to_research_backed_candidate():
    result = evolution.diagnose_strategy_results([
        {
            "role": "challenger", "summary": {"aggregate_cost_after_return": .02},
            "failure_attribution": {"primary_cause": "selection_or_allocation_residual", "confidence": "high"},
        },
    ])
    assert result["primary_gap"] == "selection_or_allocation_residual"
    assert result["recommended_next_candidate"] == "china_value_lowvol_reversal_rank_buffer_candidate_v1"


def test_strategy_diagnosis_keeps_every_challenger_and_selects_explicitly():
    result = evolution.diagnose_strategy_results([
        {
            "role": "challenger", "model_version_id": 51, "strategy_id": 2, "name": "Costs",
            "summary": {"aggregate_cost_after_return": -.1, "aggregate_benchmark_return": .2},
            "failure_attribution": {"primary_cause": "transaction_costs", "confidence": "medium"},
        },
        {
            "role": "challenger", "model_version_id": 52, "strategy_id": 3, "name": "Exposure",
            "summary": {"aggregate_cost_after_return": .01, "aggregate_benchmark_return": .2},
            "failure_attribution": {"primary_cause": "market_exposure", "confidence": "high"},
        },
    ])
    assert result["schema_version"] == "alis_strategy_diagnosis_v2"
    assert [item["model_version_id"] for item in result["challenger_diagnoses"]] == [51, 52]
    assert result["selected_model_version_id"] == 52
    assert result["primary_gap"] == "market_exposure"


def test_formal_reference_contract_freezes_same_run_windows_and_cost_protocol():
    contract = evolution._formal_reference_contract(
        {
            "schema_version": "alis_purged_walk_forward_oos_v1",
            "production_strategy_version_id": 50,
            "production_strategy_version": "champion-v50",
            "strategy_controller_version": "controller-v1",
            "protocol": {"minimum_windows": 5, "cost_policy": "fees and slippage"},
            "windows": [
                {"index": 1, "test": {"start": "2025-01-01", "end": "2025-06-30", "sessions": 126}},
                {"index": 2, "test": {"start": "2025-07-01", "end": "2025-12-31", "sessions": 126}},
            ],
        },
        task_id=29,
        window_start="20200101",
        window_end="20260731",
    )
    assert contract["cycle_task_id"] == 29
    assert contract["protocol"]["cost_policy"] == "fees and slippage"
    assert contract["test_windows"] == [
        {"index": 1, "start": "2025-01-01", "end": "2025-06-30", "sessions": 126},
        {"index": 2, "start": "2025-07-01", "end": "2025-12-31", "sessions": 126},
    ]


def test_strategy_targets_routes_persisted_selection_residual_without_exposure_candidate(monkeypatch):
    monkeypatch.setattr(evolution, "audit_oos_hard_data", lambda **_kwargs: {"window_end": "20260731"})
    monkeypatch.setattr(evolution, "resolve_production_strategy_champion", lambda: {
        "strategy_id": 1, "champion_id": 10, "strategy": {"name": "Champion"},
    })
    versions = [{
        "id": 43,
        "config": {"strategy_id": 2},
        "validation": {"status": "formal_oos_failed"},
        "metrics": {"formal_oos": {"failure_attribution": {
            "primary_cause": "selection_or_allocation_residual", "confidence": "medium",
        }}},
    }]
    monkeypatch.setattr(evolution, "list_model_versions", lambda model_key, limit: versions if model_key == "alis_strategy_challenger" else [])
    monkeypatch.setattr(
        "app.services.strategy_candidate_generator.ensure_autonomous_strategy_candidates",
        lambda: {"generated": []},
    )
    monkeypatch.setattr(
        "app.services.strategy_candidate_generator.ensure_dynamic_exposure_challenger",
        lambda: (_ for _ in ()).throw(AssertionError("must not generate an exposure candidate")),
    )
    generated = {}
    monkeypatch.setattr(
        "app.services.strategy_candidate_generator.ensure_selection_allocation_challenger",
        lambda **kwargs: generated.update(kwargs) or None,
    )

    targets = evolution._strategy_targets()

    assert [item["strategy_id"] for item in targets] == [1]
    assert generated["source_model_version_id"] == 43
    assert generated["failure_attribution"]["primary_cause"] == "selection_or_allocation_residual"


def test_latest_failure_attribution_uses_newest_persisted_version(monkeypatch):
    monkeypatch.setattr(evolution, "list_model_versions", lambda *_args, **_kwargs: [
        {
            "id": 52, "version": "new", "config": {"strategy_id": 12},
            "metrics": {"formal_oos": {"failure_attribution": {
                "primary_cause": "selection_or_allocation_residual", "confidence": "high",
            }}},
        },
        {
            "id": 43, "version": "old", "config": {"strategy_id": 2},
            "metrics": {"formal_oos": {"failure_attribution": {
                "primary_cause": "market_exposure", "confidence": "high",
            }}},
        },
    ])

    attribution = evolution._latest_challenger_failure_attribution()

    assert attribution["primary_cause"] == "selection_or_allocation_residual"
    assert attribution["_routing_source"] == {
        "model_version_id": 52, "strategy_id": 12, "model_version": "new",
    }


def test_future_shadow_tasks_are_safely_skipped_when_current_data_is_degraded(monkeypatch):
    monkeypatch.setattr(evolution, "_experiment_data_gate", lambda: {
        "status": "degraded", "experiment_ready": False,
        "issues": [{"code": "latest_adj_factor_not_ready"}],
    })
    monkeypatch.setattr(
        evolution, "_execute_strategy_cycle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run shadow")),
    )

    result = evolution.execute_evolution_task({"id": 9, "task_type": "strategy_shadow_cycle"})

    assert result["status"] == "blocked_by_decision_data"
    assert result["data_gate"]["issues"] == [{"code": "latest_adj_factor_not_ready"}]


def test_formal_oos_uses_historical_gate_not_current_close_gate(monkeypatch):
    monkeypatch.setattr(evolution, "_experiment_data_gate", lambda: {"status": "ready", "experiment_ready": True})
    monkeypatch.setattr(evolution, "_execute_strategy_cycle", lambda *_args, **_kwargs: {"status": "completed"})

    result = evolution.execute_evolution_task({"id": 9, "task_type": "strategy_evolution_cycle"})

    assert result == {"status": "completed"}


def test_current_experiment_gate_fails_closed_on_optimistic_result(monkeypatch):
    monkeypatch.setattr(evolution, "_experiment_data_gate", lambda: {
        "ok": False, "status": "ready", "experiment_ready": True,
    })

    result = evolution.execute_evolution_task({"id": 9, "task_type": "strategy_shadow_cycle"})

    assert result["status"] == "blocked_by_decision_data"


def test_formal_oos_is_not_blocked_by_latest_close_gap(monkeypatch):
    monkeypatch.setattr(evolution, "_experiment_data_gate", lambda: {
        "status": "blocked", "experiment_ready": False,
        "issues": [{"code": "latest_adj_factor_not_ready"}],
    })
    monkeypatch.setattr(evolution, "_execute_strategy_cycle", lambda *_args, **_kwargs: {"status": "completed"})

    result = evolution.execute_evolution_task({"id": 9, "task_type": "strategy_controller_oos"})

    assert result == {"status": "completed"}


def test_formal_oos_is_blocked_by_historical_pit_gap(monkeypatch):
    monkeypatch.setattr(evolution, "audit_oos_hard_data", lambda **_kwargs: {
        "status": "blocked",
        "non_repairable_dates": ["20260701"],
        "repair_dates": [],
    })

    result = evolution.execute_evolution_task({
        "id": 9,
        "task_type": "strategy_evolution_cycle",
        "payload": {},
    })

    assert result["status"] == "blocked_by_oos_hard_data"
    assert result["hard_data_audit"]["non_repairable_dates"] == ["20260701"]


def test_model_strategy_ab_is_blocked_by_the_same_historical_pit_gate(monkeypatch):
    monkeypatch.setattr(evolution, "audit_oos_hard_data", lambda **_kwargs: {
        "status": "blocked",
        "non_repairable_dates": ["20260701"],
        "repair_dates": [],
    })

    result = evolution.execute_evolution_task({
        "id": 9,
        "task_type": "model_strategy_impact_validation",
        "payload": {"model_version_id": 17},
    })

    assert result["status"] == "blocked_by_oos_hard_data"
    assert result["scope"] == "formal_oos"
    assert result["hard_data_audit"]["non_repairable_dates"] == ["20260701"]


def test_worker_defers_data_block_without_creating_lineage_tasks(monkeypatch):
    task = {
        "id": 91,
        "task_type": "strategy_evolution_cycle",
        "status": "running",
        "attempt": 1,
    }
    deferred = []
    monkeypatch.setattr(evolution, "_claim_next_task", lambda: task)
    monkeypatch.setattr(
        evolution,
        "execute_evolution_task",
        lambda _task: {
            "status": "blocked_by_decision_data",
            "reason": "latest_decision_data_not_safe_for_research",
        },
    )
    monkeypatch.setattr(
        evolution,
        "_defer_for_decision_data",
        lambda pending, result: deferred.append((pending, result)) or True,
    )
    monkeypatch.setattr(
        evolution,
        "_finish_success",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a data-blocked task must not complete")
        ),
    )
    monkeypatch.setattr(
        evolution,
        "_schedule_strategy_continuation",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("a data-blocked task must not clone its lineage")
        ),
    )
    monkeypatch.setattr(
        evolution,
        "get_evolution_task",
        lambda task_id: {"id": task_id, "status": "retry_wait"},
    )

    result = evolution.run_worker_once()

    assert result == {"id": 91, "status": "retry_wait"}
    assert deferred[0][0] == task
    assert deferred[0][1]["status"] == "blocked_by_decision_data"


def test_data_gate_deferral_reuses_task_id_and_does_not_consume_attempt(monkeypatch):
    transition = {}
    monkeypatch.setattr(
        evolution,
        "_update_active_task",
        lambda task_id, **fields: transition.update(
            {"task_id": task_id, **fields}
        ) or True,
    )
    blocked = {
        "status": "blocked_by_decision_data",
        "reason": "latest_decision_data_not_safe_for_research",
    }

    assert evolution._defer_for_decision_data(
        {"id": 91, "attempt": 1}, blocked
    ) is True

    assert transition["task_id"] == 91
    assert transition["status"] == "retry_wait"
    assert transition["phase"] == "waiting_for_decision_data"
    assert transition["attempt"] == 0
    assert transition["result"] == blocked
    assert transition["lease_owner"] is None
    assert transition["deadline_at"] is None


def test_nonrepairable_oos_deferral_reuses_task_id_without_retrying_every_few_minutes(monkeypatch):
    transition = {}
    monkeypatch.setattr(
        evolution,
        "_update_active_task",
        lambda task_id, **fields: transition.update({"task_id": task_id, **fields}) or True,
    )
    blocked = {
        "status": "blocked_by_oos_hard_data",
        "hard_data_audit": {"status": "blocked", "non_repairable_dates": ["20260701"]},
    }

    assert evolution._defer_for_oos_hard_data({"id": 92, "attempt": 1}, blocked) is True

    assert transition["task_id"] == 92
    assert transition["status"] == "retry_wait"
    assert transition["phase"] == "waiting_for_historical_pit_evidence"
    assert transition["attempt"] == 0
    assert transition["result"] == blocked
    assert transition["lease_owner"] is None
    assert datetime.fromisoformat(transition["next_run_at"]) >= datetime.now().astimezone() + timedelta(hours=23)


def test_cycle_uses_checkpoint_and_reports_overall_progress(monkeypatch):
    monkeypatch.setattr(evolution, "audit_oos_hard_data", lambda **kwargs: {
        "status": "ready", "window_start": "20200101", "window_end": "20261231",
    })
    monkeypatch.setattr(evolution, "_strategy_targets", lambda: [
        {"role": "champion", "strategy_id": 1, "model_version_id": 42, "name": "Champion"},
        {"role": "challenger", "strategy_id": 2, "model_version_id": 43, "name": "Challenger"},
    ])
    monkeypatch.setattr(evolution, "_persist_candidate_result", lambda *args: None)
    monkeypatch.setattr(evolution, "_ensure_task_active", lambda *args: None)
    monkeypatch.setattr(evolution, "advance_formal_candidate_lifecycle", lambda *args, **kwargs: [])
    monkeypatch.setattr(evolution, "list_model_versions", lambda *_args, **_kwargs: [])
    heartbeats: list[dict] = []
    monkeypatch.setattr(evolution, "_heartbeat", lambda task_id, **fields: heartbeats.append(fields))
    received_checkpoints: list[list[dict]] = []
    received_ranges: list[tuple[str, str]] = []

    def runner(**kwargs):
        received_checkpoints.append(kwargs["checkpoint_results"])
        received_ranges.append((kwargs["start_date"], kwargs["end_date"]))
        windows = list(kwargs["checkpoint_results"])
        while len(windows) < 2:
            index = len(windows) + 1
            windows.append({
                "index": index,
                "metrics": {"total_return": .01, "benchmark_return": 0, "excess_return": .01, "max_drawdown": -.01},
                "summary": {"total_fees": 1},
                "data_complete": True,
                "point_in_time_passed": True,
                "production_strategy_version": "champion-v1",
                "market_state_attribution": {},
            })
            kwargs["progress_callback"]({
                "checkpoint_results": list(windows),
                "message": f"{len(windows)}/2",
            })
        return {
            "status": "failed", "windows": windows,
            "summary": {"aggregate_cost_after_return": .01, "aggregate_benchmark_return": .42},
            "promotion_gates": {}, "blocking_reasons": ["aggregate_cost_after_excess_positive"],
        }

    result = evolution._execute_strategy_cycle(
        {"id": 7, "payload": {"max_windows": 2}, "checkpoint": {
            "completed": [{"role": "champion", "strategy_id": 1, "summary": {}}],
            "current": {"strategy_id": 2, "windows": [{"index": 1}]},
            "hard_data_window": {"start": "20200102", "end": "20261230"},
        }},
        validation_runner=runner,
    )
    assert received_checkpoints == [[{"index": 1}]]
    assert received_ranges == [("20200102", "20261230")]
    assert len(result["strategies"]) == 2
    assert heartbeats[-1]["progress"] == 1.0
    assert heartbeats[-1]["checkpoint"]["schema_version"] == "alis_formal_oos_checkpoint_v2"
    assert [item["strategy_id"] for item in heartbeats[-1]["checkpoint"]["targets"]] == [1, 2]


def test_cycle_resume_uses_frozen_targets_instead_of_new_database_candidates(monkeypatch):
    monkeypatch.setattr(evolution, "audit_oos_hard_data", lambda **_kwargs: {
        "status": "ready", "window_start": "20200101", "window_end": "20261231",
    })
    monkeypatch.setattr(
        evolution,
        "_strategy_targets",
        lambda: (_ for _ in ()).throw(
            AssertionError("resume must not resolve a new target set")
        ),
    )
    monkeypatch.setattr(evolution, "_ensure_task_active", lambda *_args: None)
    monkeypatch.setattr(evolution, "_heartbeat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(evolution, "_persist_candidate_result", lambda *_args: None)
    monkeypatch.setattr(evolution, "list_model_versions", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(evolution, "advance_formal_candidate_lifecycle", lambda *_args, **_kwargs: [])
    seen = []

    def runner(**kwargs):
        seen.append(kwargs["strategy_id"])
        return {
            "status": "failed",
            "windows": [],
            "summary": {},
            "promotion_gates": {},
            "blocking_reasons": ["test"],
        }

    result = evolution._execute_strategy_cycle(
        {
            "id": 92,
            "payload": {"max_candidates": 1, "max_windows": 1},
            "checkpoint": {
                "targets": [
                    {"role": "champion", "strategy_id": 1, "model_version_id": 50, "name": "Champion"},
                    {"role": "challenger", "strategy_id": 8, "model_version_id": 88, "name": "Frozen"},
                ],
                "completed": [{"role": "champion", "strategy_id": 1, "status": "failed"}],
                "current": {"target_identity": "strategy:8", "strategy_id": 8, "windows": []},
                "hard_data_window": {"start": "20200102", "end": "20261230"},
            },
        },
        validation_runner=runner,
    )

    assert seen == [8]
    assert [item["strategy_id"] for item in result["strategies"]] == [1, 8]
    assert result["hard_data_window"] == {"start": "20200102", "end": "20261230"}


def test_direct_challenger_cycle_injects_same_run_champion_reference(monkeypatch):
    monkeypatch.setattr(evolution, "audit_oos_hard_data", lambda **_kwargs: {
        "status": "ready", "window_start": "20200101", "window_end": "20260731",
    })
    monkeypatch.setattr(evolution, "resolve_production_strategy_champion", lambda: {
        "strategy_id": 1, "champion_id": 50, "champion_version": "champion-v50",
        "strategy_controller_version": "controller-v1", "strategy": {"name": "Champion"},
    })
    monkeypatch.setattr(evolution, "_ensure_task_active", lambda *_args: None)
    monkeypatch.setattr(evolution, "_heartbeat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(evolution, "_persist_candidate_result", lambda *_args: None)
    monkeypatch.setattr(evolution, "list_model_versions", lambda model_key, **_kwargs: [
        {"id": 44, "version": "equal-weight", "config": {"baseline_key": "multifactor_equal_weight", "strategy_id": 3}},
        {"id": 45, "version": "momentum", "config": {"baseline_key": "simple_momentum", "strategy_id": 4}},
    ] if model_key == "alis_strategy_baseline" else [])
    captured: list[list[dict]] = []
    monkeypatch.setattr(
        evolution,
        "advance_formal_candidate_lifecycle",
        lambda items, **_kwargs: captured.append(items) or [],
    )

    def runner(**kwargs):
        return {
            "schema_version": "alis_purged_walk_forward_oos_v1",
            "status": "passed",
            "production_strategy_version_id": 50,
            "production_strategy_version": "champion-v50",
            "strategy_controller_version": "controller-v1",
            "protocol": {"minimum_windows": 5, "cost_policy": "fees and slippage"},
            "windows": [{"index": 1, "test": {"start": "2025-01-01", "end": "2025-06-30", "sessions": 126}}],
            "summary": {"aggregate_cost_after_return": .01, "worst_window_drawdown": -.1, "total_fees": 100},
            "promotion_gates": {"passed": True}, "blocking_reasons": [],
        }

    result = evolution._execute_strategy_cycle(
        {
            "id": 31,
            "payload": {
                "max_candidates": 1, "max_windows": 1,
                "targets": [{"role": "challenger", "strategy_id": 11, "model_version_id": 57, "name": "V4"}],
            },
            "checkpoint": {},
        },
        validation_runner=runner,
    )

    assert [item["role"] for item in result["strategies"]] == ["champion", "baseline", "baseline", "challenger"]
    assert [item.get("baseline_key") for item in result["strategies"][1:3]] == ["multifactor_equal_weight", "simple_momentum"]
    assert captured and len({item["reference_contract"]["cycle_task_id"] for item in captured[0]}) == 1


def test_controller_candidate_uses_own_identity_and_skips_generic_lifecycle(monkeypatch):
    monkeypatch.setattr(evolution, "audit_oos_hard_data", lambda **kwargs: {
        "status": "ready", "window_start": "20200101", "window_end": "20261231",
    })
    monkeypatch.setattr(evolution, "_ensure_task_active", lambda *args: None)
    monkeypatch.setattr(evolution, "_heartbeat", lambda *args, **kwargs: None)
    persisted = []
    monkeypatch.setattr(evolution, "_persist_candidate_result", lambda target, result: persisted.append(target))
    monkeypatch.setattr(evolution, "advance_formal_candidate_lifecycle", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not run")))
    seen = []

    def runner(**kwargs):
        seen.append(kwargs.get("strategy_controller_version_id"))
        return {"status": "failed", "windows": [], "summary": {}, "promotion_gates": {}, "blocking_reasons": []}

    result = evolution._execute_strategy_cycle({"id": 8, "payload": {"max_windows": 5, "targets": [{
        "role": "strategy_controller_candidate", "strategy_id": 1, "model_version_id": 55,
        "controller_version_id": 55, "name": "V55",
    }]}, "checkpoint": {"completed": [], "current": {}}}, validation_runner=runner)
    assert seen == [55]
    assert persisted[0]["controller_version_id"] == 55
    assert result["lifecycle"] == []


def test_controller_candidate_cannot_rebind_from_proxy_shadow_alone(monkeypatch):
    version = {
        "id": 55,
        "metrics": {
            "evaluation_scope": "candidate_specific_shadow_20d",
            "shadow_sessions": 20,
            "net_lift": .01,
            "walk_forward_lift": .01,
            "worst_window_net_lift": .005,
        },
        "validation": {"status": "formal_oos_queued"},
    }
    persisted_validation = {}
    activated = []
    rebound = []
    monkeypatch.setattr(evolution, "get_model_version", lambda _id: version)
    monkeypatch.setattr(evolution, "update_model_version_metrics", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(evolution, "update_model_version_validation", lambda _id, *, validation: persisted_validation.update(validation))
    monkeypatch.setattr("app.db.store.set_active_model_version", lambda version_id: activated.append(version_id))
    monkeypatch.setattr("app.services.production_strategy.ensure_production_strategy_champion", lambda **kwargs: rebound.append(kwargs))

    evolution._persist_candidate_result(
        {"role": "strategy_controller_candidate", "model_version_id": 55},
        {"status": "passed", "promotion_gates": {"cost_after": True, "point_in_time": True}, "blocking_reasons": [], "summary": {}, "windows": []},
    )

    assert activated == []
    assert rebound == []
    assert persisted_validation["status"] == "formal_oos_passed"
    assert persisted_validation["production_eligible"] is False
    assert "portfolio_shadow_and_statistics_gate" in persisted_validation["blocking_reasons"]


def test_controller_candidate_failed_formal_oos_is_rejected_without_shadow(monkeypatch):
    version = {"id": 55, "metrics": {}, "validation": {"status": "formal_oos_queued"}}
    persisted_validation = {}
    status_updates = []
    monkeypatch.setattr(evolution, "get_model_version", lambda _id: version)
    monkeypatch.setattr(evolution, "update_model_version_metrics", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(evolution, "update_model_version_validation", lambda _id, *, validation: persisted_validation.update(validation))
    monkeypatch.setattr(evolution, "update_model_version_status", lambda *args, **kwargs: status_updates.append((args, kwargs)))

    evolution._persist_candidate_result(
        {"role": "strategy_controller_candidate", "model_version_id": 55},
        {"status": "failed", "promotion_gates": {"cost_after": False}, "blocking_reasons": ["cost_after"], "summary": {}, "windows": []},
    )

    assert persisted_validation["status"] == "formal_oos_failed"
    assert persisted_validation["next_gate"] == "formal_oos_rejected"
    assert status_updates == [((55,), {"status": "rejected", "is_active": False})]


def test_strategy_cycle_automatically_queues_remaining_lineage_candidates(monkeypatch):
    monkeypatch.setattr(evolution, "V10_RESEARCH_ENABLED", False)
    monkeypatch.setattr(evolution, "_strategy_targets", lambda: [
        {"role": "champion", "strategy_id": 1, "model_version_id": 50},
        {"role": "challenger", "strategy_id": 12, "model_version_id": 58},
        {"role": "challenger", "strategy_id": 14, "model_version_id": 60},
    ])
    created = []
    monkeypatch.setattr(evolution, "create_evolution_task", lambda **kwargs: created.append(kwargs) or {"id": 99, **kwargs})

    result = evolution._schedule_strategy_continuation(completed_task_id=31)

    assert result["id"] == 99
    assert created[0]["task_type"] == "strategy_evolution_cycle"
    assert created[0]["payload"]["trigger"] == "automatic_lineage_continuation"
    assert created[0]["dedupe_key"] == "strategy_evolution_continuation:v2:58-60:after-31"


def test_reconcile_failed_controller_oos_candidate(monkeypatch):
    updates = []
    monkeypatch.setattr(evolution, "list_model_versions", lambda *_args, **_kwargs: [{
        "id": 55, "status": "candidate",
        "validation": {"status": "formal_oos_failed", "next_gate": "candidate_specific_shadow_observation"},
        "metrics": {"formal_oos": {"status": "failed", "blocking_reasons": ["cost_after"]}},
    }])
    monkeypatch.setattr(evolution, "update_model_version_validation", lambda version_id, *, validation: updates.append((version_id, validation)))
    monkeypatch.setattr(evolution, "update_model_version_status", lambda *args, **kwargs: updates.append((args, kwargs)))

    result = evolution.reconcile_failed_controller_oos_candidates()

    assert result["reconciled_version_ids"] == [55]
    assert updates[0][1]["next_gate"] == "formal_oos_rejected"
    assert updates[1] == ((55,), {"status": "rejected", "is_active": False})


def test_cancel_task_immediately_enters_terminal_state_and_releases_lease(monkeypatch):
    snapshots = iter([
        {"id": 25, "status": "running", "stop_requested": 0},
        {
            "id": 25, "status": "cancelled", "phase": "cancelled",
            "stop_requested": 1, "lease_owner": None, "lease_expires_at": None,
        },
    ])
    transition = {}
    monkeypatch.setattr(evolution, "get_evolution_task", lambda _task_id: next(snapshots))
    monkeypatch.setattr(
        evolution,
        "_update_active_task",
        lambda task_id, **fields: transition.update({"task_id": task_id, **fields}) or True,
    )

    result = evolution.cancel_evolution_task(25)

    assert result["status"] == "cancelled"
    assert transition["statuses"] == evolution.ACTIVE_STATES
    assert transition["status"] == "cancelled"
    assert transition["stop_requested"] == 1
    assert transition["lease_owner"] is None
    assert transition["lease_expires_at"] is None


def test_late_worker_success_uses_atomic_non_cancelled_transition(monkeypatch):
    transition = {}
    monkeypatch.setattr(
        evolution,
        "_update_active_task",
        lambda task_id, **fields: transition.update({"task_id": task_id, **fields}) or False,
    )

    evolution._finish_success(25, {"status": "completed"})

    assert transition["statuses"] == ("running",)
    assert transition["require_not_stopped"] is True
    assert transition["expected_lease_owner"] == evolution.WORKER_ID
    assert transition["status"] == "completed"
    assert transition["lease_owner"] is None
    assert transition["lease_expires_at"] is None


def test_late_worker_failure_cannot_overwrite_cancelled_terminal_state(monkeypatch):
    monkeypatch.setattr(
        evolution,
        "get_evolution_task",
        lambda _task_id: {"id": 25, "status": "cancelled", "stop_requested": 1},
    )
    monkeypatch.setattr(
        evolution,
        "_update_active_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("terminal task must not change")),
    )

    evolution._finish_failure({"id": 25}, RuntimeError("late failure"))


def test_unknown_evolution_failure_stops_without_blind_retry(monkeypatch):
    task = {
        "id": 26, "task_type": "strategy_evolution_cycle", "status": "running",
        "stop_requested": 0, "lease_owner": evolution.WORKER_ID,
        "attempt": 1, "max_attempts": 3,
    }
    transition = {}
    monkeypatch.setattr(evolution, "get_evolution_task", lambda _task_id: task)
    monkeypatch.setattr(
        evolution,
        "_update_active_task",
        lambda task_id, **fields: transition.update({"task_id": task_id, **fields}) or True,
    )

    evolution._finish_failure(task, RuntimeError("unexpected invariant violation"))

    assert transition["status"] == "failed"
    assert transition["result"]["classification"]["retryable"] is False
    assert transition["result"]["classification"]["category"] == "unknown_training_failure"


def test_historical_contract_incompatibility_is_terminal_but_not_owner_incident(monkeypatch):
    task = {
        "id": 28, "task_type": "controller_candidate_observation", "status": "running",
        "stop_requested": 0, "lease_owner": evolution.WORKER_ID,
        "attempt": 1, "max_attempts": 3,
    }
    transition = {}
    monkeypatch.setattr(evolution, "get_evolution_task", lambda _task_id: task)
    monkeypatch.setattr(
        evolution, "_update_active_task",
        lambda task_id, **fields: transition.update({"task_id": task_id, **fields}) or True,
    )

    evolution._finish_failure(task, ValueError("冻结策略包缺少因子绑定：momentum_60"))

    assert transition["status"] == "failed"
    assert transition["phase"] == "historical_contract_incompatible"
    assert transition["result"]["classification"]["category"] == "historical_contract_incompatible"


def test_shadow_equity_evidence_gap_is_not_reported_as_unknown_failure(monkeypatch):
    task = {
        "id": 29, "task_type": "strategy_shadow_cycle", "status": "running",
        "stop_requested": 0, "lease_owner": evolution.WORKER_ID,
        "attempt": 1, "max_attempts": 3,
    }
    transition = {}
    monkeypatch.setattr(evolution, "get_evolution_task", lambda _task_id: task)
    monkeypatch.setattr(
        evolution,
        "_update_active_task",
        lambda task_id, **fields: transition.update({"task_id": task_id, **fields}) or True,
    )

    evolution._finish_failure(task, RuntimeError("影子重放 2026-08-18 缺少相邻两个权益点"))

    assert transition["status"] == "failed"
    assert transition["phase"] == "shadow_equity_evidence_gap"
    assert transition["result"]["classification"]["category"] == "shadow_equity_evidence_gap"
    assert "未知故障" not in transition["message"]


def test_historical_contract_reconciliation_reason_is_limited_to_retired_bindings():
    assert evolution._historical_contract_incompatibility_reason({
        "task_type": "controller_candidate_observation",
        "error": "冻结策略包缺少因子绑定：momentum_60",
    })
    assert evolution._historical_contract_incompatibility_reason({
        "task_type": "controller_candidate_observation",
        "error": "未知模型绑定：event_surprise_lgbm_v1",
    })
    assert evolution._historical_contract_incompatibility_reason({
        "task_type": "controller_candidate_observation",
        "error": "database deadlock",
    }) is None
    assert evolution._historical_contract_incompatibility_reason({
        "task_type": "factor_research_cycle",
        "error": "冻结策略包缺少因子绑定：is_st,is_suspended",
    })


def test_historical_data_gate_reconciliation_requires_persisted_blocked_evidence():
    assert evolution._historical_data_gate_waiting_reason({
        "task_type": "strategy_shadow_cycle",
        "error": "OOS 永久硬数据未就绪，影子观察冻结",
        "result_json": '{"status": "blocked_by_decision_data"}',
    })
    assert evolution._historical_data_gate_waiting_reason({
        "task_type": "strategy_shadow_cycle",
        "error": "database deadlock",
        "result_json": '{"status": "blocked_by_decision_data"}',
    }) is None


def test_transient_connection_failure_keeps_bounded_retry(monkeypatch):
    task = {
        "id": 27, "task_type": "strategy_evolution_cycle", "status": "running",
        "stop_requested": 0, "lease_owner": evolution.WORKER_ID,
        "attempt": 1, "max_attempts": 3,
    }
    transition = {}
    monkeypatch.setattr(evolution, "get_evolution_task", lambda _task_id: task)
    monkeypatch.setattr(
        evolution,
        "_update_active_task",
        lambda task_id, **fields: transition.update({"task_id": task_id, **fields}) or True,
    )

    evolution._finish_failure(task, ConnectionError("connection reset by peer"))

    assert transition["status"] == "retry_wait"
    assert transition["result"]["classification"]["retryable"] is True
    assert transition["result"]["classification"]["category"] == "transient_external_dependency"


def test_formal_oos_resource_budget_requeues_same_task_with_checkpoint(monkeypatch):
    task = {
        "id": 1860,
        "task_type": "strategy_evolution_cycle",
        "status": "running",
        "stop_requested": 0,
        "lease_owner": evolution.WORKER_ID,
        "attempt": 1,
        "checkpoint": {
            "completed": [
                {"strategy_id": 1, "role": "champion", "status": "failed"},
                {"strategy_id": 3, "role": "baseline", "status": "failed"},
            ],
            "current": {
                "target_identity": "strategy:4",
                "strategy_id": 4,
                "windows": [{"index": 1}],
            },
            "hard_data_window": {"start": "20200327", "end": "20260729"},
        },
    }
    transition = {}
    monkeypatch.setattr(evolution, "get_evolution_task", lambda _task_id: task)
    monkeypatch.setattr(
        evolution,
        "_update_active_task",
        lambda task_id, **fields: transition.update(
            {"task_id": task_id, **fields}
        ) or True,
    )

    evolution._finish_failure(
        task, evolution.EvolutionTaskBudgetExceeded("任务超过资源预算时限")
    )

    assert transition["task_id"] == 1860
    assert transition["status"] == "retry_wait"
    assert transition["phase"] == "resource_budget_checkpoint"
    assert transition["attempt"] == 0
    assert transition["lease_owner"] is None
    assert transition["deadline_at"] is None
    assert len(transition["checkpoint"]["completed"]) == 2
    assert transition["checkpoint"]["current"]["windows"] == [{"index": 1}]
    assert transition["checkpoint"]["resource_budget_resume"]["resume_count"] == 1
    assert transition["checkpoint"]["resource_budget_resume"]["stalled_resumes"] == 0


def test_budget_edge_heartbeat_persists_completed_window_before_yield(monkeypatch):
    transition = {}
    monkeypatch.setattr(
        evolution,
        "_ensure_task_active",
        lambda _task_id: (_ for _ in ()).throw(
            evolution.EvolutionTaskBudgetExceeded("任务超过资源预算时限")
        ),
    )
    monkeypatch.setattr(
        evolution,
        "_update_active_task",
        lambda task_id, **fields: transition.update(
            {"task_id": task_id, **fields}
        ) or True,
    )
    checkpoint = {
        "current": {"strategy_id": 1, "windows": [{"index": 1}]},
    }

    try:
        evolution._heartbeat(
            2239,
            phase="formal_oos",
            progress=0.2,
            message="窗口 1 已完成",
            checkpoint=checkpoint,
        )
    except evolution.EvolutionTaskBudgetExceeded:
        pass
    else:
        raise AssertionError("budget edge must yield after persisting checkpoint")

    assert transition["checkpoint"] == checkpoint
    assert transition["progress"] == 0.2
    assert transition["expected_lease_owner"] == evolution.WORKER_ID


def test_local_lease_owner_liveness_distinguishes_dead_and_live_processes(monkeypatch):
    owner = f"{evolution.socket.gethostname()}:12345:worker"
    monkeypatch.setattr(evolution.os, "kill", lambda pid, signal: None)
    assert evolution._lease_owner_process_alive(owner) is True

    def missing_process(_pid, _signal):
        raise ProcessLookupError

    monkeypatch.setattr(evolution.os, "kill", missing_process)
    assert evolution._lease_owner_process_alive(owner) is False
    assert evolution._lease_owner_process_alive("remote-host:12345:worker") is None


def test_task_checkpoint_rejects_a_worker_that_no_longer_owns_the_lease(monkeypatch):
    monkeypatch.setattr(
        evolution,
        "get_evolution_task",
        lambda _task_id: {
            "id": 28, "status": "running", "stop_requested": 0,
            "lease_owner": "another-worker", "deadline_at": None,
        },
    )

    try:
        evolution._ensure_task_active(28)
    except evolution.EvolutionTaskCancelled as exc:
        assert str(exc) == "任务租约已转移"
    else:
        raise AssertionError("stale worker must lose the task at the next checkpoint")


def test_restart_recovery_does_not_steal_a_live_workers_unexpired_lease(monkeypatch):
    owner = f"{evolution.socket.gethostname()}:12345:worker"
    queries = []

    class Cursor:
        def __init__(self, rows=(), rowcount=0):
            self._rows = list(rows)
            self.rowcount = rowcount

        def fetchall(self):
            return self._rows

    class Connection:
        _is_postgres = True

        def execute(self, sql, params=()):
            queries.append((sql, params))
            if "stop_requested=1" in sql and sql.lstrip().startswith("SELECT"):
                return Cursor()
            if "SELECT id,lease_owner,lease_expires_at" in sql:
                return Cursor([{
                    "id": 28,
                    "lease_owner": owner,
                    "lease_expires_at": (evolution._now() + timedelta(minutes=10)).isoformat(),
                }])
            raise AssertionError("live lease must not be recovered or overwritten")

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(evolution, "_conn", connection)
    monkeypatch.setattr(evolution, "_lease_owner_process_alive", lambda value: value == owner)

    assert evolution.recover_evolution_tasks() == 0
    assert len(queries) == 2


def test_research_event_uses_stable_evidence_dedupe_key(monkeypatch):
    captured = {}
    monkeypatch.setattr(evolution, "V10_RESEARCH_ENABLED", True)
    monkeypatch.setattr(
        evolution,
        "create_evolution_task",
        lambda **kwargs: captured.update(kwargs) or {"id": 71, "status": "queued"},
    )

    result = evolution.enqueue_factor_strategy_research_event(
        trigger="plan_model_training_completed",
        evidence_type="model_version",
        evidence_id=21,
        evidence={"model_key": "medium_horizon_lambdarank_v1"},
        priority=75,
    )

    assert result == {"id": 71, "status": "queued"}
    assert captured["task_type"] == "strategy_candidate_generation"
    assert captured["payload"]["evidence_id"] == "21"
    assert captured["payload"]["batch_size"] == 4
    assert captured["dedupe_key"].startswith(
        "factor_strategy_research_event:v2:strategy_candidate_generation:"
    )
    assert captured["priority"] == 75


def test_factor_research_continuation_reuses_same_snapshot_progress(monkeypatch):
    monkeypatch.setattr(evolution, "V10_RESEARCH_ENABLED", False)
    tasks_by_key = {}
    created_keys = []

    def create_task(**kwargs):
        key = kwargs["dedupe_key"]
        if key not in tasks_by_key:
            tasks_by_key[key] = {
                "id": len(tasks_by_key) + 1,
                "status": "queued",
                "payload": kwargs["payload"],
            }
            created_keys.append(key)
        return tasks_by_key[key]

    monkeypatch.setattr(evolution, "create_evolution_task", create_task)
    monkeypatch.setattr(evolution, "_update_task", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        evolution,
        "get_evolution_task",
        lambda task_id: next(
            item for item in tasks_by_key.values() if item["id"] == task_id
        ),
    )
    result = {
        "continue_research": True,
        "snapshot": {"snapshot_id": "snapshot-1"},
        "experiment_count": 15,
        "quick_screen_completed": 12,
        "confirmation_completed": 3,
        "current_stage": "confirmation",
        "rollover_epoch": True,
    }

    first = evolution._schedule_factor_research_continuation(
        completed_task_id=513,
        result=result,
    )
    second = evolution._schedule_factor_research_continuation(
        completed_task_id=514,
        result=result,
    )

    assert first["id"] == second["id"]
    assert len(created_keys) == 1
    assert created_keys[0].startswith(
        "factor_research_continuation:v2:snapshot-1:progress-"
    )
    assert first["payload"]["research_progress"] == {
        "experiment_count": 15,
        "quick_screen_completed": 12,
        "confirmation_completed": 3,
        "current_stage": "confirmation",
        "rollover_epoch": True,
    }


def test_factor_research_continuation_allows_progress_to_advance(monkeypatch):
    monkeypatch.setattr(evolution, "V10_RESEARCH_ENABLED", False)
    captured = []

    def create_task(**kwargs):
        captured.append(kwargs)
        return {"id": len(captured), "status": "completed"}

    monkeypatch.setattr(evolution, "create_evolution_task", create_task)
    monkeypatch.setattr(
        evolution,
        "get_evolution_task",
        lambda task_id: {"id": task_id, "status": "completed"},
    )
    base = {
        "continue_research": True,
        "snapshot": {"snapshot_id": "snapshot-1"},
        "experiment_count": 15,
        "quick_screen_completed": 12,
        "confirmation_completed": 3,
        "current_stage": "confirmation",
        "rollover_epoch": False,
    }

    evolution._schedule_factor_research_continuation(
        completed_task_id=513,
        result=base,
    )
    evolution._schedule_factor_research_continuation(
        completed_task_id=515,
        result={**base, "experiment_count": 16, "confirmation_completed": 4},
    )

    assert len(captured) == 2
    assert captured[0]["dedupe_key"] != captured[1]["dedupe_key"]


def test_factor_research_continuation_stops_without_continue_flag(monkeypatch):
    monkeypatch.setattr(
        evolution,
        "create_evolution_task",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not enqueue")),
    )

    result = evolution._schedule_factor_research_continuation(
        completed_task_id=513,
        result={
            "continue_research": False,
            "snapshot": {"snapshot_id": "snapshot-1"},
            "experiment_count": 15,
        },
    )

    assert result is None


def test_v10_environment_completion_uses_one_wave_followup_without_extra_drain(monkeypatch):
    monkeypatch.setattr(evolution, "V10_RESEARCH_ENABLED", True)
    captured = []
    monkeypatch.setattr(
        evolution,
        "create_evolution_task",
        lambda **kwargs: captured.append(kwargs) or {
            "id": len(captured),
            "status": "queued",
        },
    )

    evolution._schedule_v10_research_continuation(
        completed_task={
            "id": 765,
            "task_type": "strategy_environment_backtest",
            "payload": {"predecessor_task_id": 764},
        },
        result={"candidate_id": "candidate-a", "status": "completed"},
    )

    assert [item["task_type"] for item in captured] == [
        "strategy_cluster_distillation",
        "strategy_failure_model_training",
    ]
    assert all("legacy-764" in item["dedupe_key"] for item in captured)
    assert [item["priority"] for item in captured] == [90, 91]
    assert captured[1]["coalesce_active_type"] is True


def test_v10_candidate_generation_propagates_wave_binding_to_backtest(monkeypatch):
    monkeypatch.setattr(evolution, "V10_RESEARCH_ENABLED", True)
    captured = []
    monkeypatch.setattr(
        evolution,
        "create_evolution_task",
        lambda **kwargs: captured.append(kwargs) or {
            "id": len(captured),
            "status": "queued",
        },
    )

    evolution._schedule_v10_research_continuation(
        completed_task={"id": 800, "task_type": "strategy_candidate_generation"},
        result={
            "generated": [{
                "candidate_id": "candidate-a",
                "research_wave_id": "wave-a",
            }],
        },
    )

    assert captured[0]["task_type"] == "strategy_environment_backtest"
    assert captured[0]["payload"]["candidate_id"] == "candidate-a"
    assert captured[0]["payload"]["research_wave_id"] == "wave-a"


def test_v10_wave_waits_for_every_bound_candidate_before_distillation(monkeypatch):
    monkeypatch.setattr(evolution, "V10_RESEARCH_ENABLED", True)
    monkeypatch.setattr(
        evolution,
        "create_evolution_task",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not enqueue")),
    )

    created = evolution._schedule_v10_research_continuation(
        completed_task={
            "id": 900,
            "task_type": "strategy_environment_backtest",
            "payload": {
                "candidate_id": "candidate-a",
                "research_wave_id": "wave-a",
            },
        },
        result={
            "candidate_id": "candidate-a",
            "status": "completed",
            "research_wave_id": "wave-a",
            "wave_ready_for_distillation": False,
        },
    )

    assert created == []


def test_v10_completed_wave_schedules_one_wave_scoped_followup(monkeypatch):
    monkeypatch.setattr(evolution, "V10_RESEARCH_ENABLED", True)
    captured = []
    monkeypatch.setattr(
        evolution,
        "create_evolution_task",
        lambda **kwargs: captured.append(kwargs) or {
            "id": len(captured),
            "status": "queued",
        },
    )

    evolution._schedule_v10_research_continuation(
        completed_task={
            "id": 904,
            "task_type": "strategy_environment_backtest",
            "payload": {
                "candidate_id": "candidate-d",
                "research_wave_id": "wave-a",
            },
        },
        result={
            "candidate_id": "candidate-d",
            "status": "completed",
            "research_wave_id": "wave-a",
            "wave_ready_for_distillation": True,
        },
    )

    assert [item["task_type"] for item in captured] == [
        "strategy_cluster_distillation",
        "strategy_failure_model_training",
    ]
    assert all("wave-a" in item["dedupe_key"] for item in captured)
    assert captured[0]["payload"]["research_wave_id"] == "wave-a"


def test_v10_portfolio_needs_improvement_does_not_double_generate(monkeypatch):
    monkeypatch.setattr(evolution, "V10_RESEARCH_ENABLED", True)
    monkeypatch.setattr(
        evolution,
        "create_evolution_task",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not enqueue")),
    )

    created = evolution._schedule_v10_research_continuation(
        completed_task={"id": 772, "task_type": "strategy_portfolio_research"},
        result={"status": "needs_improvement", "portfolio_id": "portfolio-a"},
    )

    assert created == []


def test_v10_cluster_followups_coalesce_global_research_lines(monkeypatch):
    monkeypatch.setattr(evolution, "V10_RESEARCH_ENABLED", True)
    captured = []
    monkeypatch.setattr(
        evolution,
        "create_evolution_task",
        lambda **kwargs: captured.append(kwargs) or {
            "id": len(captured),
            "status": "queued",
        },
    )

    evolution._schedule_v10_research_continuation(
        completed_task={"id": 901, "task_type": "strategy_cluster_distillation"},
        result={"generated": [{"prototype_id": "prototype-a"}]},
    )

    by_type = {item["task_type"]: item for item in captured}
    assert by_type["strategy_portfolio_research"]["coalesce_active_type"] is True
    assert by_type["strategy_candidate_generation"]["coalesce_active_type"] is True
