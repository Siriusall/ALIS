from app.services import model_strategy_impact as impact


def _walk_forward_evidence():
    return [
        {
            "fold": index,
            "validation_end": f"20260{index}31",
            "test_start": f"20260{index + 1}01",
            "test_end": f"20260{index + 1}28",
            "point_in_time_artifact": {
                "binding_contract": "point_in_time_fold_model_v1",
                "fold": index,
                "artifact_path": f"/tmp/fold-{index}/artifact.json",
                "model_sha256": f"sha-{index}",
                "available_at": f"2026-0{index}-31T15:00:00+08:00",
                "validation_end": f"20260{index}31",
                "test_start": f"20260{index + 1}01",
                "test_end": f"20260{index + 1}28",
            },
        }
        for index in range(1, 6)
    ]


def test_controlled_model_ab_freezes_everything_and_requires_paired_improvement(monkeypatch):
    monkeypatch.setattr(impact, "get_model_version", lambda version_id: {
        "id": version_id,
        "model_key": "medium_horizon_lambdarank_v1",
        "version": "candidate-v3",
        "status": "candidate",
        "metrics": {
            "walk_forward_evidence": _walk_forward_evidence(),
        },
        "validation": {},
    })
    monkeypatch.setattr(impact, "resolve_production_strategy_champion", lambda: {
        "champion_id": 7,
        "strategy_id": 3,
        "strategy": {"name": "Champion", "content": {"portfolio_method": "hrp"}},
        "strategy_controller_version_id": 11,
        "factor_bindings": {"momentum_20": {"version": "v1"}},
        "model_bindings": {
            "medium_horizon_lambdarank_v1": {"id": 4, "version": "old"},
            "leader_lambdarank_v1": {"id": 5, "version": "leader"},
        },
        "market_state_binding": {"production_model": "transparent_four_state_v1"},
        "portfolio_optimizer": {"implementation": "cvar"},
        "execution_contract": {"signal": "T_close"},
    })
    monkeypatch.setattr(impact, "update_model_version_metrics", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(impact, "update_model_version_validation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        impact,
        "active_production_plan_model_bindings",
        lambda: {
            key: (
                {"id": 5, "model_key": key, "version": "eligible-reference"}
                if key == "leader_lambdarank_v1"
                else None
            )
            for key in impact.PLAN_MODELS
        },
    )
    monkeypatch.setattr(
        impact,
        "registered_factor_bindings",
        lambda _keys: {
            "momentum_20": {"version": "v1"},
            "momentum_60": {"version": "v1"},
            "volatility_20": {"version": "v1"},
        },
    )

    calls = []

    def replay(**kwargs):
        calls.append(kwargs)
        candidate = kwargs["model_bindings"]["medium_horizon_lambdarank_v1"] is not None
        value = 100.0
        equity = [{"date": f"2026-01-{index:02d}", "total_value": value + index * (1.2 if candidate else 1.0)} for index in range(1, 31)]
        return {
            "status": "completed",
            "metrics": {
                "total_return": .30 if candidate else .20,
                "max_drawdown": -.10,
                "turnover_rate": .20,
            },
            "summary": {
                "total_fees": 12.5 if candidate else 10.0,
                "trade_count": 8 if candidate else 6,
            },
            "component_contract": {
                "execution": "A-share open constraints + shared fee/slippage rules",
            },
            "equity_series": equity,
            "model_fusion_history": (
                [{
                    "production_models": [{
                        "model_key": "medium_horizon_lambdarank_v1",
                        "consumed": True,
                        "availability_reason": "point_in_time_available",
                    }],
                }]
                if candidate else []
            ),
        }

    progress = []
    result = impact.run_controlled_model_strategy_ab(
        model_version_id=9,
        replay_runner=replay,
        progress_callback=progress.append,
    )

    assert len(calls) == 10
    assert all(call["strategy_id"] == 3 for call in calls)
    assert all(call["strategy_controller_version_id"] == 11 for call in calls)
    assert calls[0]["factor_bindings"] == calls[1]["factor_bindings"]
    assert "momentum_60" in calls[0]["factor_bindings"]
    assert len({id(calls[index]["data_source"]) for index in range(0, 10, 2)}) == 1
    assert len({id(calls[index]["data_source"]) for index in range(1, 10, 2)}) == 1
    assert calls[0]["data_source"] is not calls[1]["data_source"]
    assert result["status"] == "passed"
    assert result["schema_version"] == impact.CONTROLLED_STRATEGY_AB_CONTRACT
    assert (
        result["comparison_contract"]
        == impact.CONTROLLED_STRATEGY_AB_COMPARISON_CONTRACT
    )
    assert result["model_effect_contract"] == impact.DEFAULT_MODEL_EFFECT_CONTRACT
    assert result["gates"]["all_replays_completed"] is True
    assert result["gates"]["paired_dates_complete"] is True
    assert result["gates"]["candidate_fold_model_actually_consumed"] is True
    assert result["gates"]["candidate_fold_model_actually_effective"] is True
    assert result["gates"]["shared_fee_slippage_contract_frozen"] is True
    assert result["gates"]["strategy_returns_are_cost_after"] is True
    assert (
        result["frozen_contract"]["execution_cost_contract"]["schema_version"]
        == impact.CONTROLLED_STRATEGY_AB_COST_CONTRACT
    )
    assert result["frozen_contract"]["reference_model_bindings"]["medium_horizon_lambdarank_v1"] is None
    assert result["frozen_contract"]["reference_model_bindings"]["leader_lambdarank_v1"]["id"] == 5
    assert [
        calls[index]["model_bindings"]["medium_horizon_lambdarank_v1"]
        ["point_in_time_fold_artifact"]["fold"]
        for index in range(1, 10, 2)
    ] == [1, 2, 3, 4, 5]
    assert all(item["production_change_forbidden"] is True for item in [result])
    assert [len(item["completed_windows"]) for item in progress] == [1, 2, 3, 4, 5]

    calls.clear()
    resumed = impact.run_controlled_model_strategy_ab(
        model_version_id=9,
        replay_runner=replay,
        checkpoint_results=progress[1]["completed_windows"],
        checkpoint_contract_hash=progress[1]["contract_hash"],
    )
    assert len(calls) == 6
    assert len(resumed["windows"]) == 5


def test_reconcile_requeues_task_valid_legacy_strategy_ab(monkeypatch):
    candidate = {
        "id": 216,
        "model_key": "medium_horizon_lambdarank_v1",
        "version": "medium-v216",
        "status": "candidate",
        "metrics": {
            "task_metric_passed": True,
            "task_native_oos": {"passed": True, "baseline_outperformed": True},
            "strategy_impact_ab": {
                "schema_version": "alis_controlled_model_strategy_ab_v3",
                "comparison_contract": "point_in_time_fold_model_vs_disabled_ablation_v1",
                "status": "passed",
            },
        },
        "validation": {
            "status": "pending_live_observation",
            "production_eligibility": {
                "eligible": False,
                "gates": {
                    "data_ready": True,
                    "controlled_strategy_ab": True,
                    "live_observation_20_sessions": False,
                    "human_review": False,
                },
            },
            "model_shadow": {"status": "awaiting_first_future_signal"},
        },
    }
    monkeypatch.setattr(
        impact,
        "list_model_versions",
        lambda model_key, limit=10: [candidate]
        if model_key == "medium_horizon_lambdarank_v1"
        else [],
    )
    persisted = {}
    monkeypatch.setattr(
        impact,
        "update_model_version_metrics",
        lambda model_version_id, *, metrics: persisted.update(
            {"metrics_id": model_version_id, "metrics": metrics}
        ),
    )
    monkeypatch.setattr(
        impact,
        "update_model_version_validation",
        lambda model_version_id, *, validation: persisted.update(
            {"validation_id": model_version_id, "validation": validation}
        ),
    )
    from app.services import evolution_orchestrator

    created = {}
    monkeypatch.setattr(
        evolution_orchestrator,
        "create_evolution_task",
        lambda **kwargs: created.update(kwargs) or {"id": 2288, "status": "queued"},
    )

    result = impact.reconcile_plan_model_strategy_impact_contracts()

    assert result["changed_count"] == 1
    assert "strategy_impact_ab" not in persisted["metrics"]
    assert "model_shadow" not in persisted["validation"]
    assert persisted["validation"]["status"] == "pending_strategy_ab"
    assert created["task_type"] == "model_strategy_impact_validation"
    assert created["payload"]["strategy_ab_contract"] == impact.CONTROLLED_STRATEGY_AB_CONTRACT


def test_reconcile_narrative_ab_invalidates_old_effect_contract(monkeypatch):
    metrics = {
        "model_evaluation_contract": impact.MODEL_EVALUATION_CONTRACT,
        "task_native_oos": {
            "passed": True,
            "baseline_outperformed": True,
        },
        "strategy_impact_ab": {
            "schema_version": "alis_controlled_model_strategy_ab_v3",
            "status": "failed",
            "gates": {
                "candidate_fold_model_actually_consumed": True,
            },
        },
    }
    validation = {
        "status": "failed",
        "production_eligible": False,
        "production_eligibility": {
            "eligible": False,
            "gates": {
                "data_ready": True,
                "controlled_strategy_ab": False,
                "human_review": False,
                "live_observation_20_sessions": False,
            },
            "blocked_reasons": ["controlled_strategy_ab"],
        },
    }
    monkeypatch.setattr(
        impact,
        "list_model_versions",
        lambda model_key, limit=10: [
            {
                "id": 237,
                "model_key": model_key,
                "version": "narrative-v237",
                "status": "candidate",
                "metrics": metrics,
                "validation": validation,
            }
        ],
    )
    persisted = {}
    monkeypatch.setattr(
        impact,
        "update_model_version_metrics",
        lambda model_version_id, *, metrics: persisted.update(
            {"model_version_id": model_version_id, "metrics": metrics}
        ),
    )
    monkeypatch.setattr(
        impact,
        "update_model_version_validation",
        lambda model_version_id, *, validation: persisted.update(
            {"validation_version_id": model_version_id, "validation": validation}
        ),
    )
    from app.services import evolution_orchestrator

    task_payload = {}
    monkeypatch.setattr(
        evolution_orchestrator,
        "create_evolution_task",
        lambda **kwargs: task_payload.update(kwargs) or {
            "id": 1800,
            "status": "queued",
        },
    )

    result = impact.reconcile_narrative_strategy_impact_contract()

    assert result["changed"] is True
    assert result["model_version_id"] == 237
    assert "strategy_impact_ab" not in persisted["metrics"]
    assert persisted["validation"]["status"] == "pending_strategy_ab"
    assert (
        persisted["validation"]["strategy_ab_contract_reconciliation"][
            "required_contract"
        ]
        == impact.NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT
    )
    assert task_payload["task_type"] == "model_strategy_impact_validation"
    assert task_payload["payload"]["model_version_id"] == 237
    assert (
        task_payload["payload"]["model_effect_contract"]
        == impact.NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT
    )


def test_controlled_model_ab_blocks_legacy_version_without_fold_artifacts(monkeypatch):
    persisted = {}
    monkeypatch.setattr(impact, "get_model_version", lambda version_id: {
        "id": version_id,
        "model_key": "medium_horizon_lambdarank_v1",
        "version": "legacy",
        "status": "candidate",
        "metrics": {
            "walk_forward_evidence": [
                {
                    "fold": index,
                    "validation_end": f"20260{index}31",
                    "test_start": f"20260{index + 1}01",
                    "test_end": f"20260{index + 1}28",
                }
                for index in range(1, 6)
            ]
        },
        "validation": {},
    })
    monkeypatch.setattr(
        impact,
        "update_model_version_metrics",
        lambda version_id, **values: persisted.update(
            {"metrics_version_id": version_id, **values}
        ),
    )
    monkeypatch.setattr(
        impact,
        "update_model_version_validation",
        lambda version_id, **values: persisted.update(
            {"validation_version_id": version_id, **values}
        ),
    )

    result = impact.run_controlled_model_strategy_ab(
        model_version_id=9,
        replay_runner=lambda **_kwargs: {},
    )

    assert result["status"] == "blocked_by_missing_point_in_time_fold_artifacts"
    assert result["production_change_forbidden"] is True
    assert persisted["metrics"]["strategy_impact_ab"]["status"] == result["status"]
    assert persisted["validation"]["status"] == "failed"
    assert result["status"] in persisted["validation"]["production_eligibility"]["blocked_reasons"]


def test_controlled_model_ab_persists_replay_errors_as_blocked_evidence(monkeypatch):
    persisted = {}
    monkeypatch.setattr(impact, "get_model_version", lambda version_id: {
        "id": version_id,
        "model_key": "leader_lambdarank_v1",
        "version": "candidate-v3",
        "status": "candidate",
        "metrics": {"walk_forward_evidence": _walk_forward_evidence()},
        "validation": {},
    })
    monkeypatch.setattr(impact, "resolve_production_strategy_champion", lambda: {
        "champion_id": 7,
        "strategy_id": 3,
        "strategy": {"name": "Champion", "content": {"portfolio_method": "hrp"}},
        "strategy_controller_version_id": 11,
        "factor_bindings": {},
        "model_bindings": {},
        "market_state_binding": {},
        "portfolio_optimizer": {},
        "execution_contract": {},
    })
    monkeypatch.setattr(impact, "registered_factor_bindings", lambda _keys: {})
    monkeypatch.setattr(
        impact,
        "update_model_version_metrics",
        lambda version_id, **values: persisted.update(
            {"metrics_version_id": version_id, **values}
        ),
    )
    monkeypatch.setattr(
        impact,
        "update_model_version_validation",
        lambda version_id, **values: persisted.update(
            {"validation_version_id": version_id, **values}
        ),
    )

    def broken_replay(**_kwargs):
        raise RuntimeError("fixture replay failed")

    result = impact.run_controlled_model_strategy_ab(
        model_version_id=10,
        replay_runner=broken_replay,
    )

    assert result["status"] == "blocked_by_strategy_replay_error"
    assert result["failed_fold"] == 1
    assert result["error_type"] == "RuntimeError"
    assert result["production_change_forbidden"] is True
    assert persisted["metrics"]["strategy_impact_ab"] == result
    assert persisted["validation"]["status"] == "failed"
