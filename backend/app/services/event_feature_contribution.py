from __future__ import annotations

from collections import defaultdict
import hashlib
from typing import Any, Callable

import numpy as np

from app.services.model_evaluation_contracts import event_impact_target_labels


EVENT_FEATURE_CONTRIBUTION_AUDIT_CONTRACT = (
    "alis_event_family_market_state_feature_permutation_audit_v1"
)
EVENT_FEATURE_CONTRIBUTION_MINIMUM_GROUP_SUPPORT = 20
EVENT_FEATURE_CONTRIBUTION_REPEATS = 5


def _pinball_loss(actual: np.ndarray, prediction: np.ndarray, alpha: float) -> float:
    error = actual - prediction
    return float(np.mean(np.maximum(alpha * error, (alpha - 1.0) * error)))


def _event_output_losses(
    outputs: dict[str, np.ndarray],
    targets: dict[str, Any],
    abnormal_returns: np.ndarray,
    *,
    minimum_support: int,
) -> dict[str, dict[str, Any]]:
    labels = targets["labels"]
    masks = targets["observable_masks"]
    heads = {
        "actionability_brier": ("actionability", "positive_impact"),
        "priced_in_brier": ("priced_in", "priced_in"),
        "continuation_brier": ("continuation", "continuation"),
        "reversal_brier": ("reversal", "reversal"),
        "direction_realization_brier": (
            "direction_realization",
            "direction_realization",
        ),
    }
    result: dict[str, dict[str, Any]] = {}
    for metric, (label_key, output_key) in heads.items():
        observed = np.asarray(masks[label_key], dtype=bool)
        rows = int(observed.sum())
        if rows < minimum_support or output_key not in outputs:
            result[metric] = {"rows": rows, "loss": None}
            continue
        actual = np.asarray(labels[label_key], dtype=float)[observed]
        prediction = np.asarray(outputs[output_key], dtype=float)[observed]
        result[metric] = {
            "rows": rows,
            "loss": float(np.mean(np.square(prediction - actual))),
        }
    for output_key, alpha in (("q10", 0.10), ("q50", 0.50), ("q90", 0.90)):
        metric = f"{output_key}_pinball"
        rows = int(len(abnormal_returns))
        result[metric] = {
            "rows": rows,
            "loss": (
                _pinball_loss(
                    np.asarray(abnormal_returns, dtype=float),
                    np.asarray(outputs[output_key], dtype=float),
                    alpha,
                )
                if rows >= minimum_support and output_key in outputs
                else None
            ),
        }
    return result


def _permutation_seed(*parts: Any) -> int:
    raw = "|".join(str(part) for part in parts)
    return int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8], 16)


def fold_event_feature_permutation_audit(
    *,
    predict: Callable[[np.ndarray], dict[str, np.ndarray]] | None = None,
    predict_with_context: Callable[
        [np.ndarray, list[str], list[str], np.ndarray, np.ndarray],
        dict[str, np.ndarray],
    ] | None = None,
    values: np.ndarray,
    feature_names: list[str],
    abnormal_returns: np.ndarray,
    event_reaction: np.ndarray,
    event_direction_signal: np.ndarray,
    event_families: list[str],
    market_states: list[str],
    event_sources: list[str] | None = None,
    event_types: list[str] | None = None,
    fold: int,
    transaction_cost_rate: float,
    repeats: int = EVENT_FEATURE_CONTRIBUTION_REPEATS,
    minimum_support: int = EVENT_FEATURE_CONTRIBUTION_MINIMUM_GROUP_SUPPORT,
) -> dict[str, Any]:
    matrix = np.asarray(values, dtype=float)
    returns = np.asarray(abnormal_returns, dtype=float)
    reaction = np.asarray(event_reaction, dtype=float)
    direction = np.asarray(event_direction_signal, dtype=float)
    families = np.asarray([str(value or "other") for value in event_families])
    states = np.asarray([str(value or "unknown") for value in market_states])
    sources = np.asarray([
        str(value or "unknown")
        for value in (event_sources or ["unknown"] * len(matrix))
    ])
    types = np.asarray([
        str(value or "other")
        for value in (event_types or event_families)
    ])
    if predict is None and predict_with_context is None:
        raise ValueError("event feature contribution predictor is required")
    if matrix.ndim != 2 or matrix.shape[1] != len(feature_names):
        raise ValueError("event feature matrix does not match feature names")
    row_count = len(matrix)
    if not all(
        len(values_) == row_count
        for values_ in (returns, reaction, direction, families, states, sources, types)
    ):
        raise ValueError("event feature contribution vectors must have equal lengths")
    dimensions = {
        "event_family": families,
        "market_state": states,
        "event_family_x_market_state": np.asarray(
            [f"{family}|{state}" for family, state in zip(families, states)]
        ),
    }
    cells: dict[str, dict[str, Any]] = {}
    for dimension, groups in dimensions.items():
        cells[dimension] = {}
        for group in sorted(set(groups.tolist())):
            group_mask = groups == group
            rows = int(group_mask.sum())
            if rows < minimum_support:
                cells[dimension][group] = {
                    "rows": rows,
                    "supported": False,
                    "reason": "minimum_group_support_not_met",
                }
                continue
            group_values = matrix[group_mask]
            group_returns = returns[group_mask]
            group_reaction = reaction[group_mask]
            group_direction = direction[group_mask]
            group_sources = sources[group_mask].tolist()
            group_types = types[group_mask].tolist()
            def run_prediction(candidate_values: np.ndarray) -> dict[str, np.ndarray]:
                if predict_with_context is not None:
                    return predict_with_context(
                        candidate_values,
                        group_sources,
                        group_types,
                        group_reaction,
                        group_direction,
                    )
                assert predict is not None
                return predict(candidate_values)
            target_package = event_impact_target_labels(
                abnormal_return=group_returns,
                event_reaction=group_reaction,
                event_direction_signal=group_direction,
                transaction_cost_rate=transaction_cost_rate,
            )
            original_outputs = run_prediction(group_values)
            original_losses = _event_output_losses(
                original_outputs,
                target_package,
                group_returns,
                minimum_support=minimum_support,
            )
            contributions: dict[str, Any] = {}
            for feature_index, feature in enumerate(feature_names):
                feature_values = group_values[:, feature_index]
                varies = bool(np.ptp(feature_values) > 1e-12)
                repeat_deltas: dict[str, list[float]] = defaultdict(list)
                if varies:
                    for repeat in range(max(1, int(repeats))):
                        rng = np.random.default_rng(
                            _permutation_seed(fold, dimension, group, feature, repeat)
                        )
                        permuted = group_values.copy()
                        permuted[:, feature_index] = rng.permutation(feature_values)
                        permuted_losses = _event_output_losses(
                            run_prediction(permuted),
                            target_package,
                            group_returns,
                            minimum_support=minimum_support,
                        )
                        for metric, original in original_losses.items():
                            baseline_loss = original.get("loss")
                            changed_loss = (permuted_losses.get(metric) or {}).get("loss")
                            if baseline_loss is None or changed_loss is None:
                                continue
                            repeat_deltas[metric].append(
                                float(changed_loss) - float(baseline_loss)
                            )
                contributions[feature] = {
                    "varies_within_group": varies,
                    "metrics": {
                        metric: {
                            "observed_rows": int(original_losses[metric]["rows"]),
                            "repeat_count": len(deltas),
                            "mean_loss_increase_when_permuted": round(
                                float(np.mean(deltas)), 10
                            ),
                            "median_loss_increase_when_permuted": round(
                                float(np.median(deltas)), 10
                            ),
                            "positive_repeat_share": round(
                                float(np.mean(np.asarray(deltas) > 0)), 8
                            ),
                        }
                        for metric, deltas in repeat_deltas.items()
                        if deltas
                    },
                }
            cells[dimension][group] = {
                "rows": rows,
                "supported": True,
                "original_losses": original_losses,
                "feature_contributions": contributions,
            }
    return {
        "contract": EVENT_FEATURE_CONTRIBUTION_AUDIT_CONTRACT,
        "fold": int(fold),
        "rows": row_count,
        "repeats": max(1, int(repeats)),
        "minimum_group_support": int(minimum_support),
        "loss_delta_definition": "permuted_loss_minus_original_loss",
        "positive_delta_interpretation": "feature_helped_that_frozen_model_metric",
        "negative_delta_interpretation": "feature_hurt_or_was_used_unstably",
        "cells": cells,
    }


def _rank_feature_contributions(
    fold_audits: list[dict[str, Any]],
) -> dict[str, Any]:
    observations: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for fold_audit in fold_audits:
        fold = int(fold_audit["fold"])
        for dimension, groups in (fold_audit.get("cells") or {}).items():
            for group, cell in groups.items():
                if not bool(cell.get("supported")):
                    continue
                for feature, contribution in (
                    cell.get("feature_contributions") or {}
                ).items():
                    for metric, evidence in (
                        contribution.get("metrics") or {}
                    ).items():
                        observations[(dimension, group, metric, feature)].append({
                            "fold": fold,
                            "delta": float(
                                evidence["mean_loss_increase_when_permuted"]
                            ),
                            "rows": int(evidence["observed_rows"]),
                        })
    by_group: dict[str, Any] = defaultdict(lambda: defaultdict(dict))
    for (dimension, group, metric, feature), items in observations.items():
        deltas = np.asarray([item["delta"] for item in items], dtype=float)
        by_group[dimension][group].setdefault(metric, []).append({
            "feature": feature,
            "supported_fold_count": len({int(item["fold"]) for item in items}),
            "supported_folds": sorted({int(item["fold"]) for item in items}),
            "observed_rows": int(sum(int(item["rows"]) for item in items)),
            "mean_loss_increase_when_permuted": round(float(np.mean(deltas)), 10),
            "median_loss_increase_when_permuted": round(float(np.median(deltas)), 10),
            "positive_fold_share": round(float(np.mean(deltas > 0)), 8),
            "negative_fold_share": round(float(np.mean(deltas < 0)), 8),
        })
    result: dict[str, Any] = {}
    for dimension, groups in by_group.items():
        result[dimension] = {}
        for group, metrics in groups.items():
            result[dimension][group] = {}
            for metric, items in metrics.items():
                ranked = sorted(
                    items,
                    key=lambda item: (
                        float(item["median_loss_increase_when_permuted"]),
                        float(item["positive_fold_share"]),
                    ),
                    reverse=True,
                )
                result[dimension][group][metric] = {
                    "ranked_features": ranked,
                    "top_helpful": ranked[:10],
                    "top_harmful": sorted(
                        ranked,
                        key=lambda item: float(
                            item["median_loss_increase_when_permuted"]
                        ),
                    )[:10],
                }
    return result


def _feature_variation_support(
    fold_audits: list[dict[str, Any]],
) -> dict[str, Any]:
    support: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "supported_folds": set(),
            "varying_folds": set(),
            "supported_rows": 0,
        }
    )
    for fold_audit in fold_audits:
        fold = int(fold_audit["fold"])
        for dimension, groups in (fold_audit.get("cells") or {}).items():
            for group, cell in groups.items():
                if not bool(cell.get("supported")):
                    continue
                for feature, contribution in (
                    cell.get("feature_contributions") or {}
                ).items():
                    item = support[(dimension, group, feature)]
                    item["supported_folds"].add(fold)
                    item["supported_rows"] += int(cell.get("rows") or 0)
                    if bool(contribution.get("varies_within_group")):
                        item["varying_folds"].add(fold)
    result: dict[str, Any] = defaultdict(lambda: defaultdict(dict))
    for (dimension, group, feature), item in support.items():
        supported_folds = sorted(item["supported_folds"])
        varying_folds = sorted(item["varying_folds"])
        result[dimension][group][feature] = {
            "supported_fold_count": len(supported_folds),
            "supported_folds": supported_folds,
            "varying_fold_count": len(varying_folds),
            "varying_folds": varying_folds,
            "supported_rows": int(item["supported_rows"]),
            "not_estimable_reason": (
                "no_within_group_variation"
                if supported_folds and not varying_folds
                else None
            ),
        }
    return result


def aggregate_event_feature_permutation_audit(
    fold_audits: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "contract": EVENT_FEATURE_CONTRIBUTION_AUDIT_CONTRACT,
        "audit_only": True,
        "promotion_gate_impact": "none",
        "model_parameter_changes": False,
        "evaluation_gate_changes": False,
        "retraining": False,
        "synthetic_samples": False,
        "oversampling": False,
        "fold_count": len(fold_audits),
        "minimum_group_support": EVENT_FEATURE_CONTRIBUTION_MINIMUM_GROUP_SUPPORT,
        "permutation_repeats": EVENT_FEATURE_CONTRIBUTION_REPEATS,
        "metric_policy": (
            "每个概率头分别使用Brier loss，每个收益分位分别使用pinball loss；"
            "不合成任意总分，不定义新的晋升阈值"
        ),
        "interpretation_caveat": (
            "置换贡献描述冻结模型对特征的实际依赖；相关特征会分摊或替代贡献，"
            "正贡献不等于因果收益，负贡献也不能单独触发删除"
        ),
        "feature_variation_support": _feature_variation_support(fold_audits),
        "group_rankings": _rank_feature_contributions(fold_audits),
        "folds": fold_audits,
    }
