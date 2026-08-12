from app.services import decision_version_manifest


def test_decision_manifest_is_stable_and_freezes_all_component_versions(monkeypatch):
    monkeypatch.setattr(
        decision_version_manifest,
        "resolve_production_strategy_champion",
        lambda: {"champion_id": 50, "champion_version": "champion-v1", "strategy_id": 1},
    )
    monkeypatch.setattr(
        decision_version_manifest,
        "active_strategy_policy_context",
        lambda: {"strategy_policy_version_id": 2, "strategy_policy_version": "controller-v1"},
    )
    monkeypatch.setattr(
        decision_version_manifest,
        "get_active_model_version",
        lambda key: {"id": len(key), "version": f"{key}-v1", "status": "active"},
    )
    monkeypatch.setattr(
        decision_version_manifest,
        "active_factor_bindings",
        lambda: {"momentum_20": {"definition_id": 1, "version": "v1"}},
    )
    monkeypatch.setattr(
        decision_version_manifest,
        "active_execution_cost_contract",
        lambda: {"version": "cost-v1", "round_trip_rate": 0.004},
    )
    monkeypatch.setattr(
        decision_version_manifest,
        "active_release",
        lambda: {"id": 9, "release_key": "alis-test", "manifest_hash": "a" * 64, "status": "active"},
    )
    portfolio = {
        "optimizer": {
            "selected_method": "hrp",
            "method_competition": {"schema_version": "portfolio_method_competition_v1"},
        },
        "risk_budget": {"equity_budget": 0.8},
    }
    first = decision_version_manifest.build_decision_version_manifest(
        trade_date="20260722", portfolio=portfolio
    )
    second = decision_version_manifest.build_decision_version_manifest(
        trade_date="20260722", portfolio=portfolio
    )

    assert first == second
    assert first["strategy"]["model_version_id"] == 50
    assert first["strategy"]["version"] == "champion-v1"
    assert first["portfolio"]["selected_method"] == "hrp"
    assert first["execution_cost"]["version"] == "cost-v1"
    assert len(first["model_versions"]) == 5
    assert len(first["manifest_hash"]) == 64
    assert first["release"]["id"] == 9


def test_frozen_observation_only_models_do_not_fall_back_to_active_manifest(monkeypatch):
    monkeypatch.setattr(
        decision_version_manifest,
        "resolve_production_strategy_champion",
        lambda: {"champion_id": 50, "champion_version": "champion-v1", "strategy_id": 1},
    )
    monkeypatch.setattr(
        decision_version_manifest,
        "active_strategy_policy_context",
        lambda: {"strategy_policy_version_id": 2, "strategy_policy_version": "controller-v1"},
    )
    monkeypatch.setattr(
        decision_version_manifest,
        "get_active_model_version",
        lambda key: {"id": len(key), "version": f"{key}-v1", "status": "candidate"},
    )
    monkeypatch.setattr(decision_version_manifest, "active_factor_bindings", lambda: {})
    monkeypatch.setattr(decision_version_manifest, "active_execution_cost_contract", lambda: {})
    monkeypatch.setattr(decision_version_manifest, "active_release", lambda: None)
    portfolio = {
        "selection_snapshot": {
            "model_signal_snapshot": {
                "fusion": {
                    "production_models": [{
                        "model_key": "medium_horizon_lambdarank_v1",
                        "version_id": 7,
                        "version": "candidate-v7",
                        "consumed": False,
                    }],
                },
            },
        },
    }

    manifest = decision_version_manifest.build_decision_version_manifest(
        trade_date="20260722", portfolio=portfolio,
    )

    assert manifest["model_versions"] == {}
