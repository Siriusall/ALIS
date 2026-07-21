import pickle

from app.services import plan_model_control


class DummyModel:
    def predict(self, rows):
        return [float(rows[0][1]) + 0.25]


def test_plan_model_family_reports_proxy_when_data_not_ready(monkeypatch):
    monkeypatch.setattr(plan_model_control, "_base_coverage_snapshot", lambda: {})
    monkeypatch.setattr(plan_model_control, "_coverage_snapshot", lambda key, base=None: {
        "trade_sessions": 10,
        "theme_snapshot_sessions": 3,
        "event_rows": 0,
        "capital_coverage_slices": 0,
        "effective_sessions": 10,
        "minimum_sessions": plan_model_control.PLAN_MODELS[key]["minimum_sessions"],
        "data_ready": False,
    })
    monkeypatch.setattr(plan_model_control, "list_model_versions", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(plan_model_control, "get_active_model_version", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(plan_model_control, "_latest_run_for_model", lambda _key, runs=None: None)

    result = plan_model_control.model_family_status()

    assert result["ok"] is True
    assert result["counts"]["proxy"] == len(plan_model_control.PLAN_MODELS)
    assert {item["model_key"] for item in result["items"]} >= {
        "medium_horizon_lambdarank_v1",
        "capital_persistence_multitask_v1",
        "leader_lambdarank_v1",
    }


def test_request_unknown_plan_model_is_rejected():
    result = plan_model_control.request_plan_model_training("unknown")

    assert result["ok"] is False
    assert result["error"] == "unknown_plan_model"


def test_review_plan_model_approves_and_sets_active(monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(plan_model_control, "get_model_version", lambda _id: {
        "id": 11,
        "model_key": "medium_horizon_lambdarank_v1",
        "version": "medium_horizon_lambdarank_v1-test",
        "validation": {
            "status": "passed",
            "production_eligible": False,
            "production_eligibility": {
                "gates": {
                    "data_ready": True,
                    "rolling_oos": True,
                    "cost_after_backplay": True,
                    "baseline_compared": True,
                    "human_review": False,
                    "live_observation_20_sessions": False,
                }
            },
        },
    })
    monkeypatch.setattr(plan_model_control, "update_model_version_validation", lambda model_version_id, *, validation: captured.update({"model_version_id": model_version_id, "validation": validation}))
    monkeypatch.setattr(plan_model_control, "set_active_model_version", lambda model_version_id: captured.update({"active": model_version_id}))

    result = plan_model_control.review_plan_model_version(
        11,
        reviewer="alice",
        decision="approved",
        comment="模拟系统评审通过",
        waive_live_observation=True,
    )

    assert result["ok"] is True
    assert result["production_eligible"] is True
    assert captured["active"] == 11
    validation = captured["validation"]
    assert validation["production_eligibility"]["gates"]["human_review"] is True
    assert validation["production_eligibility"]["gates"]["simulation_only_live_observation_waived"] is True


def test_plan_model_scorer_returns_numeric_score(monkeypatch):
    monkeypatch.setattr(plan_model_control, "get_active_model_version", lambda _key: None)
    result = plan_model_control.score_plan_model_features(
        "medium_horizon_lambdarank_v1",
        {"momentum_60": 1.0, "volatility_20": 0.5, "capital_persistence": 0.8},
    )

    assert result["ok"] is True
    assert isinstance(result["score"], float)
    assert result["model_type"] == "plan_linear_ranker"


def test_plan_model_scorer_loads_lightgbm_artifact(tmp_path):
    model_path = tmp_path / "model.pkl"
    with model_path.open("wb") as handle:
        pickle.dump(DummyModel(), handle)

    result = plan_model_control.score_plan_model_features(
        "medium_horizon_lambdarank_v1",
        {"momentum_60": 0.75},
        artifact={
            "version": "medium-test",
            "features": plan_model_control.PLAN_MODEL_FEATURES,
            "model_path": str(model_path),
            "model_type": "plan_lightgbm_ranker",
        },
    )

    assert result["ok"] is True
    assert result["model_type"] == "plan_lightgbm_ranker"
    assert result["score"] == 1.0


def test_train_all_only_starts_trainable(monkeypatch):
    monkeypatch.setattr(plan_model_control, "model_family_status", lambda: {
        "items": [
            {"model_key": "medium_horizon_lambdarank_v1", "status": "trainable"},
            {"model_key": "leader_lambdarank_v1", "status": "proxy"},
        ]
    })
    monkeypatch.setattr(plan_model_control, "request_plan_model_training", lambda key: {"ok": True, "started": True, "model_key": key})

    result = plan_model_control.train_all_trainable_plan_models()

    assert result["started_count"] == 1
    assert result["skipped_count"] == 1
