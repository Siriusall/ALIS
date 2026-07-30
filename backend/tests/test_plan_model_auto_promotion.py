from app.services import plan_model_auto_optimizer as optimizer
from datetime import datetime, timedelta


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
                        "strategy_impact_ab": {"status": "passed"},
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
