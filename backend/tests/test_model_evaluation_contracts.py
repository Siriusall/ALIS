import numpy as np
import pytest

from app.services.model_evaluation_contracts import (
    SCHEMA_VERSION,
    evaluate_capital_multitask,
    evaluate_event_impact,
    evaluate_learning_to_rank,
    evaluate_narrative_transition,
    event_impact_target_labels,
    event_response_group_keys,
    fit_event_probability_baseline,
    apply_event_probability_baseline,
    fit_event_quantile_baseline,
    apply_event_quantile_baseline,
    supported_model_contracts,
)


def _hard_probabilities(labels: np.ndarray, class_count: int = 5) -> np.ndarray:
    probabilities = np.full((len(labels), class_count), 0.001, dtype=float)
    probabilities[np.arange(len(labels)), labels] = 0.996
    return probabilities


def test_public_event_baseline_fit_apply_matches_frozen_group_contract():
    groups = event_response_group_keys(
        ["announcement"] * 4,
        [0.1, 0.1, -0.1, -0.1],
        [1.0, 1.0, -1.0, -1.0],
        ["earnings"] * 4,
    )
    probability = fit_event_probability_baseline([1, 0, 0, 0], groups)
    applied_probability = apply_event_probability_baseline(probability, groups)
    assert applied_probability.tolist() == [0.5, 0.5, 0.25, 0.25]

    quantile = fit_event_quantile_baseline(
        np.arange(20, dtype=float),
        ["stable"] * 10 + ["other"] * 10,
        0.5,
    )
    applied_quantile = apply_event_quantile_baseline(
        quantile, ["stable", "missing"],
    )
    assert applied_quantile[0] == 4.5
    assert applied_quantile[1] == 9.5


def test_narrative_uses_lifecycle_baselines_not_return_or_momentum():
    actual = np.asarray([1, 2, 3, 0] * 5, dtype=int)
    current = np.asarray([0, 1, 2, 3] * 5, dtype=int)
    train_current = np.asarray([0, 1, 2, 3] * 20, dtype=int)
    train_future = train_current.copy()

    result = evaluate_narrative_transition(
        probabilities=_hard_probabilities(actual, class_count=4),
        transition_probability=np.full(len(actual), 0.999),
        destination_probabilities=_hard_probabilities(actual, class_count=4),
        actual_future=actual,
        current_state=current,
        train_current_state=train_current,
        train_future_state=train_future,
    )

    assert result["passed"] is True
    assert result["contract"] == "narrative_two_stage_lifecycle_task_native_v3"
    assert set(result["baselines"]) == {
        "state_persistence",
        "empirical_transition_matrix_train_only",
    }
    assert result["transition_detection"]["f1"] == 1.0
    assert result["evaluation_contract"]["task_baseline"] == (
        "strongest_of_state_persistence_and_train_only_transition_hazard_and_conditional_destination"
    )


def test_narrative_high_accuracy_on_imbalanced_persistence_is_not_a_pass():
    current = np.zeros(100, dtype=int)
    actual = np.asarray([0] * 97 + [1] * 3, dtype=int)

    result = evaluate_narrative_transition(
        probabilities=_hard_probabilities(current),
        transition_probability=np.zeros(len(actual)),
        destination_probabilities=_hard_probabilities(current),
        actual_future=actual,
        current_state=current,
        train_current_state=np.zeros(100, dtype=int),
        train_future_state=np.zeros(100, dtype=int),
    )

    assert result["model"]["accuracy"] == 0.97
    assert result["passed"] is False
    assert result["gates"]["class_coverage"] is False
    assert result["gates"]["final_state_macro_f1_beats_state_persistence"] is False
    assert result["gates"]["final_state_macro_f1_beats_empirical_transition"] is False


def test_rankers_use_grouped_ndcg_and_spearman_against_same_query_baseline():
    relevance = np.asarray([0, 1, 2, 3, 4, 0, 1, 2, 3, 4], dtype=float)
    result = evaluate_learning_to_rank(
        model_key="medium_horizon_lambdarank_v1",
        relevance=relevance,
        model_scores=relevance,
        baseline_scores=-relevance,
        groups=["20260101"] * 5 + ["20260102"] * 5,
        eval_at=[5, 10, 20],
    )

    assert result["passed"] is True
    assert result["query_count"] == 2
    assert result["model"]["spearman_rank_ic"] == 1.0
    assert result["model"]["ndcg@5"] > result["baseline"]["ndcg@5"]


def test_capital_multitask_evaluates_all_three_heads_against_task_baselines():
    actual_direction = np.asarray([0, 1, 2, 3, 4] * 4, dtype=int)
    actual_strength = np.linspace(-1.0, 1.0, len(actual_direction))
    train_strength = np.linspace(-1.5, 1.5, 100)
    train_dates = [f"d{index // 5:02d}" for index in range(100)]
    train_current = np.tile(np.arange(5, dtype=float), 20)
    train_future = np.tile(np.asarray([4, 3, 2, 1, 0], dtype=int), 20)
    test_dates = [f"t{index // 5:02d}" for index in range(20)]

    result = evaluate_capital_multitask(
        direction_probabilities=_hard_probabilities(actual_direction),
        actual_direction=actual_direction,
        current_persistence=np.tile(np.arange(5, dtype=float), 4),
        dates=test_dates,
        train_current_persistence=train_current,
        train_dates=train_dates,
        train_future_direction=train_future,
        actual_strength=actual_strength,
        train_strength=train_strength,
        q10=actual_strength,
        q50=actual_strength,
        q90=actual_strength,
        failure_probability=np.where(actual_strength < 0, 0.999, 0.001),
    )

    assert result["passed"] is True
    assert set(result["quantiles"]["pinball"]) == {"q10", "q50", "q90"}
    assert result["failure"]["brier"] < result["failure"]["baseline_brier"]
    assert result["direction"]["macro_f1"] > result["direction_baseline"]["macro_f1"]


def test_event_model_uses_conditional_impact_heads_and_strong_train_only_baselines():
    actual = np.tile(
        np.asarray([-0.04, -0.01, 0.02, 0.05], dtype=float),
        5,
    )
    train = np.tile(np.asarray([
        -0.03, 0.03, -0.02, 0.02,
        -0.04, 0.04, -0.01, 0.01,
    ], dtype=float), 4)
    kwargs = {
        "positive_impact_probability": np.tile(
            np.asarray([0.001, 0.001, 0.999, 0.999]), 5,
        ),
        "priced_in_probability": np.full(20, 0.001),
        "continuation_probability": np.full(20, 0.999),
        "reversal_probability": np.full(20, 0.001),
        "direction_realization_probability": np.full(20, 0.999),
        "q10": actual,
        "q50": actual,
        "q90": actual,
        "actual_abnormal_return": actual,
        "train_abnormal_return": train,
        "event_reaction": np.asarray([-1.0, -1.0, 1.0, 1.0] * 5),
        "train_event_reaction": np.asarray([-1.0, -1.0, 1.0, 1.0] * 8),
        "event_direction_signal": np.asarray([-1.0, -1.0, 1.0, 1.0] * 5),
        "train_event_direction_signal": np.asarray([-1.0, -1.0, 1.0, 1.0] * 8),
        "event_sources": ["announcement", "major_news"] * 10,
        "train_event_sources": ["announcement", "major_news"] * 16,
        "event_types": ["earnings", "contract_operation"] * 10,
        "train_event_types": ["earnings", "contract_operation"] * 16,
        "transaction_cost_rate": 0.004,
    }

    invalid = evaluate_event_impact(
        **kwargs,
        target_contract="raw_future_return_v1",
    )
    valid = evaluate_event_impact(
        **kwargs,
        target_contract="conditional_market_adjusted_event_impact_v3",
    )

    assert invalid["passed"] is False
    assert invalid["gates"]["conditional_market_adjusted_event_impact_target"] is False
    assert valid["passed"] is True
    assert valid["contract"] == "conditional_event_impact_task_native_v3"
    assert set(valid["probability_heads"]) == {
        "actionability", "priced_in", "continuation", "reversal",
        "direction_realization",
    }
    assert valid["baselines"]["contract"] == (
        "train_only_source_x_event_type_x_market_response_priors_and_quantiles_v3"
    )
    assert valid["target_definition"]["direction_magnitude_multiplication_forbidden"] is True
    assert "mae" not in str(valid).lower()


def test_event_model_rejects_misaligned_or_nonfinite_evaluation_inputs():
    values = {
        "positive_impact_probability": [0.6, 0.4],
        "priced_in_probability": [0.2, 0.3],
        "continuation_probability": [0.7, 0.2],
        "reversal_probability": [0.1, 0.5],
        "direction_realization_probability": [0.7, 0.2],
        "q10": [-0.02, -0.01],
        "q50": [0.01, 0.0],
        "q90": [0.04, 0.03],
        "actual_abnormal_return": [0.02, -0.01],
        "train_abnormal_return": [0.01, -0.02, 0.03],
        "event_reaction": [0.01, -0.02],
        "train_event_reaction": [0.01, -0.01, 0.02],
        "event_direction_signal": [1.0, -1.0],
        "train_event_direction_signal": [1.0, -1.0, 1.0],
        "event_sources": ["major_news", "announcement"],
        "train_event_sources": ["major_news", "announcement", "major_news"],
        "transaction_cost_rate": 0.004,
        "target_contract": "conditional_market_adjusted_event_impact_v3",
    }

    with pytest.raises(ValueError, match="evaluation event vectors"):
        evaluate_event_impact(**{**values, "q90": [0.04]})
    with pytest.raises(ValueError, match="contains NaN or infinity"):
        evaluate_event_impact(
            **{**values, "positive_impact_probability": [0.6, float("nan")]}
        )


def test_event_direction_labels_use_sign_not_direction_magnitude_and_exclude_flat_rows():
    labels = event_impact_target_labels(
        abnormal_return=[0.01, 0.01, -0.01, 0.02],
        event_reaction=[0.01, 10.0, -0.01, 0.0],
        event_direction_signal=[0.01, 10.0, -0.01, 0.0],
        transaction_cost_rate=0.004,
    )

    assert labels["labels"]["continuation"].tolist() == [1, 1, 1, 0]
    assert labels["labels"]["reversal"].tolist() == [0, 0, 0, 0]
    assert labels["labels"]["direction_realization"].tolist() == [1, 1, 1, 0]
    assert labels["observable_masks"]["continuation"].tolist() == [True, True, True, False]
    assert labels["observable_masks"]["direction_realization"].tolist() == [True, True, True, False]
    assert labels["direction_magnitude_multiplication_forbidden"] is True


def test_contract_catalog_separates_task_oos_from_strategy_ab():
    catalog = supported_model_contracts()

    assert catalog["schema_version"] == SCHEMA_VERSION
    assert set(catalog["models"]) == {
        "medium_horizon_lambdarank_v1",
        "capital_persistence_multitask_v1",
        "leader_lambdarank_v1",
        "event_impact_assessor_v2",
        "narrative_transition_multitask_v1",
        "market_regime_hsmm_v1",
    }
    assert catalog["policy"]["promotion_requires_task_native_oos"] is True
    assert catalog["policy"]["promotion_requires_separate_strategy_ab"] is True
    assert catalog["policy"]["generic_momentum_baseline_for_all_models_forbidden"] is True
