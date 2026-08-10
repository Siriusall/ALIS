from app.services import plan_model_auto_optimizer as optimizer
from app.services import plan_model_control
from datetime import datetime, timedelta


def _current_strategy_ab(model_key="leader_lambdarank_v1"):
    gates = {
        key: True
        for key in plan_model_control.CONTROLLED_STRATEGY_AB_REQUIRED_GATES
    }
    return {
        "schema_version": plan_model_control.CONTROLLED_STRATEGY_AB_CONTRACT,
        "comparison_contract": (
            plan_model_control.CONTROLLED_STRATEGY_AB_COMPARISON_CONTRACT
        ),
        "status": "passed",
        "model_effect_contract": (
            plan_model_control.NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT
            if model_key == "narrative_transition_multitask_v1"
            else "weighted_model_signal_fusion_v1"
        ),
        "frozen_contract": {
            "execution_cost_contract": {
                "schema_version": (
                    plan_model_control.CONTROLLED_STRATEGY_AB_COST_CONTRACT
                ),
            },
        },
        "windows": [{"fold": index} for index in range(1, 6)],
        "gates": gates,
    }


def test_event_optimizer_retrains_current_contract_after_label_audit_fix():
    recommendation = optimizer._recommendation({
        "model_key": "event_impact_assessor_v2",
        "status": "trainable",
        "coverage": {"data_ready": True},
        "latest_version": {"id": 271, "status": "candidate"},
    })

    assert recommendation["action"] == "retrain_challenger"
    assert recommendation["requires_human"] is False


def test_event_optimizer_waits_after_adaptive_formal_cohort_reuse():
    recommendation = optimizer._recommendation({
        "model_key": "event_impact_assessor_v2",
        "status": "trainable",
        "coverage": {
            "data_ready": True,
            "event_rows": 389,
            "latest_training_sessions": 1793,
        },
        "latest_training_protocol_status": "current",
        "latest_version": {
            "id": 278,
            "status": "candidate",
            "metrics": {
                "model_evaluation_contract": optimizer.MODEL_EVALUATION_CONTRACT,
                "effective_sessions": 1792,
                "event_research_cohort_governance": {
                    "status": "adaptive_same_cohort_non_promotable",
                    "allowed": False,
                },
            },
            "validation": {"review": {"status": "rejected"}},
        },
        "latest_train_run": {
            "metrics": {"coverage": {"event_rows": 388}},
        },
    })

    assert recommendation["action"] == "wait_for_oos_cohort_advance"
    assert recommendation["requires_human"] is False


def test_event_retrain_inherits_latest_frozen_candidate_spec():
    spec = {
        "name": "event_validation_selected_broad_or_refined_heads_v1",
        "event_head_feature_subsets": {"positive_impact": ["momentum_20"]},
        "param_overrides": {},
    }
    context = optimizer._training_request_context(
        {
            "model_key": "event_impact_assessor_v2",
            "reason": "完整新事件测试窗形成",
            "latest_version": {
                "id": 278,
                "metrics": {"candidate_spec": spec},
            },
        },
        trigger="retrain_challenger",
    )

    assert context["source_model_version_id"] == 278
    assert context["model_candidate_spec"] == spec
    assert context["model_candidate_spec"]["param_overrides"] == {}
    assert context["event_research_resume_contract"].endswith("_v1")


def test_rejected_narrative_starts_non_promotable_research_shadow():
    item = {
        "model_key": "narrative_transition_multitask_v1",
        "status": "rejected_candidate",
        "coverage": {
            "data_ready": True,
            "latest_training_sessions": 1601,
        },
        "latest_training_protocol_status": "current",
        "latest_version": {
            "id": 237,
            "status": "candidate",
            "metrics": {
                "model_evaluation_contract": optimizer.MODEL_EVALUATION_CONTRACT,
                "effective_sessions": 1599,
            },
            "validation": {"review": {"status": "rejected"}},
        },
        "latest_train_run": {
            "finished_at": (datetime.now() - timedelta(days=8)).isoformat(),
            "labeled_trade_date_count": 1599,
            "metrics": {"coverage": {"effective_sessions": 1600}},
        },
    }

    recommendation = optimizer._recommendation(item)

    assert recommendation["action"] == "train_research_shadow"
    assert "126交易日" in recommendation["reason"]

    context = optimizer._training_request_context(
        item,
        trigger=recommendation["action"],
    )
    assert context["evaluation_scope"] == "research_shadow_only"
    assert context["production_change_forbidden"] is True
    assert context["formal_promotion_forbidden"] is True


def test_narrative_research_shadow_observes_until_twenty_new_sessions():
    item = {
        "model_key": "narrative_transition_multitask_v1",
        "status": "candidate",
        "coverage": {
            "data_ready": True,
            "latest_training_sessions": 1618,
        },
        "latest_training_protocol_status": "current",
        "latest_version": {
            "id": 280,
            "status": "candidate",
            "metrics": {
                "model_evaluation_contract": optimizer.MODEL_EVALUATION_CONTRACT,
                "effective_sessions": 1600,
                "feature_computation_contract": {
                    "narrative_lifecycle": optimizer.NARRATIVE_LABEL_CONTRACT,
                },
                "targeted_training_context": {
                    "evaluation_scope": "research_shadow_only",
                    "formal_promotion_forbidden": True,
                },
            },
            "validation": {},
        },
        "latest_train_run": {
            "finished_at": (datetime.now() - timedelta(days=8)).isoformat(),
            "labeled_trade_date_count": 1600,
            "metrics": {"coverage": {"effective_sessions": 1600}},
        },
    }

    recommendation = optimizer._recommendation(item)

    assert recommendation["action"] == "observe_research_shadow"
    assert "新增20个交易日" in recommendation["reason"]


def test_rejected_narrative_retrains_after_complete_new_test_window():
    item = {
        "model_key": "narrative_transition_multitask_v1",
        "status": "rejected_candidate",
        "coverage": {
            "data_ready": True,
            "latest_training_sessions": 1725,
        },
        "latest_training_protocol_status": "current",
        "latest_version": {
            "id": 237,
            "status": "candidate",
            "metrics": {
                "model_evaluation_contract": optimizer.MODEL_EVALUATION_CONTRACT,
                "effective_sessions": 1599,
            },
            "validation": {"review": {"status": "rejected"}},
        },
        "latest_train_run": {
            "finished_at": (datetime.now() - timedelta(days=8)).isoformat(),
            "labeled_trade_date_count": 1599,
            "metrics": {"coverage": {"effective_sessions": 1599}},
        },
    }

    recommendation = optimizer._recommendation(item)

    assert recommendation["action"] == "retrain_challenger"


def test_plan_model_auto_promotion_requires_task_ab_and_real_shadow(monkeypatch):
    reviewed: list[dict] = []
    monkeypatch.setattr(
        optimizer,
        "review_plan_model_version",
        lambda model_version_id, **kwargs: reviewed.append({"id": model_version_id, **kwargs})
        or {"production_eligible": True},
    )
    family = {
        "items": [{
            "model_key": "leader_lambdarank_v1",
            "active_version": {
                "id": 1,
                "metrics": {},
            },
            "latest_version": {
                "id": 2,
                "version": "candidate-v2",
                "status": "candidate",
                    "metrics": {
                        "model_evaluation_contract": optimizer.MODEL_EVALUATION_CONTRACT,
                        "task_metric_passed": True,
                        "task_native_oos": {
                            "passed": True,
                            "baseline_outperformed": True,
                        },
                        "strategy_impact_ab": _current_strategy_ab(),
                },
                "validation": {
                    "production_eligibility": {
                        "gates": {
                            "artifact_exists": True,
                            "walk_forward_passed": True,
                            "live_observation_20_sessions": True,
                            "human_review": False,
                        }
                    }
                },
            },
        }]
    }
    result = optimizer._auto_promote_ready_candidates(family)
    assert len(result) == 1
    assert reviewed[0]["reviewer"] == "deterministic_auto_governance"
    assert reviewed[0]["waive_live_observation"] is False

    family["items"][0]["latest_version"]["validation"]["production_eligibility"]["gates"]["live_observation_20_sessions"] = False
    assert optimizer._auto_promote_ready_candidates(family) == []


def test_rejected_candidate_retrains_only_after_new_data_cadence(monkeypatch):
    item = {
        "coverage": {"data_ready": True, "effective_sessions": 101},
        "latest_version": {
            "id": 2,
            "status": "candidate",
            "created_at": (datetime.now() - timedelta(days=8)).isoformat(),
            "validation": {"review": {"status": "rejected"}},
        },
        "latest_train_run": {
            "finished_at": (datetime.now() - timedelta(days=8)).isoformat(),
            "metrics": {"coverage": {"effective_sessions": 100}},
        },
    }
    assert optimizer._periodic_retrain_due(item, require_rejected=True) is True

    item["latest_train_run"]["finished_at"] = datetime.now().isoformat()
    assert optimizer._periodic_retrain_due(item, require_rejected=True) is False


def test_rejected_candidate_does_not_retrain_without_ready_data():
    item = {
        "coverage": {"data_ready": False},
        "latest_version": {"validation": {"review": {"status": "rejected"}}},
    }
    assert optimizer._periodic_retrain_due(item, require_rejected=True) is False


def test_outdated_feature_contract_retrains_without_waiting_for_cadence():
    item = {
        "status": "legacy_invalid_candidate",
        "latest_training_protocol_status": "legacy_invalid",
        "coverage": {"data_ready": True},
        "active_version": {},
        "latest_version": {
            "id": 270,
            "status": "candidate",
            "metrics": {
                "model_evaluation_contract": optimizer.MODEL_EVALUATION_CONTRACT,
            },
            "validation": {"review": {"status": "rejected"}},
        },
        "latest_train_run": {"finished_at": datetime.now().isoformat()},
    }

    recommendation = optimizer._recommendation(item)

    assert recommendation["action"] == "retrain_challenger"
    assert "当前训练、评价或特征协议" in recommendation["reason"]


def test_periodic_production_retrain_requires_advanced_data_and_cadence():
    item = {
        "status": "production",
        "coverage": {"data_ready": True, "trade_sessions": 101},
        "active_version": {"id": 1, "metrics": {
            "model_evaluation_contract": optimizer.MODEL_EVALUATION_CONTRACT,
        }},
        "latest_version": {"id": 1, "status": "production", "metrics": {
            "model_evaluation_contract": optimizer.MODEL_EVALUATION_CONTRACT,
        }},
        "latest_train_run": {
            "finished_at": (datetime.now() - timedelta(days=8)).isoformat(),
            "metrics": {"coverage": {"trade_sessions": 100}},
        },
    }
    assert optimizer._recommendation(item)["action"] == "retrain_challenger"

    item["coverage"]["trade_sessions"] = 100
    assert optimizer._recommendation(item)["action"] == "healthy"


def test_auto_optimizer_ensures_registered_data_work_order(monkeypatch):
    status = {
        "auto_rejected": [],
        "auto_promoted": [],
        "items": [{
            "model_key": "capital_persistence_multitask_v1",
            "trigger": "wait_for_data",
            "running": False,
            "status": "proxy",
            "reason": "历史数据不足",
        }],
    }
    monkeypatch.setattr(
        optimizer,
        "build_auto_optimization_status",
        lambda **_kwargs: status,
    )
    from app.data_platform.services import backfill
    from app.services import model_shadow_observation, model_strategy_impact

    monkeypatch.setattr(
        model_strategy_impact,
        "reconcile_narrative_strategy_impact_contract",
        lambda: {"ok": True, "changed": False, "status": "test_noop"},
    )
    monkeypatch.setattr(
        model_strategy_impact,
        "reconcile_plan_model_strategy_impact_contracts",
        lambda: {"ok": True, "changed_count": 0, "changed": []},
    )
    monkeypatch.setattr(
        model_shadow_observation,
        "ensure_model_shadow_tasks",
        lambda: {"ok": True, "queued": [], "enrolled": [], "skipped": []},
    )

    monkeypatch.setattr(
        backfill,
        "ensure_plan_model_data_backfill",
        lambda: {"ok": True, "created": True, "task": {"status": "queued"}},
    )

    result = optimizer.run_auto_optimization_once()

    assert result["model_data_backfill"]["created"] is True
    assert result["started_count"] == 0
