from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

import numpy as np

from app.services.model_evaluation_contracts import evaluate_event_impact


EVENT_CONDITIONAL_FAILURE_AUDIT_CONTRACT = (
    "alis_event_family_market_state_conditional_failure_audit_v1"
)
EVENT_CONDITIONAL_FAILURE_MINIMUM_GROUP_SUPPORT = 20


def _subset(values: Any, mask: np.ndarray) -> Any:
    array = np.asarray(values)
    return array[mask]


def fold_event_conditional_failure_audit(
    *,
    outputs: dict[str, np.ndarray],
    actual_abnormal_return: np.ndarray,
    train_abnormal_return: np.ndarray,
    event_reaction: np.ndarray,
    train_event_reaction: np.ndarray,
    event_direction_signal: np.ndarray,
    train_event_direction_signal: np.ndarray,
    event_sources: list[str],
    train_event_sources: list[str],
    event_families: list[str],
    train_event_families: list[str],
    market_states: list[str],
    fold: int,
    transaction_cost_rate: float,
    target_contract: str,
    formal_evaluation: dict[str, Any] | None = None,
    minimum_support: int = EVENT_CONDITIONAL_FAILURE_MINIMUM_GROUP_SUPPORT,
) -> dict[str, Any]:
    """Measure frozen model failures inside naturally occurring OOS cells.

    Every cell uses the same full training-only conditional baseline as the
    formal evaluation.  Test labels never select a model, threshold or sample.
    """

    returns = np.asarray(actual_abnormal_return, dtype=float)
    reaction = np.asarray(event_reaction, dtype=float)
    direction = np.asarray(event_direction_signal, dtype=float)
    sources = np.asarray([str(value or "unknown") for value in event_sources])
    families = np.asarray([str(value or "other") for value in event_families])
    states = np.asarray([str(value or "unknown") for value in market_states])
    row_count = len(returns)
    if not all(len(values) == row_count for values in (reaction, direction, sources, families, states)):
        raise ValueError("event conditional failure vectors must have equal lengths")
    required_outputs = {
        "positive_impact", "priced_in", "continuation", "reversal",
        "direction_realization", "q10", "q50", "q90",
    }
    if set(outputs) < required_outputs:
        raise ValueError("event conditional failure outputs are incomplete")
    if any(len(np.asarray(outputs[name])) != row_count for name in required_outputs):
        raise ValueError("event conditional failure output lengths do not match")

    dimensions = {
        "event_family": families,
        "market_state": states,
        "event_family_x_market_state": np.asarray([
            f"{family}|{state}" for family, state in zip(families, states)
        ]),
    }
    cells: dict[str, dict[str, Any]] = {}
    for dimension, groups in dimensions.items():
        cells[dimension] = {}
        for group in sorted(set(groups.tolist())):
            mask = groups == group
            rows = int(mask.sum())
            if rows < int(minimum_support):
                cells[dimension][group] = {
                    "rows": rows,
                    "supported": False,
                    "reason": "minimum_group_support_not_met",
                }
                continue
            evaluation = evaluate_event_impact(
                positive_impact_probability=_subset(outputs["positive_impact"], mask),
                priced_in_probability=_subset(outputs["priced_in"], mask),
                continuation_probability=_subset(outputs["continuation"], mask),
                reversal_probability=_subset(outputs["reversal"], mask),
                direction_realization_probability=_subset(
                    outputs["direction_realization"], mask,
                ),
                q10=_subset(outputs["q10"], mask),
                q50=_subset(outputs["q50"], mask),
                q90=_subset(outputs["q90"], mask),
                actual_abnormal_return=returns[mask],
                train_abnormal_return=np.asarray(train_abnormal_return, dtype=float),
                event_reaction=reaction[mask],
                train_event_reaction=np.asarray(train_event_reaction, dtype=float),
                event_direction_signal=direction[mask],
                train_event_direction_signal=np.asarray(
                    train_event_direction_signal, dtype=float,
                ),
                event_sources=sources[mask].tolist(),
                train_event_sources=[str(value or "unknown") for value in train_event_sources],
                event_types=families[mask].tolist(),
                train_event_types=[str(value or "other") for value in train_event_families],
                transaction_cost_rate=float(transaction_cost_rate),
                target_contract=str(target_contract),
            )
            cells[dimension][group] = {
                "rows": rows,
                "supported": True,
                "evaluation": evaluation,
            }
    return {
        "contract": EVENT_CONDITIONAL_FAILURE_AUDIT_CONTRACT,
        "fold": int(fold),
        "rows": row_count,
        "minimum_group_support": int(minimum_support),
        "formal_evaluation": dict(formal_evaluation or {}),
        "cells": cells,
    }


def aggregate_event_conditional_failure_audit(
    fold_audits: list[dict[str, Any]],
) -> dict[str, Any]:
    observations: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for fold_audit in fold_audits:
        fold = int(fold_audit.get("fold") or 0)
        for dimension, groups in (fold_audit.get("cells") or {}).items():
            for group, cell in groups.items():
                if not bool(cell.get("supported")):
                    continue
                observations[(dimension, group)].append({
                    "fold": fold,
                    "rows": int(cell.get("rows") or 0),
                    "evaluation": dict(cell.get("evaluation") or {}),
                })

    groups: dict[str, dict[str, Any]] = defaultdict(dict)
    probability_heads = (
        "actionability", "priced_in", "continuation", "reversal",
        "direction_realization",
    )
    quantile_heads = ("q10", "q50", "q90")
    for (dimension, group), items in observations.items():
        head_summary: dict[str, Any] = {}
        for head in probability_heads:
            values = [
                (item["evaluation"].get("probability_heads") or {}).get(head) or {}
                for item in items
            ]
            improvements = np.asarray([
                float(value.get("brier_improvement") or 0.0) for value in values
            ])
            head_summary[head] = {
                "supported_fold_count": len(items),
                "positive_brier_improvement_folds": int(np.sum(improvements > 0)),
                "median_brier_improvement": round(float(np.median(improvements)), 10),
                "mean_brier_improvement": round(float(np.mean(improvements)), 10),
                "observed_rows": int(sum(int(value.get("observed_rows") or 0) for value in values)),
            }
        quantile_summary: dict[str, Any] = {}
        for quantile in quantile_heads:
            values = [
                (((item["evaluation"].get("quantiles") or {}).get("pinball") or {}).get(quantile) or {})
                for item in items
            ]
            improvements = np.asarray([
                float(value.get("improvement") or 0.0) for value in values
            ])
            quantile_summary[quantile] = {
                "supported_fold_count": len(items),
                "positive_pinball_improvement_folds": int(np.sum(improvements > 0)),
                "median_pinball_improvement": round(float(np.median(improvements)), 10),
                "mean_pinball_improvement": round(float(np.mean(improvements)), 10),
            }
        failure_counts: Counter[str] = Counter()
        for item in items:
            gates = (item["evaluation"].get("gates") or {})
            failure_counts.update(name for name, passed in gates.items() if not bool(passed))
        groups[dimension][group] = {
            "supported_fold_count": len(items),
            "supported_folds": sorted(int(item["fold"]) for item in items),
            "rows": int(sum(int(item["rows"]) for item in items)),
            "passed_folds": int(sum(bool(item["evaluation"].get("passed")) for item in items)),
            "baseline_outperformed_folds": int(sum(
                bool(item["evaluation"].get("baseline_outperformed")) for item in items
            )),
            "probability_heads": head_summary,
            "quantile_heads": quantile_summary,
            "failed_gate_counts": dict(sorted(failure_counts.items())),
        }
    return {
        "contract": EVENT_CONDITIONAL_FAILURE_AUDIT_CONTRACT,
        "audit_only": True,
        "promotion_gate_impact": "none",
        "model_parameter_changes": False,
        "evaluation_gate_changes": False,
        "retraining": False,
        "synthetic_samples": False,
        "oversampling": False,
        "fold_count": len(fold_audits),
        "minimum_group_support": EVENT_CONDITIONAL_FAILURE_MINIMUM_GROUP_SUPPORT,
        "baseline_policy": (
            "每个自然OOS单元继续使用正式训练集的来源×事件族×市场反应强基线"
        ),
        "interpretation_caveat": (
            "少于三个自然测试窗的事件族或市场状态只能描述，不能据此形成稳定性结论"
        ),
        "group_summary": groups,
        "folds": fold_audits,
    }
