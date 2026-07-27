from contextlib import contextmanager
from datetime import timedelta

from app.services import evolution_orchestrator as evolution


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


def test_direct_experiment_tasks_are_safely_skipped_when_current_data_is_degraded(monkeypatch):
    monkeypatch.setattr(evolution, "_experiment_data_gate", lambda: {
        "status": "degraded", "experiment_ready": False,
        "issues": [{"code": "latest_adj_factor_not_ready"}],
    })
    monkeypatch.setattr(
        evolution, "_execute_strategy_cycle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run OOS")),
    )

    result = evolution.execute_evolution_task({"id": 9, "task_type": "strategy_evolution_cycle"})

    assert result["status"] == "blocked_by_decision_data"
    assert result["data_gate"]["issues"] == [{"code": "latest_adj_factor_not_ready"}]


def test_direct_experiment_tasks_continue_when_current_data_is_ready(monkeypatch):
    monkeypatch.setattr(evolution, "_experiment_data_gate", lambda: {"status": "ready", "experiment_ready": True})
    monkeypatch.setattr(evolution, "_execute_strategy_cycle", lambda *_args, **_kwargs: {"status": "completed"})

    result = evolution.execute_evolution_task({"id": 9, "task_type": "strategy_evolution_cycle"})

    assert result == {"status": "completed"}


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

    def runner(**kwargs):
        received_checkpoints.append(kwargs["checkpoint_results"])
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
        }},
        validation_runner=runner,
    )
    assert received_checkpoints == [[{"index": 1}]]
    assert len(result["strategies"]) == 2
    assert heartbeats[-1]["progress"] == 1.0


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
