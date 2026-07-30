from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any

from app.services.model_evaluation_contracts import (
    SCHEMA_VERSION as MODEL_EVALUATION_CONTRACT,
    evaluation_contract,
)


TRAINING_PROTOCOL_V1: dict[str, Any] = {
    "schema_version": "alis_task_native_training_protocol_v1",
    "calendar": {
        "minimum_train_sessions": 756,
        "validation_sessions": 126,
        "test_sessions": 126,
        "roll_step_sessions": 126,
        "minimum_oos_windows": 5,
        "purge_sessions": 60,
        "embargo_sessions": 5,
    },
    "point_in_time": {
        "required_fields": ["available_time", "source_as_of_time"],
        "decision_lag": "signal_t_trade_t_plus_1",
        "future_data_forbidden": True,
    },
    "feature_quality": {
        "minimum_coverage": 0.80,
        "native_missing_branch": True,
        "missing_indicator_suffix": "_missing",
        "strict_json": True,
        "reject_nan_infinity": True,
    },
    "calibration": {
        "default": "platt_sigmoid",
        "isotonic_allowed_when": "calibration_rows_gt_1000_and_oos_brier_improves",
        "fit_on_training_rows": False,
    },
    "baseline": {
        "required": True,
        "selection": "task_native_per_model",
        "promotion_rule": (
            "model_must_beat_same_task_baseline_in_majority_purged_oos_windows"
        ),
        "generic_momentum_baseline_for_all_models_forbidden": True,
    },
    "model_evaluation": {
        "contract": MODEL_EVALUATION_CONTRACT,
        "task_native_metrics_required": True,
        "purged_walk_forward_required": True,
        "controlled_strategy_ab_required": True,
        "task_and_strategy_evidence_separate": True,
    },
}


def strict_json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _is_bad_number(value: Any) -> bool:
    return isinstance(value, float) and (math.isnan(value) or math.isinf(value))


def validate_strict_json_payload(payload: Any, *, path: str = "$") -> dict[str, Any]:
    errors: list[str] = []

    def walk(value: Any, current: str) -> None:
        if _is_bad_number(value):
            errors.append(f"{current}:nan_or_infinity")
        elif isinstance(value, dict):
            for key, child in value.items():
                walk(child, f"{current}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                walk(child, f"{current}[{index}]")

    walk(payload, path)
    if not errors:
        try:
            strict_json_dumps(payload)
        except ValueError as exc:
            errors.append(str(exc))
    return {"ok": not errors, "errors": errors, "policy": "strict_json_allow_nan_false"}


def validate_feature_frame_contract(
    rows: list[dict[str, Any]],
    *,
    feature_names: list[str],
    require_source_time: bool = True,
    minimum_coverage: float | None = None,
) -> dict[str, Any]:
    min_coverage = float(minimum_coverage if minimum_coverage is not None else TRAINING_PROTOCOL_V1["feature_quality"]["minimum_coverage"])
    total = len(rows)
    coverage: dict[str, float] = {}
    missing_indicators: list[str] = []
    blocked_features: list[str] = []
    errors: list[str] = []
    for feature in feature_names:
        present = sum(1 for row in rows if row.get(feature) is not None)
        ratio = present / total if total else 0.0
        coverage[feature] = round(ratio, 6)
        missing_name = f"{feature}{TRAINING_PROTOCOL_V1['feature_quality']['missing_indicator_suffix']}"
        if total and present < total:
            missing_indicators.append(missing_name)
        if ratio < min_coverage:
            blocked_features.append(feature)
    if require_source_time:
        for index, row in enumerate(rows):
            if not row.get("available_time"):
                errors.append(f"row[{index}].available_time_missing")
            if not row.get("source_as_of_time"):
                errors.append(f"row[{index}].source_as_of_time_missing")
    for index, row in enumerate(rows):
        result = validate_strict_json_payload(row, path=f"row[{index}]")
        errors.extend(result["errors"])
    return {
        "ok": not errors and not blocked_features,
        "schema_version": "feature_frame_contract_check_v1",
        "rows": total,
        "minimum_coverage": min_coverage,
        "coverage": coverage,
        "missing_indicators_required": sorted(set(missing_indicators)),
        "blocked_features": blocked_features,
        "errors": errors,
    }


def training_protocol_contract(model_key: str | None = None) -> dict[str, Any]:
    return {
        **TRAINING_PROTOCOL_V1,
        "model_key": model_key,
        "model_task_evaluation": (
            evaluation_contract(model_key) if model_key else None
        ),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def validate_training_run_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    calendar = dict((manifest.get("calendar") or {}))
    gates = {
        "minimum_train_sessions": int(calendar.get("train_sessions") or 0) >= TRAINING_PROTOCOL_V1["calendar"]["minimum_train_sessions"],
        "validation_sessions": int(calendar.get("validation_sessions") or 0) >= TRAINING_PROTOCOL_V1["calendar"]["validation_sessions"],
        "test_sessions": int(calendar.get("test_sessions") or 0) >= TRAINING_PROTOCOL_V1["calendar"]["test_sessions"],
        "minimum_oos_windows": int(manifest.get("oos_windows") or 0) >= TRAINING_PROTOCOL_V1["calendar"]["minimum_oos_windows"],
        "purge_sessions": int(calendar.get("purge_sessions") or 0) >= TRAINING_PROTOCOL_V1["calendar"]["purge_sessions"],
        "embargo_sessions": int(calendar.get("embargo_sessions") or 0) >= TRAINING_PROTOCOL_V1["calendar"]["embargo_sessions"],
        "current_model_evaluation_contract": (
            str(manifest.get("model_evaluation_contract") or "")
            == MODEL_EVALUATION_CONTRACT
        ),
        "task_native_metrics_recorded": bool(
            manifest.get("task_native_metrics_recorded")
        ),
        "task_baseline_comparison_executed": bool(
            manifest.get("task_baseline_comparison_executed")
        ),
        "calibration_checked": bool(manifest.get("calibration_checked", True)),
    }
    blocked = [key for key, passed in gates.items() if not passed]
    return {
        "ok": not blocked,
        "schema_version": "task_native_training_manifest_gate_v1",
        "gates": gates,
        "blocked_reasons": blocked,
        "task_outcome_observations": {
            "task_baseline_outperformed": bool(
                manifest.get("task_baseline_outperformed")
            ),
            "strategy_impact_evaluated_separately": True,
        },
        "protocol": training_protocol_contract(str(manifest.get("model_key") or "")),
    }
