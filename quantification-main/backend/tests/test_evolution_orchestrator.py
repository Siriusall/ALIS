from app.services import evolution_orchestrator as evolution


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


def test_strategy_diagnosis_routes_selection_allocation_residual_to_bounded_top_n_candidate():
    result = evolution.diagnose_strategy_results([
        {
            "role": "challenger", "summary": {"aggregate_cost_after_return": .02},
            "failure_attribution": {"primary_cause": "selection_or_allocation_residual", "confidence": "high"},
        },
    ])
    assert result["primary_gap"] == "selection_or_allocation_residual"
    assert result["recommended_next_candidate"] == "bounded_top_n_selection_candidate_v1"


def test_strategy_targets_routes_persisted_selection_residual_without_exposure_candidate(monkeypatch):
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
        "app.services.strategy_candidate_generator.ensure_dynamic_exposure_challenger",
        lambda: (_ for _ in ()).throw(AssertionError("must not generate an exposure candidate")),
    )
    generated = {}
    monkeypatch.setattr(
        "app.services.strategy_candidate_generator.ensure_selection_allocation_challenger",
        lambda **kwargs: generated.update(kwargs) or None,
    )

    targets = evolution._strategy_targets()

    assert [item["strategy_id"] for item in targets] == [1, 2]
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


def test_controller_candidate_promotes_only_after_formal_oos_and_20_session_shadow(monkeypatch):
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

    assert activated == [55]
    assert rebound == [{"force_rebind": True}]
    assert persisted_validation["status"] == "passed"
    assert persisted_validation["production_eligible"] is True


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
