from app.services import model_evaluation_migration as migration


def test_current_evaluation_scrub_removes_only_legacy_top_level_metrics():
    payload = {
        "model_evaluation_contract": migration.SCHEMA_VERSION,
        "net_lift": 0.01,
        "baseline_metrics": {"model_minus_simple_momentum": 0.02},
        "task_metrics": {
            "contract": "grouped_learning_to_rank_v1",
            "model": {"spearman_rank_ic": 0.03},
        },
        "walk_forward_evidence": [{
            "fold": 1,
            "net_lift": 0.02,
            "task_native_evaluation": {"passed": True},
        }],
    }

    result = migration._sanitize_current_evaluation(payload)

    assert "net_lift" not in result
    assert "baseline_metrics" not in result
    assert "net_lift" not in result["walk_forward_evidence"][0]
    assert result["task_metrics"]["model"]["spearman_rank_ic"] == 0.03
    assert result["walk_forward_evidence"][0]["task_native_evaluation"] == {
        "passed": True
    }


def test_active_archive_marker_preserves_runtime_only_not_evaluation():
    marker = migration._archive_marker(
        archive_id="test-archive",
        active=True,
    )

    assert marker["runtime_compatibility_only"] is True
    assert marker["production_eligible"] is False
    assert marker["legacy_evaluation_removed"] is True
    assert marker["current_evaluation_contract"] == migration.SCHEMA_VERSION


def test_nested_runtime_evidence_scrub_removes_retired_protocol_only():
    payload = {
        "result": {
            "candidate_metrics": {
                "task_native_walk_forward_contract": "current-contract",
                "walk_forward_contract": "retired-contract",
                "economic_momentum_diagnostic": {"return": 0.1},
                "task_metrics": {"macro_f1": 0.4},
            },
        },
        "transaction_cost_bps": 12.0,
    }

    assert migration._contains_legacy_evaluation_fragment(payload) is True
    result = migration._sanitize_embedded_evaluation(payload)

    metrics = result["result"]["candidate_metrics"]
    assert metrics["task_native_walk_forward_contract"] == "current-contract"
    assert metrics["task_metrics"] == {"macro_f1": 0.4}
    assert "walk_forward_contract" not in metrics
    assert "economic_momentum_diagnostic" not in metrics
    assert result["transaction_cost_bps"] == 12.0
