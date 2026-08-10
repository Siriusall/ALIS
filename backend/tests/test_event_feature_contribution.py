import numpy as np

from app.services.event_feature_contribution import (
    EVENT_FEATURE_CONTRIBUTION_AUDIT_CONTRACT,
    aggregate_event_feature_permutation_audit,
    fold_event_feature_permutation_audit,
)


def _predict_from_first_feature(values: np.ndarray) -> dict[str, np.ndarray]:
    positive = np.where(values[:, 0] > 0, 0.9, 0.1)
    predicted_return = np.where(values[:, 0] > 0, 0.02, -0.02)
    rows = len(values)
    return {
        "positive_impact": positive,
        "priced_in": np.full(rows, 0.01),
        "continuation": np.full(rows, 0.9),
        "reversal": np.full(rows, 0.1),
        "direction_realization": np.full(rows, 0.9),
        "q10": predicted_return,
        "q50": predicted_return,
        "q90": predicted_return,
    }


def test_event_feature_permutation_audit_finds_held_out_signal_without_retraining():
    signal = np.asarray([-1.0, 1.0] * 20)
    values = np.column_stack([signal, np.arange(40, dtype=float) % 3])
    returns = np.where(signal > 0, 0.02, -0.02)
    kwargs = {
        "predict": _predict_from_first_feature,
        "values": values,
        "feature_names": ["signal", "noise"],
        "abnormal_returns": returns,
        "event_reaction": signal,
        "event_direction_signal": signal,
        "event_families": ["earnings"] * 40,
        "market_states": ["S1"] * 40,
        "fold": 1,
        "transaction_cost_rate": 0.004,
    }

    first = fold_event_feature_permutation_audit(**kwargs)
    second = fold_event_feature_permutation_audit(**kwargs)
    cell = first["cells"]["event_family"]["earnings"]
    signal_effect = cell["feature_contributions"]["signal"]["metrics"]
    noise_effect = cell["feature_contributions"]["noise"]["metrics"]

    assert first == second
    assert first["contract"] == EVENT_FEATURE_CONTRIBUTION_AUDIT_CONTRACT
    assert signal_effect["actionability_brier"][
        "mean_loss_increase_when_permuted"
    ] > 0
    assert signal_effect["q50_pinball"][
        "mean_loss_increase_when_permuted"
    ] > 0
    assert noise_effect["actionability_brier"][
        "mean_loss_increase_when_permuted"
    ] == 0

    aggregate = aggregate_event_feature_permutation_audit([first])
    ranking = aggregate["group_rankings"]["event_family"]["earnings"][
        "actionability_brier"
    ]["ranked_features"]
    assert ranking[0]["feature"] == "signal"
    assert aggregate["promotion_gate_impact"] == "none"
    assert aggregate["retraining"] is False
    variation = aggregate["feature_variation_support"]["event_family"][
        "earnings"
    ]
    assert variation["signal"]["varying_folds"] == [1]
    assert variation["noise"]["not_estimable_reason"] is None
