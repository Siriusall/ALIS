from app.services.model_evidence_contract import evidence_binding_registry, normalize_model_output
from app.services.training_protocol import validate_feature_frame_contract, validate_training_run_manifest
from app.services.two_group_optimizer import two_group_cvar_optimizer


def test_training_protocol_blocks_missing_purge_and_embargo():
    result = validate_training_run_manifest({
        "model_key": "medium_horizon_lambdarank_v1",
        "calendar": {"train_sessions": 756, "validation_sessions": 126, "test_sessions": 126},
        "oos_windows": 5,
        "cost_after_labels": True,
        "baseline_compared": True,
    })

    assert result["ok"] is False
    assert "purge_sessions" in result["blocked_reasons"]
    assert "embargo_sessions" in result["blocked_reasons"]


def test_feature_contract_requires_source_time_and_blocks_low_coverage():
    result = validate_feature_frame_contract(
        [
            {"available_time": "2026-07-19T15:30:00", "source_as_of_time": "2026-07-19T15:00:00", "x": 1.0, "y": None},
            {"available_time": "2026-07-20T15:30:00", "source_as_of_time": "2026-07-20T15:00:00", "x": 2.0, "y": None},
        ],
        feature_names=["x", "y"],
    )

    assert result["ok"] is False
    assert result["coverage"]["x"] == 1.0
    assert "y" in result["blocked_features"]
    assert "y_missing" in result["missing_indicators_required"]


def test_evidence_binding_registry_maps_contract_fields_to_models():
    result = evidence_binding_registry()

    assert result["ok"] is True
    assert result["bindings"]["funding_confirmation"]["primary"] == "capital_persistence_multitask_v1"
    assert "narrative_transition_multitask_v1" in result["bindings"]["exit_conditions"]["primary"]


def test_model_output_normalization_enforces_required_fields():
    result = normalize_model_output("capital_persistence_multitask_v1", {
        "direction_probability_3d": .62,
        "direction_probability_5d": .60,
        "flow_ratio_p50": .03,
    })

    assert result["ok"] is False
    assert "flow_ratio_p10" in result["missing_fields"]
    assert result["fallback"] == "capital_persistence_proxy_v1"


def test_two_group_optimizer_returns_explainable_weights():
    returns = {
        "511880.SH": [0.0001, 0.0002, 0.0001, 0.0003],
        "000001.SZ": [0.01, -0.02, 0.015, -0.005],
        "000002.SZ": [0.012, -0.018, 0.011, -0.004],
        "000003.SZ": [0.02, -0.015, 0.01, -0.012],
        "000004.SZ": [0.018, -0.02, 0.014, -0.01],
        "000005.SZ": [0.016, -0.017, 0.013, -0.009],
        "000006.SZ": [0.014, -0.016, 0.012, -0.008],
    }
    buckets = {"511880.SH": "defensive", **{symbol: "narrative" for symbol in returns if symbol != "511880.SH"}}
    result = two_group_cvar_optimizer(returns, buckets=buckets)

    assert result["schema_version"] == "portfolio_cvar_optimizer_v2"
    assert result["ok"] is True
    assert result["status"] in {"optimal", "fallback"}
    assert abs(result["weight_sum"] - 1.0) < 1e-6
    assert result["explanations"]["511880.SH"]["function_bucket"] == "defensive"
    assert "constraint_shadow_price" in result["explanations"]["000001.SZ"]
    assert "stable_group_45_55" not in result["gates"]
    assert "offensive_group_45_55" not in result["gates"]
