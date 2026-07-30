"""Task-native evaluation contracts for ALIS production model families.

The model layer deliberately separates:

1. task validity (does the model solve the statistical task it was trained for?);
2. point-in-time/OOS integrity;
3. downstream strategy impact after costs.

A return backtest is never used as a substitute for a classifier, ranker,
quantile, event-study, or latent-state model's native evaluation.
"""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np
from scipy.stats import spearmanr
from sklearn.metrics import (
    balanced_accuracy_score,
    confusion_matrix,
    f1_score,
    log_loss,
    mean_absolute_error,
    mean_pinball_loss,
    mean_squared_error,
    precision_recall_fscore_support,
    roc_auc_score,
)
from sklearn.metrics import ndcg_score


SCHEMA_VERSION = "alis_task_native_model_evaluation_v1"

# These are implementation references, not invented alpha thresholds.  Gates
# below use task-matched naive/current-system baselines and held-out dominance.
SOURCE_REFERENCES: dict[str, dict[str, str]] = {
    "sklearn_metrics": {
        "title": "scikit-learn: Metrics and scoring",
        "url": "https://scikit-learn.org/stable/modules/model_evaluation.html",
    },
    "sklearn_calibration": {
        "title": "scikit-learn: Probability calibration",
        "url": "https://scikit-learn.org/stable/modules/calibration.html",
    },
    "lightgbm_ranking": {
        "title": "LightGBM parameters: LambdaRank and NDCG",
        "url": "https://lightgbm.readthedocs.io/en/latest/Parameters.html",
    },
    "scipy_spearman": {
        "title": "SciPy: Spearman rank-order correlation",
        "url": "https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.spearmanr.html",
    },
    "event_study": {
        "title": "MacKinlay (1997), Event Studies in Economics and Finance",
        "doi": "10.2307/2729691",
        "url": "https://www.bu.edu/econ/files/2011/01/MacKinlay-1996-Event-Studies-in-Economics-and-Finance.pdf",
    },
    "asset_pricing_ml": {
        "title": "Gu, Kelly and Xiu (2020), Empirical Asset Pricing via Machine Learning",
        "doi": "10.1093/rfs/hhaa009",
        "url": "https://academic.oup.com/rfs/article/33/5/2223/5758276",
    },
    "hsmm_review": {
        "title": "Yu (2010), Hidden semi-Markov models",
        "doi": "10.1016/j.artint.2009.11.011",
        "url": "https://www.sciencedirect.com/science/article/pii/S0004370209001416",
    },
    "hsmm_inference": {
        "title": "Melnyk and Banerjee (2017), A Spectral Algorithm for Inference in HSMMs",
        "url": "https://jmlr.org/papers/v18/15-468.html",
    },
    "time_series_split": {
        "title": "scikit-learn: TimeSeriesSplit",
        "url": "https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html",
    },
    "quantile_pinball": {
        "title": "scikit-learn: mean pinball loss",
        "url": "https://scikit-learn.org/stable/modules/generated/sklearn.metrics.mean_pinball_loss.html",
    },
}


MODEL_EVALUATION_CONTRACTS: dict[str, dict[str, Any]] = {
    "medium_horizon_lambdarank_v1": {
        "task": "daily_cross_sectional_learning_to_rank",
        "primary_metrics": ["ndcg@5", "ndcg@10", "ndcg@20", "spearman_rank_ic"],
        "task_baseline": "same_query_momentum_20_ranking",
        "downstream_gate": "frozen_strategy_ab_after_costs",
        "sources": ["lightgbm_ranking", "scipy_spearman", "asset_pricing_ml"],
    },
    "leader_lambdarank_v1": {
        "task": "within_theme_learning_to_rank",
        "primary_metrics": ["ndcg@1", "ndcg@3", "ndcg@5", "spearman_rank_ic"],
        "task_baseline": "same_theme_query_momentum_20_ranking",
        "downstream_gate": "frozen_strategy_ab_after_costs",
        "sources": ["lightgbm_ranking", "scipy_spearman"],
    },
    "capital_persistence_multitask_v1": {
        "task": "direction_classification_plus_quantiles_plus_failure_probability",
        "primary_metrics": [
            "direction_macro_f1", "direction_balanced_accuracy", "direction_log_loss",
            "direction_multiclass_brier", "quantile_pinball_q10_q50_q90",
            "q10_q90_interval_coverage", "failure_brier", "failure_log_loss", "failure_auc",
        ],
        "task_baseline": "current_persistence_plus_empirical_transition_and_unconditional_quantiles",
        "downstream_gate": "frozen_strategy_ab_after_costs",
        "sources": [
            "sklearn_metrics",
            "sklearn_calibration",
            "quantile_pinball",
            "time_series_split",
        ],
    },
    "event_surprise_lgbm_v1": {
        "task": "market_adjusted_event_abnormal_return_regression",
        "primary_metrics": ["mae", "rmse", "spearman_rank_ic", "sign_balanced_accuracy"],
        "task_baseline": "zero_abnormal_return_and_training_mean",
        "downstream_gate": "frozen_strategy_ab_after_costs",
        "sources": ["event_study", "sklearn_metrics", "scipy_spearman"],
    },
    "narrative_transition_multitask_v1": {
        "task": "two_stage_observable_lifecycle_transition_and_destination",
        "primary_metrics": [
            "transition_event_f1", "transition_event_brier",
            "conditional_destination_macro_f1",
            "final_state_macro_f1", "final_state_log_loss",
            "final_state_multiclass_brier", "per_class_recall",
        ],
        "task_baseline": "state_persistence_plus_train_only_transition_hazard_and_conditional_destination",
        "downstream_gate": "deterministic_lifecycle_rule_vs_rule_plus_model_shadow_ab",
        "sources": ["sklearn_metrics", "sklearn_calibration", "time_series_split"],
    },
    "market_regime_hsmm_v1": {
        "task": "explicit_duration_latent_market_regime_model",
        "primary_metrics": [
            "heldout_sequence_log_likelihood_per_observation",
            "heldout_likelihood_gain_over_hmm",
            "duration_log_score", "state_occupancy", "walk_forward_state_stability",
        ],
        "task_baseline": "same_emissions_geometric_duration_hmm",
        "downstream_gate": "transparent_state_machine_vs_hsmm_shadow_ab",
        "sources": ["hsmm_review", "hsmm_inference"],
    },
}


def evaluation_contract(model_key: str) -> dict[str, Any]:
    contract = dict(MODEL_EVALUATION_CONTRACTS.get(model_key) or {})
    return {
        "schema_version": SCHEMA_VERSION,
        "model_key": model_key,
        **contract,
        "references": {
            key: SOURCE_REFERENCES[key]
            for key in contract.get("sources") or []
            if key in SOURCE_REFERENCES
        },
        "layering_policy": {
            "task_native_gate_first": True,
            "point_in_time_oos_gate_required": True,
            "strategy_ab_is_separate": True,
            "task_metric_must_not_be_replaced_by_return_backtest": True,
        },
    }


def _finite(values: Any) -> np.ndarray:
    return np.asarray(values, dtype=float)


def _normalise_probabilities(probabilities: Any, *, class_count: int) -> np.ndarray:
    values = _finite(probabilities)
    if values.ndim != 2 or values.shape[1] != class_count:
        raise ValueError(f"probability shape must be (n,{class_count})")
    values = np.clip(values, 1e-12, 1.0)
    return values / np.maximum(values.sum(axis=1, keepdims=True), 1e-12)


def _multiclass_brier(y_true: np.ndarray, probabilities: np.ndarray, class_count: int) -> float:
    one_hot = np.eye(class_count, dtype=float)[y_true]
    return float(np.mean(np.sum((probabilities - one_hot) ** 2, axis=1)))


def _classification_metrics(y_true: Any, probabilities: Any, *, class_count: int) -> dict[str, Any]:
    actual = np.asarray(y_true, dtype=int)
    probs = _normalise_probabilities(probabilities, class_count=class_count)
    predicted = np.argmax(probs, axis=1)
    precision, recall, f1, support = precision_recall_fscore_support(
        actual,
        predicted,
        labels=list(range(class_count)),
        zero_division=0,
    )
    return {
        "accuracy": round(float(np.mean(predicted == actual)), 8),
        "balanced_accuracy": round(float(balanced_accuracy_score(actual, predicted)), 8),
        "macro_f1": round(float(f1_score(actual, predicted, average="macro", zero_division=0)), 8),
        "macro_recall": round(float(np.mean(recall)), 8),
        "log_loss": round(float(log_loss(actual, probs, labels=list(range(class_count)))), 8),
        "multiclass_brier": round(_multiclass_brier(actual, probs, class_count), 8),
        "confusion_matrix": confusion_matrix(
            actual,
            predicted,
            labels=list(range(class_count)),
        ).astype(int).tolist(),
        "per_class": [
            {
                "class_id": index,
                "precision": round(float(precision[index]), 8),
                "recall": round(float(recall[index]), 8),
                "f1": round(float(f1[index]), 8),
                "support": int(support[index]),
            }
            for index in range(class_count)
        ],
        "class_support_count": int(sum(int(value) > 0 for value in support)),
        "predicted": predicted,
    }


def _transition_matrix_probabilities(
    train_current: np.ndarray,
    train_future: np.ndarray,
    current: np.ndarray,
    *,
    class_count: int,
) -> np.ndarray:
    counts = np.ones((class_count, class_count), dtype=float)
    for left, right in zip(train_current, train_future):
        if 0 <= int(left) < class_count and 0 <= int(right) < class_count:
            counts[int(left), int(right)] += 1.0
    matrix = counts / counts.sum(axis=1, keepdims=True)
    return matrix[np.clip(current.astype(int), 0, class_count - 1)]


def _hard_probabilities(labels: np.ndarray, *, class_count: int, confidence: float = 0.99) -> np.ndarray:
    off = (1.0 - confidence) / max(1, class_count - 1)
    result = np.full((len(labels), class_count), off, dtype=float)
    result[np.arange(len(labels)), np.clip(labels.astype(int), 0, class_count - 1)] = confidence
    return result


def evaluate_narrative_transition(
    *,
    probabilities: Any,
    transition_probability: Any | None = None,
    destination_probabilities: Any | None = None,
    actual_future: Any,
    current_state: Any,
    train_current_state: Any,
    train_future_state: Any,
) -> dict[str, Any]:
    """Evaluate transition occurrence and destination as separate OOS tasks."""

    raw_probabilities = _finite(probabilities)
    if raw_probabilities.ndim != 2:
        raise ValueError("narrative probabilities must be a two-dimensional matrix")
    class_count = int(raw_probabilities.shape[1])
    actual = np.asarray(actual_future, dtype=int)
    current = np.asarray(current_state, dtype=int)
    train_current = np.asarray(train_current_state, dtype=int)
    train_future = np.asarray(train_future_state, dtype=int)
    model = _classification_metrics(
        actual,
        raw_probabilities,
        class_count=class_count,
    )
    persistence = _classification_metrics(
        actual,
        _hard_probabilities(current, class_count=class_count),
        class_count=class_count,
    )
    transition_probs = _transition_matrix_probabilities(
        train_current,
        train_future,
        current,
        class_count=class_count,
    )
    markov = _classification_metrics(actual, transition_probs, class_count=class_count)
    model_predicted = np.asarray(model.pop("predicted"), dtype=int)
    persistence.pop("predicted", None)
    markov.pop("predicted", None)
    actual_change = actual != current
    if transition_probability is None:
        transition_probability_values = np.asarray(
            model_predicted != current,
            dtype=float,
        )
    else:
        transition_probability_values = np.clip(
            _finite(transition_probability).reshape(-1),
            1e-9,
            1.0 - 1e-9,
        )
    if len(transition_probability_values) != len(actual):
        raise ValueError("transition_probability length does not match labels")
    predicted_change = transition_probability_values >= .5
    transition_f1 = float(f1_score(actual_change, predicted_change, zero_division=0))
    transition_recall = float(
        np.mean(predicted_change[actual_change])
        if bool(np.any(actual_change)) else 0.0
    )
    transition_brier = float(np.mean(
        (
            transition_probability_values
            - actual_change.astype(float)
        ) ** 2
    ))
    transition_log_loss = float(log_loss(
        actual_change.astype(int),
        np.column_stack([
            1.0 - transition_probability_values,
            transition_probability_values,
        ]),
        labels=[0, 1],
    ))
    hazard_counts = np.ones((class_count, 2), dtype=float)
    for left, right in zip(train_current, train_future):
        if 0 <= int(left) < class_count:
            hazard_counts[int(left), int(left != right)] += 1.0
    hazard_rates = hazard_counts[:, 1] / hazard_counts.sum(axis=1)
    baseline_transition_probability = hazard_rates[
        np.clip(current, 0, class_count - 1)
    ]
    baseline_transition_brier = float(np.mean(
        (
            baseline_transition_probability
            - actual_change.astype(float)
        ) ** 2
    ))
    baseline_transition_log_loss = float(log_loss(
        actual_change.astype(int),
        np.column_stack([
            1.0 - baseline_transition_probability,
            baseline_transition_probability,
        ]),
        labels=[0, 1],
    ))

    if destination_probabilities is None:
        destination_values = _normalise_probabilities(
            raw_probabilities,
            class_count=class_count,
        )
    else:
        destination_values = _normalise_probabilities(
            destination_probabilities,
            class_count=class_count,
        )
    changed_count = int(actual_change.sum())
    if changed_count:
        destination_model = _classification_metrics(
            actual[actual_change],
            destination_values[actual_change],
            class_count=class_count,
        )
        destination_model.pop("predicted", None)
        conditional_counts = np.ones((class_count, class_count), dtype=float)
        for left, right in zip(train_current, train_future):
            if (
                left != right
                and 0 <= int(left) < class_count
                and 0 <= int(right) < class_count
            ):
                conditional_counts[int(left), int(right)] += 1.0
        for state in range(class_count):
            conditional_counts[state, state] = 0.0
        conditional_counts /= np.maximum(
            conditional_counts.sum(axis=1, keepdims=True),
            1e-12,
        )
        destination_baseline = _classification_metrics(
            actual[actual_change],
            conditional_counts[
                np.clip(current[actual_change], 0, class_count - 1)
            ],
            class_count=class_count,
        )
        destination_baseline.pop("predicted", None)
    else:
        destination_model = {
            "macro_f1": 0.0,
            "log_loss": None,
            "multiclass_brier": None,
            "class_support_count": 0,
            "per_class": [],
        }
        destination_baseline = dict(destination_model)

    gates = {
        "class_coverage": int(model["class_support_count"]) >= 3,
        "transition_events_observed": changed_count > 0,
        "transition_detection_nonzero": transition_f1 > 0.0,
        "transition_brier_beats_train_only_hazard": (
            transition_brier < baseline_transition_brier
        ),
        "conditional_destination_coverage": (
            int(destination_model["class_support_count"]) >= 2
        ),
        "conditional_destination_macro_f1_beats_empirical": (
            float(destination_model["macro_f1"])
            > float(destination_baseline["macro_f1"])
        ),
        # Persistence is already the explicit no-transition baseline for
        # stage one. Requiring the joint argmax to beat it again would
        # double-count the same baseline and reward suppressing transitions.
        "final_state_macro_f1_beats_empirical_transition": (
            float(model["macro_f1"]) > float(markov["macro_f1"])
        ),
        "final_state_log_loss_beats_empirical_transition": (
            float(model["log_loss"]) < float(markov["log_loss"])
        ),
        "final_state_brier_beats_empirical_transition": (
            float(model["multiclass_brier"]) < float(markov["multiclass_brier"])
        ),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "contract": "narrative_two_stage_lifecycle_task_native_v2",
        "state_count": class_count,
        "model": model,
        "baselines": {
            "state_persistence": persistence,
            "empirical_transition_matrix_train_only": markov,
        },
        "transition_detection": {
            "event_count": changed_count,
            "predicted_event_count": int(predicted_change.sum()),
            "f1": round(transition_f1, 8),
            "recall": round(transition_recall, 8),
            "brier": round(transition_brier, 8),
            "log_loss": round(transition_log_loss, 8),
            "baseline_train_only_hazard_brier": round(
                baseline_transition_brier,
                8,
            ),
            "baseline_train_only_hazard_log_loss": round(
                baseline_transition_log_loss,
                8,
            ),
        },
        "conditional_destination": {
            "event_count": changed_count,
            "model": destination_model,
            "baseline_train_only_empirical_destination": destination_baseline,
        },
        "gates": gates,
        "passed": all(gates.values()),
        "baseline_outperformed": (
            gates["transition_brier_beats_train_only_hazard"]
            and gates["conditional_destination_macro_f1_beats_empirical"]
            and gates["final_state_macro_f1_beats_empirical_transition"]
            and gates["final_state_log_loss_beats_empirical_transition"]
            and gates["final_state_brier_beats_empirical_transition"]
        ),
        "evaluation_contract": evaluation_contract("narrative_transition_multitask_v1"),
    }


def _daily_quintile_labels(values: np.ndarray, dates: Iterable[str]) -> np.ndarray:
    labels = np.zeros(len(values), dtype=int)
    date_values = [str(value) for value in dates]
    for trade_date in sorted(set(date_values)):
        indices = np.asarray(
            [index for index, value in enumerate(date_values) if value == trade_date],
            dtype=int,
        )
        if len(indices) < 5:
            continue
        quantiles = np.quantile(values[indices], [0.2, 0.4, 0.6, 0.8])
        labels[indices] = np.digitize(values[indices], quantiles).astype(int)
    return labels


def evaluate_capital_multitask(
    *,
    direction_probabilities: Any,
    actual_direction: Any,
    current_persistence: Any,
    dates: Iterable[str],
    train_current_persistence: Any,
    train_dates: Iterable[str],
    train_future_direction: Any,
    actual_strength: Any,
    train_strength: Any,
    q10: Any,
    q50: Any,
    q90: Any,
    failure_probability: Any,
) -> dict[str, Any]:
    class_count = 5
    actual_class = np.asarray(actual_direction, dtype=int)
    current = _finite(current_persistence)
    current_class = _daily_quintile_labels(current, dates)
    train_current_class = _daily_quintile_labels(
        _finite(train_current_persistence),
        train_dates,
    )
    direction = _classification_metrics(
        actual_class,
        direction_probabilities,
        class_count=class_count,
    )
    baseline_probs = _transition_matrix_probabilities(
        train_current_class,
        np.asarray(train_future_direction, dtype=int),
        current_class,
        class_count=class_count,
    )
    direction_baseline = _classification_metrics(
        actual_class,
        baseline_probs,
        class_count=class_count,
    )
    direction.pop("predicted", None)
    direction_baseline.pop("predicted", None)

    actual = _finite(actual_strength)
    train = _finite(train_strength)
    quantile_predictions = {
        "q10": (0.10, _finite(q10)),
        "q50": (0.50, _finite(q50)),
        "q90": (0.90, _finite(q90)),
    }
    pinball: dict[str, dict[str, float]] = {}
    for key, (alpha, prediction) in quantile_predictions.items():
        baseline_value = float(np.quantile(train, alpha))
        model_loss = float(mean_pinball_loss(actual, prediction, alpha=alpha))
        baseline_loss = float(
            mean_pinball_loss(
                actual,
                np.full(len(actual), baseline_value, dtype=float),
                alpha=alpha,
            )
        )
        pinball[key] = {
            "alpha": alpha,
            "model": round(model_loss, 8),
            "unconditional_train_baseline": round(baseline_loss, 8),
            "improvement": round(baseline_loss - model_loss, 8),
        }
    lower = quantile_predictions["q10"][1]
    upper = quantile_predictions["q90"][1]
    interval_coverage = float(np.mean((actual >= lower) & (actual <= upper)))
    crossing_rate = float(np.mean(lower > upper))

    hazard_actual = (actual < 0).astype(int)
    hazard_probability = np.clip(_finite(failure_probability), 1e-12, 1 - 1e-12)
    base_probability = float(np.mean(_finite(train) < 0))
    baseline_hazard = np.full(len(actual), np.clip(base_probability, 1e-12, 1 - 1e-12))
    hazard_brier = float(np.mean((hazard_probability - hazard_actual) ** 2))
    baseline_brier = float(np.mean((baseline_hazard - hazard_actual) ** 2))
    hazard_logloss = float(log_loss(hazard_actual, hazard_probability, labels=[0, 1]))
    baseline_logloss = float(log_loss(hazard_actual, baseline_hazard, labels=[0, 1]))
    hazard_auc = (
        float(roc_auc_score(hazard_actual, hazard_probability))
        if len(set(hazard_actual.tolist())) == 2 else 0.5
    )
    gates = {
        "direction_macro_f1_beats_current_persistence": (
            float(direction["macro_f1"]) > float(direction_baseline["macro_f1"])
        ),
        "direction_log_loss_beats_empirical_transition": (
            float(direction["log_loss"]) < float(direction_baseline["log_loss"])
        ),
        "all_quantile_pinball_losses_improve": all(
            float(item["improvement"]) > 0 for item in pinball.values()
        ),
        "quantiles_do_not_cross": crossing_rate == 0.0,
        "failure_probability_brier_improves": hazard_brier < baseline_brier,
        "failure_probability_log_loss_improves": hazard_logloss < baseline_logloss,
        "failure_probability_discriminates": hazard_auc > 0.5,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "contract": "capital_persistence_multitask_task_native_v1",
        "direction": direction,
        "direction_baseline": direction_baseline,
        "quantiles": {
            "pinball": pinball,
            "q10_q90_interval_coverage": round(interval_coverage, 8),
            "nominal_interval_coverage": 0.8,
            "coverage_error": round(abs(interval_coverage - 0.8), 8),
            "crossing_rate": round(crossing_rate, 8),
        },
        "failure": {
            "brier": round(hazard_brier, 8),
            "baseline_brier": round(baseline_brier, 8),
            "log_loss": round(hazard_logloss, 8),
            "baseline_log_loss": round(baseline_logloss, 8),
            "roc_auc": round(hazard_auc, 8),
            "event_rate": round(float(np.mean(hazard_actual)), 8),
        },
        "gates": gates,
        "passed": all(gates.values()),
        "baseline_outperformed": all(
            gates[key]
            for key in (
                "direction_macro_f1_beats_current_persistence",
                "direction_log_loss_beats_empirical_transition",
                "all_quantile_pinball_losses_improve",
                "failure_probability_brier_improves",
                "failure_probability_log_loss_improves",
            )
        ),
        "evaluation_contract": evaluation_contract("capital_persistence_multitask_v1"),
    }


def _group_indices(groups: Iterable[str]) -> list[np.ndarray]:
    values = [str(value) for value in groups]
    return [
        np.asarray([index for index, value in enumerate(values) if value == group], dtype=int)
        for group in dict.fromkeys(values)
    ]


def evaluate_learning_to_rank(
    *,
    model_key: str,
    relevance: Any,
    model_scores: Any,
    baseline_scores: Any,
    groups: Iterable[str],
    eval_at: Iterable[int],
) -> dict[str, Any]:
    actual = _finite(relevance)
    model = _finite(model_scores)
    baseline = _finite(baseline_scores)
    indices_by_group = [indices for indices in _group_indices(groups) if len(indices) >= 2]
    if not indices_by_group:
        raise ValueError("learning-to-rank evaluation requires non-empty query groups")
    positions = sorted({max(1, int(value)) for value in eval_at})
    model_ndcg: dict[str, float] = {}
    baseline_ndcg: dict[str, float] = {}
    for position in positions:
        key = f"ndcg@{position}"
        model_values = [
            float(ndcg_score([actual[idx]], [model[idx]], k=min(position, len(idx))))
            for idx in indices_by_group
        ]
        baseline_values = [
            float(ndcg_score([actual[idx]], [baseline[idx]], k=min(position, len(idx))))
            for idx in indices_by_group
        ]
        model_ndcg[key] = round(float(np.mean(model_values)), 8)
        baseline_ndcg[key] = round(float(np.mean(baseline_values)), 8)
    rank_correlations = []
    for indices in indices_by_group:
        result = spearmanr(model[indices], actual[indices])
        value = float(result.statistic)
        rank_correlations.append(value if np.isfinite(value) else 0.0)
    mean_rank_ic = float(np.mean(rank_correlations))
    ndcg_improvements = {
        key: round(model_ndcg[key] - baseline_ndcg[key], 8)
        for key in model_ndcg
    }
    gates = {
        "all_ndcg_cutoffs_beat_task_baseline": all(
            value > 0 for value in ndcg_improvements.values()
        ),
        "spearman_rank_ic_positive": mean_rank_ic > 0,
        "query_coverage_nonempty": len(indices_by_group) > 0,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "contract": "grouped_learning_to_rank_task_native_v1",
        "model": {
            **model_ndcg,
            "spearman_rank_ic": round(mean_rank_ic, 8),
        },
        "baseline": baseline_ndcg,
        "improvement": ndcg_improvements,
        "query_count": len(indices_by_group),
        "gates": gates,
        "passed": all(gates.values()),
        "baseline_outperformed": gates["all_ndcg_cutoffs_beat_task_baseline"],
        "evaluation_contract": evaluation_contract(model_key),
    }


def evaluate_event_abnormal_return(
    *,
    predictions: Any,
    actual_abnormal_return: Any,
    train_abnormal_return: Any,
    target_contract: str,
) -> dict[str, Any]:
    actual = _finite(actual_abnormal_return)
    predicted = _finite(predictions)
    train = _finite(train_abnormal_return)
    zero = np.zeros(len(actual), dtype=float)
    train_mean = np.full(len(actual), float(np.mean(train)), dtype=float)
    model_mae = float(mean_absolute_error(actual, predicted))
    zero_mae = float(mean_absolute_error(actual, zero))
    mean_mae = float(mean_absolute_error(actual, train_mean))
    model_rmse = float(np.sqrt(mean_squared_error(actual, predicted)))
    zero_rmse = float(np.sqrt(mean_squared_error(actual, zero)))
    correlation = spearmanr(predicted, actual)
    rank_ic = float(correlation.statistic)
    if not np.isfinite(rank_ic):
        rank_ic = 0.0
    actual_sign = (actual > 0).astype(int)
    predicted_sign = (predicted > 0).astype(int)
    sign_balanced_accuracy = (
        float(balanced_accuracy_score(actual_sign, predicted_sign))
        if len(set(actual_sign.tolist())) == 2 else 0.5
    )
    gates = {
        "market_adjusted_abnormal_return_target": (
            target_contract == "market_adjusted_abnormal_return_v1"
        ),
        "mae_beats_zero_and_training_mean": model_mae < min(zero_mae, mean_mae),
        "rmse_beats_zero": model_rmse < zero_rmse,
        "spearman_rank_ic_positive": rank_ic > 0,
        "sign_balanced_accuracy_above_chance": sign_balanced_accuracy > 0.5,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "contract": "event_abnormal_return_task_native_v1",
        "target_contract": target_contract,
        "model": {
            "mae": round(model_mae, 8),
            "rmse": round(model_rmse, 8),
            "spearman_rank_ic": round(rank_ic, 8),
            "sign_balanced_accuracy": round(sign_balanced_accuracy, 8),
        },
        "baselines": {
            "zero_abnormal_return": {
                "mae": round(zero_mae, 8),
                "rmse": round(zero_rmse, 8),
            },
            "training_mean": {
                "value": round(float(np.mean(train)), 8),
                "mae": round(mean_mae, 8),
            },
        },
        "gates": gates,
        "passed": all(gates.values()),
        "baseline_outperformed": (
            gates["mae_beats_zero_and_training_mean"]
            and gates["rmse_beats_zero"]
        ),
        "evaluation_contract": evaluation_contract("event_surprise_lgbm_v1"),
    }


def supported_model_contracts() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "models": {
            key: evaluation_contract(key)
            for key in MODEL_EVALUATION_CONTRACTS
        },
        "policy": {
            "promotion_requires_task_native_oos": True,
            "promotion_requires_separate_strategy_ab": True,
            "generic_momentum_baseline_for_all_models_forbidden": True,
        },
    }
