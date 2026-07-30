from __future__ import annotations

import json
import hashlib
import os
import pickle
import threading
import time
from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import numpy as np

from app.db.store import (
    _conn as business_conn,
    create_model_train_run,
    create_model_version,
    get_active_model_version,
    get_model_train_run,
    get_model_version,
    list_model_train_runs,
    list_model_versions,
    set_active_model_version,
    update_model_train_run,
    update_model_version_status,
    update_model_version_validation,
)
from app.services.model_registry import models_root
from app.services.model_artifacts import resolve_model_artifact_path
from app.services.model_evaluation_contracts import (
    SCHEMA_VERSION as MODEL_EVALUATION_CONTRACT,
    evaluate_capital_multitask,
    evaluate_event_abnormal_return,
    evaluate_learning_to_rank,
    evaluate_narrative_transition,
    evaluation_contract,
    supported_model_contracts,
)
from app.services.model_evidence_contract import MODEL_OUTPUT_CONTRACTS
from app.services.training_protocol import training_protocol_contract, validate_training_run_manifest


LIGHTGBM_PARAMS = {
    "learning_rate": 0.03,
    "num_leaves": 31,
    "max_depth": 6,
    "n_estimators": 2000,
    "min_child_samples": 100,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "reg_alpha": 0.5,
    "reg_lambda": 2.0,
    "max_bin": 255,
    "random_state": 20260719,
    "verbosity": -1,
    "early_stopping_rounds": 100,
}

TRAINING_PROTOCOL = {
    "schema_version": "alis_task_native_plan_model_training_protocol_v1",
    "minimum_train_sessions": 756,
    "validation_sessions": 126,
    "test_sessions": 126,
    "roll_step_sessions": 126,
    "purge_sessions": 60,
    "embargo_sessions": 5,
    "walk_forward_windows": 5,
}
NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT = (
    "supervised_narrative_lifecycle_rank_and_exit_v1"
)
TRAINING_SYMBOL_SAMPLE_CONTRACT = "historical_anchor_sha256_symbol_sample_v1"
TRAINING_FAILURE_RECOVERY_CONTRACT = "alis_plan_model_training_failure_recovery_v1"
TRAINING_FAILURE_MAX_RETRIES = 2


class PlanModelTrainingDataError(RuntimeError):
    """Typed training-data failure that must not be degraded into empty rows."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(f"{self.code}: {message}")


def _deterministic_training_symbol_sample(
    symbols: list[str],
    *,
    limit: int,
) -> list[str]:
    """Select one stable cross-history universe without using database hash scans."""

    unique = {
        str(symbol).strip()
        for symbol in symbols
        if str(symbol).strip()
    }
    return sorted(
        unique,
        key=lambda symbol: (hashlib.sha256(symbol.encode("utf-8")).hexdigest(), symbol),
    )[:max(1, int(limit))]


def _training_symbol_sample(
    conn: Any,
    trade_dates: list[str],
    *,
    limit: int,
    anchor_stride: int = 20,
) -> tuple[list[str], dict[str, Any]]:
    dates = sorted({str(value) for value in trade_dates if str(value)})
    stride = max(1, int(anchor_stride))
    anchor_dates = dates[::stride]
    if dates and dates[-1] not in anchor_dates:
        anchor_dates.append(dates[-1])
    if not anchor_dates:
        raise PlanModelTrainingDataError(
            "training_symbol_anchor_missing",
            "训练日历没有可用于历史证券抽样的锚点",
        )
    placeholders = ",".join("?" for _ in anchor_dates)
    try:
        rows = conn.execute(
            f"""SELECT DISTINCT ts_code
                FROM dc_stock_daily_snapshot
                WHERE trade_date IN ({placeholders})
                  AND ts_code IS NOT NULL AND ts_code<>''""",
            tuple(anchor_dates),
        ).fetchall()
    except Exception as exc:
        raise PlanModelTrainingDataError(
            "training_symbol_anchor_query_failed",
            str(exc)[:300],
        ) from exc
    eligible = [str(dict(row).get("ts_code") or "") for row in rows]
    selected = _deterministic_training_symbol_sample(eligible, limit=limit)
    if not selected:
        raise PlanModelTrainingDataError(
            "training_symbol_sample_empty",
            "历史锚点截面没有可用证券",
        )
    evidence = {
        "contract": TRAINING_SYMBOL_SAMPLE_CONTRACT,
        "anchor_stride_sessions": stride,
        "anchor_date_count": len(anchor_dates),
        "anchor_start": anchor_dates[0],
        "anchor_end": anchor_dates[-1],
        "eligible_symbol_count": len(set(eligible)),
        "selected_symbol_count": len(selected),
        "selected_symbols_sha256": hashlib.sha256(
            "\n".join(selected).encode("utf-8")
        ).hexdigest(),
        "survivorship_policy": "union_of_periodic_historical_snapshots_before_deterministic_selection",
    }
    return selected, evidence


def _load_training_moneyflow_rows(
    conn: Any,
    *,
    model_key: str,
    start_date: str,
    end_date: str,
    symbols: list[str],
) -> list[Any]:
    if model_key == "narrative_transition_multitask_v1":
        return []
    if not symbols:
        raise PlanModelTrainingDataError(
            "moneyflow_symbol_sample_empty",
            "资金流训练查询缺少冻结证券集合",
        )
    placeholders = ",".join("?" for _ in symbols)
    try:
        return conn.execute(
            f"""SELECT ts_code,trade_date,payload_json,available_time
                FROM dc_tushare_raw_records
                WHERE api_name='moneyflow'
                  AND trade_date>=? AND trade_date<=?
                  AND ts_code IN ({placeholders})
                ORDER BY ts_code,trade_date""",
            (start_date, end_date, *symbols),
        ).fetchall()
    except Exception as exc:
        code = (
            "capital_moneyflow_query_failed"
            if model_key == "capital_persistence_multitask_v1"
            else "moneyflow_query_failed"
        )
        raise PlanModelTrainingDataError(code, str(exc)[:300]) from exc


def controlled_strategy_ab_passed_current_contract(
    model_key: str,
    impact: dict[str, Any] | None,
) -> bool:
    evidence = impact if isinstance(impact, dict) else {}
    if str(evidence.get("status") or "") != "passed":
        return False
    if model_key != "narrative_transition_multitask_v1":
        return True
    gates = evidence.get("gates") if isinstance(evidence.get("gates"), dict) else {}
    return bool(
        str(evidence.get("model_effect_contract") or "")
        == NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT
        and gates.get("candidate_fold_model_actually_effective")
        and gates.get("candidate_fold_model_effect_contract_current")
    )


PLAN_MODELS: dict[str, dict[str, Any]] = {
    "medium_horizon_lambdarank_v1": {
        "name": "中期景气排序模型",
        "family": "LightGBM LambdaRank",
        "role": "稳定底仓与中期股票排序",
        "required_data": ["1512日价格", "财务景气", "行业状态", "任务原生OOS"],
        "minimum_sessions": 252 * 4,
        "objective": "lambdarank",
        "metric": "ndcg",
        "eval_at": [5, 10, 20],
        "label_gain": [0, 1, 3, 7, 15],
    },
    "capital_persistence_multitask_v1": {
        "name": "资金持续多任务模型",
        "family": "LightGBM direction+quantile+horizon-failure bundle",
        "role": "确认叙事资金认可度与加减仓门",
        "required_data": ["个股真实资金流", "大单净流入", "5/20日资金持续率", "未来10日资金持续标签"],
        "minimum_sessions": 252 * 2,
        "tasks": [
            "direction_classifier",
            "p10_p50_p90_quantile",
            "horizon_failure_probability",
        ],
    },
    "leader_lambdarank_v1": {
        "name": "龙头排序模型",
        "family": "LightGBM LambdaRank",
        "role": "已确认主线内部龙头选择",
        "required_data": ["主题/行业query", "相对板块收益", "资金持续", "席位质量", "可成交性"],
        "minimum_sessions": 252 * 2,
        "objective": "lambdarank",
        "metric": "ndcg",
        "eval_at": [1, 3, 5],
        "lambdarank_truncation_level": 8,
    },
    "event_surprise_lgbm_v1": {
        "name": "事件预期差模型",
        "family": "公告与严格公司映射新闻事件 + LightGBM回归",
        "role": "事件与预期差仓收益/胜率评估",
        "required_data": ["公告与重大新闻", "严格公司映射", "真实可见时间", "事后标签"],
        "minimum_sessions": 250,
        "status_note": "文本历史和标签不足时只能保持proxy",
    },
    "narrative_transition_multitask_v1": {
        "name": "叙事状态转移模型",
        "family": "LightGBM transition-hazard + constrained-destination lifecycle bundle",
        "role": "主线生命周期加减仓/退出",
        "required_data": ["250日主题快照", "板块广度", "拥挤度", "状态年龄", "点时转移标签"],
        "minimum_sessions": 250,
        "status_note": "主题快照不足250日不得晋升",
    },
}

_locks: set[str] = set()
_lock = threading.Lock()
_status_cache: dict[str, Any] = {"at": 0.0, "value": None}
_operations_cache: dict[str, Any] = {"at": 0.0, "value": None}
_status_refresh_lock = threading.Lock()


def _serialize_status_refresh(func: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        with _status_refresh_lock:
            return func(*args, **kwargs)

    return wrapped
PLAN_MODEL_FEATURES = [
    "momentum_20",
    "momentum_60",
    "volatility_20",
    "turnover_slope",
    "capital_persistence",
    "narrative_strength",
    "event_surprise",
    "event_text_sentiment",
    "event_text_confidence",
    "narrative_lifecycle_code",
    "narrative_transition_pressure",
    "narrative_breadth",
    "narrative_crowding",
    "narrative_score_slope",
    "narrative_breadth_slope",
    "narrative_state_age",
]
NARRATIVE_LIFECYCLE_CLASSES = ("形成", "扩散", "成熟/拥挤", "衰退")
NARRATIVE_ALLOWED_DESTINATIONS: dict[int, tuple[int, ...]] = {
    0: (1, 3),
    1: (0, 2, 3),
    2: (1, 3),
    3: (0,),
}


def _narrative_reachable_destinations(
    source: int,
    *,
    steps: int = 3,
) -> tuple[int, ...]:
    frontier = {int(source)}
    reachable: set[int] = set()
    for _ in range(max(1, int(steps))):
        frontier = {
            destination
            for state in frontier
            for destination in (
                (state,) + NARRATIVE_ALLOWED_DESTINATIONS[state]
            )
        }
        reachable.update(frontier)
    reachable.discard(int(source))
    return tuple(sorted(reachable))


NARRATIVE_HORIZON_DESTINATIONS = {
    state: _narrative_reachable_destinations(state, steps=3)
    for state in range(len(NARRATIVE_LIFECYCLE_CLASSES))
}
NARRATIVE_MODEL_FEATURES = (
    "capital_persistence",
    "narrative_strength",
    "narrative_lifecycle_code",
    "narrative_transition_pressure",
    "narrative_breadth",
    "narrative_crowding",
    "narrative_score_slope",
    "narrative_breadth_slope",
    "narrative_state_age",
)
CAPITAL_FLOW_HISTORY_SESSIONS = 20
EVENT_POSITIVE_TERMS = ("中标", "增持", "回购", "预增", "扭亏", "签订", "获批", "突破", "上调")
EVENT_NEGATIVE_TERMS = ("减持", "立案", "处罚", "亏损", "下修", "终止", "违约", "退市", "诉讼")


def _payload_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _event_news_direction(text: str) -> tuple[str, str, float]:
    positive = sum(1 for term in EVENT_POSITIVE_TERMS if term in text)
    negative = sum(1 for term in EVENT_NEGATIVE_TERMS if term in text)
    if positive == negative:
        return "neutral", "medium", 0.55
    direction = "positive" if positive > negative else "negative"
    strength = abs(positive - negative)
    return direction, ("high" if strength >= 2 else "medium"), min(0.85, 0.6 + strength * 0.1)


def _event_usable_trade_date(
    event_time: str,
    available_time: str,
    trade_dates: list[str],
) -> str:
    source_time = str(event_time or available_time or "")
    available = str(available_time or source_time)
    source_day = source_time[:10].replace("-", "")
    available_day = available[:10].replace("-", "")
    candidate_day = max(source_day, available_day)
    clock = available[11:19] if len(available) >= 19 else ""
    if candidate_day in trade_dates and (not clock or clock <= "15:00:00"):
        return candidate_day
    index = bisect_right(trade_dates, candidate_day)
    return trade_dates[index] if index < len(trade_dates) else ""


def _moneyflow_observations(
    rows: list[Any],
    trade_dates: list[str],
) -> dict[tuple[str, str], dict[str, float]]:
    by_symbol: dict[str, dict[str, dict[str, float]]] = {}
    for row in rows:
        item = dict(row)
        payload = _payload_dict(item.get("payload_json"))
        symbol = str(item.get("ts_code") or payload.get("ts_code") or "")
        trade_date = str(item.get("trade_date") or payload.get("trade_date") or "")
        if not symbol or not trade_date:
            continue
        net_amount = float(payload.get("net_mf_amount") or 0.0)
        buy_large = float(payload.get("buy_lg_vol") or 0.0)
        sell_large = float(payload.get("sell_lg_vol") or 0.0)
        volume_scale = max(1.0, abs(buy_large) + abs(sell_large))
        by_symbol.setdefault(symbol, {})[trade_date] = {
            "net_amount": net_amount,
            "large_order_balance": (buy_large - sell_large) / volume_scale,
        }

    observations: dict[tuple[str, str], dict[str, float]] = {}
    date_index = {value: index for index, value in enumerate(trade_dates)}
    for symbol, values in by_symbol.items():
        for trade_date, current in values.items():
            index = date_index.get(trade_date)
            if index is None:
                continue
            history_dates = trade_dates[max(0, index - CAPITAL_FLOW_HISTORY_SESSIONS + 1):index + 1]
            history = [values[value]["net_amount"] for value in history_dates if value in values]
            if len(history) < CAPITAL_FLOW_HISTORY_SESSIONS:
                continue
            future_dates = trade_dates[index + 1:index + 11]
            future = [values[value]["net_amount"] for value in future_dates if value in values]
            if len(future) < 10:
                continue
            scale = max(1.0, float(np.mean(np.abs(history))))
            observations[(symbol, trade_date)] = {
                "net_flow_z": current["net_amount"] / scale,
                "positive_ratio_5": sum(value > 0 for value in history[-5:]) / 5.0,
                "positive_ratio_20": sum(value > 0 for value in history) / 20.0,
                "large_order_balance": current["large_order_balance"],
                "future_persistence": float(np.mean(future)) / scale
                + (sum(value > 0 for value in future) / 10.0 - 0.5),
            }
    return observations


def _plan_model_weights(model_key: str) -> dict[str, float]:
    presets = {
        "medium_horizon_lambdarank_v1": {"momentum_60": .28, "volatility_20": -.16, "capital_persistence": .20, "narrative_strength": .18, "turnover_slope": .12},
        "capital_persistence_multitask_v1": {"capital_persistence": .42, "turnover_slope": .24, "momentum_20": .12, "volatility_20": -.10},
        "leader_lambdarank_v1": {"momentum_20": .26, "capital_persistence": .26, "narrative_strength": .24, "turnover_slope": .14, "volatility_20": -.08},
        "event_surprise_lgbm_v1": {"event_surprise": .38, "narrative_strength": .22, "turnover_slope": .16, "capital_persistence": .12},
        "narrative_transition_multitask_v1": {"narrative_strength": .36, "capital_persistence": .22, "momentum_20": .14, "volatility_20": -.12},
    }
    raw = presets.get(model_key, {})
    total = sum(abs(v) for v in raw.values()) or 1.0
    return {feature: round(float(raw.get(feature, 0.0)) / total, 8) for feature in PLAN_MODEL_FEATURES}


def _load_artifact(path: str | Path | None) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        payload = json.loads(resolve_model_artifact_path(path).read_text(encoding="utf-8"))
        model_path = str(payload.get("model_path") or "")
        if model_path:
            payload["model_path"] = str(resolve_model_artifact_path(model_path))
        return payload
    except Exception:
        return None


def _artifact_summary(version: dict[str, Any] | None) -> dict[str, Any]:
    artifact = _load_artifact((version or {}).get("artifact_path"))
    if not artifact:
        return {"artifact_ready": False, "artifact_type": "missing"}
    model_path = str(artifact.get("model_path") or "")
    return {
        "artifact_ready": True,
        "artifact_type": artifact.get("artifact_type"),
        "model_type": artifact.get("model_type"),
        "model_path_exists": bool(model_path and resolve_model_artifact_path(model_path).is_file()),
        "features": artifact.get("features") or [],
        "training_dataset": artifact.get("training_dataset") or {},
        "predict_contract": artifact.get("predict_contract") or {},
        "output_contract": artifact.get("output_contract") or {},
        "training_protocol": artifact.get("training_protocol") or {},
    }


def _coerce_feature_vector(features: dict[str, Any], feature_names: list[str]) -> list[float]:
    return [float(features.get(feature, 0.0) or 0.0) for feature in feature_names]


def _estimator_input(estimator: Any, values: Any) -> Any:
    """Preserve fitted feature names without changing numeric values."""

    try:
        feature_names = [
            str(value)
            for value in list(getattr(estimator, "feature_names_in_", []))
        ]
    except Exception:
        feature_names = []
    if not feature_names or hasattr(values, "columns"):
        return values
    matrix = np.asarray(values, dtype=float)
    if matrix.ndim != 2 or matrix.shape[1] != len(feature_names):
        return values
    import pandas as pd

    return pd.DataFrame(matrix, columns=feature_names)


def _predict_values(model: Any, values: Any) -> np.ndarray:
    return np.asarray(
        model.predict(_estimator_input(model, values)),
        dtype=float,
    )


def _canonical_class_probabilities(
    classifier: Any,
    values: Any,
    *,
    class_count: int,
) -> np.ndarray:
    raw = np.asarray(
        classifier.predict_proba(_estimator_input(classifier, values)),
        dtype=float,
    )
    if raw.ndim == 1:
        raw = raw.reshape(-1, 1)
    classes = [
        int(value)
        for value in np.asarray(
            getattr(classifier, "classes_", np.arange(raw.shape[1])),
            dtype=int,
        ).tolist()
    ]
    probabilities = np.zeros((raw.shape[0], int(class_count)), dtype=float)
    for source_index, class_id in enumerate(classes):
        if 0 <= class_id < int(class_count) and source_index < raw.shape[1]:
            probabilities[:, class_id] = raw[:, source_index]
    return probabilities


def _model_score(model: Any, values: Any) -> np.ndarray:
    scores = _predict_values(model, values).reshape(-1)
    if len(scores) != len(values):
        raise ValueError("模型预测行数与输入不一致")
    return scores


class _ConstantProbabilityClassifier:
    """Pickle-safe fallback for a training slice containing one observed class."""

    def __init__(self, probabilities: list[float]):
        values = np.asarray(probabilities, dtype=float)
        values = np.maximum(values, 0.0)
        self._probabilities = values / max(float(values.sum()), 1e-12)
        self.classes_ = np.arange(len(values), dtype=int)

    def predict_proba(self, values: Any) -> np.ndarray:
        return np.tile(self._probabilities, (len(values), 1))


def _canonical_narrative_observation(
    *,
    lifecycle: str,
    score: float,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Recover the V2 lifecycle strictly from persisted observable evidence."""

    components = (
        payload.get("components")
        if isinstance(payload.get("components"), dict)
        else {}
    )
    contract = (
        payload.get("lifecycle_contract")
        if isinstance(payload.get("lifecycle_contract"), dict)
        else {}
    )
    observations = (
        contract.get("observations")
        if isinstance(contract.get("observations"), dict)
        else {}
    )
    evidence = (
        payload.get("evidence")
        if isinstance(payload.get("evidence"), dict)
        else {}
    )
    base_score = float(
        observations.get("base_score_before_crowding_penalty")
        or payload.get("base_score")
        or score
        or 0.0
    )
    crowding = float(
        observations.get("turnover_top_decile_concentration")
        or (float(payload.get("crowding_penalty") or 0.0) / 20.0)
    )
    breadth = float(
        observations.get("breadth_5_session")
        or components.get("breadth")
        or 0.0
    )
    price_confirmation = float(components.get("price_confirmation") or 0.0)
    degenerate_legacy_snapshot = (
        not str(contract.get("schema_version") or "").startswith(
            "alis_narrative_lifecycle_observable_v2"
        )
        and breadth == 0.0
        and float(components.get("capital_persistence") or 0.0) == 0.0
        and int(evidence.get("source_types") or 0) == 0
    )
    direct = {
        name: index for index, name in enumerate(NARRATIVE_LIFECYCLE_CLASSES)
    }
    if lifecycle in direct:
        candidate = int(direct[lifecycle])
    elif lifecycle in {"退潮", "沉寂"}:
        candidate = 3
    elif (
        base_score >= 55
        and crowding >= .60
        and price_confirmation >= .50
    ):
        candidate = 2
    elif (
        base_score >= 55
        and breadth >= .55
        and price_confirmation >= .50
    ):
        candidate = 1
    else:
        candidate = 0
    return {
        "candidate_state": candidate,
        "base_score": base_score,
        "score": float(score or 0.0),
        "breadth": breadth,
        "crowding": crowding,
        "price_confirmation": price_confirmation,
        "capital_persistence": float(
            components.get("capital_persistence") or 0.0
        ),
        "observable": not degenerate_legacy_snapshot,
    }


def _constrain_narrative_state(previous: int | None, candidate: int) -> int:
    """Apply the documented lifecycle graph without inventing observations."""

    if previous is None or candidate == previous:
        return int(candidate)
    if candidate in NARRATIVE_ALLOWED_DESTINATIONS[int(previous)]:
        return int(candidate)
    # A daily observation cannot skip an intermediate lifecycle phase.
    if previous == 0 and candidate == 2:
        return 1
    if previous == 2 and candidate == 0:
        return 1
    if previous == 3 and candidate in {1, 2}:
        return 0
    return int(candidate)


def _narrative_two_stage_probabilities(
    model: dict[str, Any],
    values: np.ndarray,
    current_states: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    transition_values = _canonical_class_probabilities(
        model["transition_classifier"],
        values,
        class_count=2,
    )
    transition_values = _temperature_scale_probabilities(
        transition_values,
        float(model.get("transition_temperature") or 1.0),
    )
    transition = _apply_binary_logit_bias(
        transition_values[:, 1],
        float(model.get("transition_logit_bias") or 0.0),
    )
    destination = _canonical_class_probabilities(
        model["destination_classifier"],
        values,
        class_count=len(NARRATIVE_LIFECYCLE_CLASSES),
    )
    destination = _temperature_scale_probabilities(
        destination,
        float(model.get("destination_temperature") or 1.0),
    )
    final = np.zeros_like(destination)
    for index, current in enumerate(np.asarray(current_states, dtype=int)):
        current = int(np.clip(current, 0, len(NARRATIVE_LIFECYCLE_CLASSES) - 1))
        allowed = NARRATIVE_HORIZON_DESTINATIONS[current]
        masked = np.zeros(len(NARRATIVE_LIFECYCLE_CLASSES), dtype=float)
        masked[list(allowed)] = destination[index, list(allowed)]
        if float(masked.sum()) <= 1e-12:
            masked[list(allowed)] = 1.0
        masked /= float(masked.sum())
        change_probability = float(np.clip(transition[index], 0.0, 1.0))
        final[index] = change_probability * masked
        final[index, current] += 1.0 - change_probability
        destination[index] = masked
    return final, transition, destination


def _temperature_scale_probabilities(
    probabilities: np.ndarray,
    temperature: float,
) -> np.ndarray:
    values = np.clip(np.asarray(probabilities, dtype=float), 1e-12, 1.0)
    scaled = np.exp(np.log(values) / max(.25, min(20.0, temperature)))
    return scaled / np.maximum(scaled.sum(axis=1, keepdims=True), 1e-12)


def _apply_binary_logit_bias(
    probabilities: np.ndarray,
    bias: float,
) -> np.ndarray:
    values = np.clip(np.asarray(probabilities, dtype=float), 1e-9, 1 - 1e-9)
    logits = np.log(values / (1.0 - values)) + float(bias)
    return 1.0 / (1.0 + np.exp(-np.clip(logits, -40, 40)))


def _validation_temperature(
    probabilities: np.ndarray,
    actual: np.ndarray,
) -> float:
    if not len(actual):
        return 1.0
    candidates = (.50, .75, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0, 12.0)
    losses = []
    for temperature in candidates:
        scaled = _temperature_scale_probabilities(
            probabilities,
            temperature,
        )
        actual_probability = scaled[
            np.arange(len(actual)),
            np.asarray(actual, dtype=int),
        ]
        losses.append(float(-np.mean(np.log(
            np.clip(actual_probability, 1e-12, 1.0)
        ))))
    return float(candidates[int(np.argmin(losses))])


def score_plan_model_features(
    model_key: str,
    features: dict[str, Any],
    artifact: dict[str, Any] | None = None,
    *,
    allow_proxy: bool = False,
) -> dict[str, Any]:
    payload = artifact
    if payload is None:
        active = get_active_model_version(model_key)
        payload = _load_artifact((active or {}).get("artifact_path"))
    if payload is None:
        if not allow_proxy:
            return {
                "ok": False,
                "model_key": model_key,
                "score": None,
                "reason": "active_model_artifact_unavailable",
                "model_type": "unavailable",
            }
        payload = {
            "weights": _plan_model_weights(model_key),
            "features": PLAN_MODEL_FEATURES,
            "model_type": "plan_linear_ranker",
        }
    feature_names = list(payload.get("features") or PLAN_MODEL_FEATURES)
    model_path = payload.get("model_path")
    model_type = str(payload.get("model_type") or "plan_linear_ranker")
    if model_path or payload.get("_model") is not None:
        model = payload.get("_model")
        if model is None:
            with Path(str(model_path)).open("rb") as handle:
                model = pickle.load(handle)
        vector = np.asarray([_coerce_feature_vector(features, feature_names)], dtype=float)
        if isinstance(model, dict) and model.get("bundle_type") == "capital_persistence_multihead_v1":
            direction_probabilities = _canonical_class_probabilities(
                model["direction"], vector, class_count=5,
            )[0]
            direction_score = float(direction_probabilities @ np.linspace(-1.0, 1.0, len(direction_probabilities)))
            quantiles = {
                key: float(_predict_values(model[key], vector)[0])
                for key in ("q10", "q50", "q90")
            }
            hazard = float(_canonical_class_probabilities(
                model["hazard"], vector, class_count=2,
            )[0, 1])
            value = direction_score + quantiles["q50"] - hazard
            return {
                "ok": True, "model_key": model_key, "score": round(value, 8),
                "model_type": model_type, "artifact_version": payload.get("version"),
                "outputs": {
                    "direction_probability": round(float(direction_probabilities[-1]), 8),
                    "strength_p10": round(quantiles["q10"], 8),
                    "strength_p50": round(quantiles["q50"], 8),
                    "strength_p90": round(quantiles["q90"], 8),
                    "failure_hazard": round(hazard, 8),
                },
            }
        if isinstance(model, dict) and model.get("bundle_type") == "narrative_transition_classifier_v1":
            probabilities = _canonical_class_probabilities(
                model["classifier"], vector, class_count=5,
            )[0]
            classes = [str(value) for value in model.get("classes") or range(len(probabilities))]
            probability_map = {key: round(float(value), 8) for key, value in zip(classes, probabilities)}
            exit_risk = sum(value for key, value in probability_map.items() if key in {"拥挤", "退潮", "沉寂"})
            value = 1.0 - float(exit_risk)
            return {
                "ok": True, "model_key": model_key, "score": round(value, 8),
                "model_type": model_type, "artifact_version": payload.get("version"),
                "outputs": {
                    "next_state_probabilities": probability_map,
                    "exit_risk": round(float(exit_risk), 8),
                    "confidence": round(float(max(probabilities, default=0.0)), 8),
                },
            }
        if isinstance(model, dict) and model.get("bundle_type") == "narrative_lifecycle_two_stage_v2":
            current_state = int(np.clip(round(
                float(features.get("narrative_lifecycle_code") or 0.0)
                * (len(NARRATIVE_LIFECYCLE_CLASSES) - 1)
            ), 0, len(NARRATIVE_LIFECYCLE_CLASSES) - 1))
            probabilities, transition, destination = (
                _narrative_two_stage_probabilities(
                    model,
                    vector,
                    np.asarray([current_state], dtype=int),
                )
            )
            probability_map = {
                key: round(float(value), 8)
                for key, value in zip(
                    NARRATIVE_LIFECYCLE_CLASSES,
                    probabilities[0],
                )
            }
            exit_risk = probability_map["成熟/拥挤"] + probability_map["衰退"]
            return {
                "ok": True,
                "model_key": model_key,
                "score": round(1.0 - float(exit_risk), 8),
                "model_type": model_type,
                "artifact_version": payload.get("version"),
                "outputs": {
                    "transition_probability": round(float(transition[0]), 8),
                    "conditional_destination_probabilities": {
                        key: round(float(value), 8)
                        for key, value in zip(
                            NARRATIVE_LIFECYCLE_CLASSES,
                            destination[0],
                        )
                    },
                    "next_state_probabilities": probability_map,
                    "exit_risk": round(float(exit_risk), 8),
                    "confidence": round(
                        float(max(probabilities[0], default=0.0)),
                        8,
                    ),
                },
            }
        value = float(_predict_values(model, vector)[0])
        result = {"ok": True, "model_key": model_key, "score": round(value, 8), "model_type": model_type, "artifact_version": payload.get("version")}
        if model_key == "event_surprise_lgbm_v1":
            result["outputs"] = {
                "expected_abnormal_return_5d": round(value, 8),
                "event_text_sentiment": round(float(features.get("event_text_sentiment") or 0.0), 8),
                "event_text_confidence": round(float(features.get("event_text_confidence") or 0.0), 8),
            }
        return result
    weights = dict(payload.get("weights") or {})
    value = sum(float(weights.get(key, 0.0) or 0.0) * float(features.get(key, 0.0) or 0.0) for key in feature_names)
    return {"ok": True, "model_key": model_key, "score": round(value, 8), "model_type": model_type}


def score_plan_model_feature_batch(
    model_key: str,
    features_by_symbol: dict[str, dict[str, Any]],
    artifact: dict[str, Any] | None = None,
    *,
    allow_proxy: bool = False,
) -> dict[str, dict[str, Any]]:
    """Score one cross-section per estimator call without changing outputs."""

    symbols = list(features_by_symbol)
    if not symbols:
        return {}
    payload = artifact
    if payload is None:
        active = get_active_model_version(model_key)
        payload = _load_artifact((active or {}).get("artifact_path"))
    if payload is None:
        if not allow_proxy:
            return {
                symbol: {
                    "ok": False,
                    "model_key": model_key,
                    "score": None,
                    "reason": "active_model_artifact_unavailable",
                    "model_type": "unavailable",
                }
                for symbol in symbols
            }
        payload = {
            "weights": _plan_model_weights(model_key),
            "features": PLAN_MODEL_FEATURES,
            "model_type": "plan_linear_ranker",
        }
    feature_names = list(payload.get("features") or PLAN_MODEL_FEATURES)
    matrix = np.asarray(
        [_coerce_feature_vector(features_by_symbol[symbol], feature_names) for symbol in symbols],
        dtype=float,
    )
    model_path = payload.get("model_path")
    model_type = str(payload.get("model_type") or "plan_linear_ranker")
    if model_path or payload.get("_model") is not None:
        model = payload.get("_model")
        if model is None:
            with Path(str(model_path)).open("rb") as handle:
                model = pickle.load(handle)
        if isinstance(model, dict) and model.get("bundle_type") == "capital_persistence_multihead_v1":
            direction_probabilities = _canonical_class_probabilities(
                model["direction"], matrix, class_count=5,
            )
            direction_scores = direction_probabilities @ np.linspace(
                -1.0, 1.0, direction_probabilities.shape[1],
            )
            quantiles = {
                key: _predict_values(model[key], matrix)
                for key in ("q10", "q50", "q90")
            }
            hazards = _canonical_class_probabilities(
                model["hazard"], matrix, class_count=2,
            )[:, 1]
            values = direction_scores + quantiles["q50"] - hazards
            return {
                symbol: {
                    "ok": True,
                    "model_key": model_key,
                    "score": round(float(values[index]), 8),
                    "model_type": model_type,
                    "artifact_version": payload.get("version"),
                    "outputs": {
                        "direction_probability": round(float(direction_probabilities[index, -1]), 8),
                        "strength_p10": round(float(quantiles["q10"][index]), 8),
                        "strength_p50": round(float(quantiles["q50"][index]), 8),
                        "strength_p90": round(float(quantiles["q90"][index]), 8),
                        "failure_hazard": round(float(hazards[index]), 8),
                    },
                }
                for index, symbol in enumerate(symbols)
            }
        if isinstance(model, dict) and model.get("bundle_type") == "narrative_transition_classifier_v1":
            probabilities = _canonical_class_probabilities(
                model["classifier"], matrix, class_count=5,
            )
            classes = [str(value) for value in model.get("classes") or range(probabilities.shape[1])]
            result: dict[str, dict[str, Any]] = {}
            for index, symbol in enumerate(symbols):
                probability_map = {
                    key: round(float(value), 8)
                    for key, value in zip(classes, probabilities[index])
                }
                exit_risk = sum(
                    value for key, value in probability_map.items()
                    if key in {"拥挤", "退潮", "沉寂"}
                )
                result[symbol] = {
                    "ok": True,
                    "model_key": model_key,
                    "score": round(1.0 - float(exit_risk), 8),
                    "model_type": model_type,
                    "artifact_version": payload.get("version"),
                    "outputs": {
                        "next_state_probabilities": probability_map,
                        "exit_risk": round(float(exit_risk), 8),
                        "confidence": round(float(max(probabilities[index], default=0.0)), 8),
                    },
                }
            return result
        if isinstance(model, dict) and model.get("bundle_type") == "narrative_lifecycle_two_stage_v2":
            current_states = np.asarray([
                int(np.clip(round(
                    float(
                        features_by_symbol[symbol].get(
                            "narrative_lifecycle_code",
                            0.0,
                        )
                        or 0.0
                    )
                    * (len(NARRATIVE_LIFECYCLE_CLASSES) - 1)
                ), 0, len(NARRATIVE_LIFECYCLE_CLASSES) - 1))
                for symbol in symbols
            ], dtype=int)
            probabilities, transitions, destinations = (
                _narrative_two_stage_probabilities(
                    model,
                    matrix,
                    current_states,
                )
            )
            result = {}
            for index, symbol in enumerate(symbols):
                probability_map = {
                    key: round(float(value), 8)
                    for key, value in zip(
                        NARRATIVE_LIFECYCLE_CLASSES,
                        probabilities[index],
                    )
                }
                exit_risk = (
                    probability_map["成熟/拥挤"]
                    + probability_map["衰退"]
                )
                result[symbol] = {
                    "ok": True,
                    "model_key": model_key,
                    "score": round(1.0 - float(exit_risk), 8),
                    "model_type": model_type,
                    "artifact_version": payload.get("version"),
                    "outputs": {
                        "transition_probability": round(
                            float(transitions[index]),
                            8,
                        ),
                        "conditional_destination_probabilities": {
                            key: round(float(value), 8)
                            for key, value in zip(
                                NARRATIVE_LIFECYCLE_CLASSES,
                                destinations[index],
                            )
                        },
                        "next_state_probabilities": probability_map,
                        "exit_risk": round(float(exit_risk), 8),
                        "confidence": round(
                            float(max(probabilities[index], default=0.0)),
                            8,
                        ),
                    },
                }
            return result
        values = _predict_values(model, matrix)
        result = {}
        for index, symbol in enumerate(symbols):
            item: dict[str, Any] = {
                "ok": True,
                "model_key": model_key,
                "score": round(float(values[index]), 8),
                "model_type": model_type,
                "artifact_version": payload.get("version"),
            }
            if model_key == "event_surprise_lgbm_v1":
                features = features_by_symbol[symbol]
                item["outputs"] = {
                    "expected_abnormal_return_5d": round(float(values[index]), 8),
                    "event_text_sentiment": round(float(features.get("event_text_sentiment") or 0.0), 8),
                    "event_text_confidence": round(float(features.get("event_text_confidence") or 0.0), 8),
                }
            result[symbol] = item
        return result
    weights = dict(payload.get("weights") or {})
    return {
        symbol: {
            "ok": True,
            "model_key": model_key,
            "score": round(sum(
                float(weights.get(key, 0.0) or 0.0)
                * float(features_by_symbol[symbol].get(key, 0.0) or 0.0)
                for key in feature_names
            ), 8),
            "model_type": model_type,
        }
        for symbol in symbols
    }


def _plan_model_horizon(model_key: str) -> int:
    if model_key == "medium_horizon_lambdarank_v1":
        return 60
    if model_key == "leader_lambdarank_v1":
        return 20
    if model_key == "event_surprise_lgbm_v1":
        return 5
    if model_key == "narrative_transition_multitask_v1":
        return 3
    return 10


def _required_training_sessions() -> int:
    return (
        int(TRAINING_PROTOCOL["minimum_train_sessions"])
        + int(TRAINING_PROTOCOL["purge_sessions"])
        + int(TRAINING_PROTOCOL["validation_sessions"])
        + int(TRAINING_PROTOCOL["embargo_sessions"])
        + int(TRAINING_PROTOCOL["test_sessions"])
        + (int(TRAINING_PROTOCOL["walk_forward_windows"]) - 1)
        * int(TRAINING_PROTOCOL["roll_step_sessions"])
    )


def _walk_forward_date_windows(unique_dates: list[str]) -> list[dict[str, list[str]]]:
    dates = sorted(set(str(value) for value in unique_dates))
    required = _required_training_sessions()
    if len(dates) < required:
        raise RuntimeError(
            f"训练交易日不足以执行正式5窗：实际 {len(dates)}，至少需要 {required}"
        )
    train_min = int(TRAINING_PROTOCOL["minimum_train_sessions"])
    validation_size = int(TRAINING_PROTOCOL["validation_sessions"])
    test_size = int(TRAINING_PROTOCOL["test_sessions"])
    roll_step = int(TRAINING_PROTOCOL["roll_step_sessions"])
    purge = int(TRAINING_PROTOCOL["purge_sessions"])
    embargo = int(TRAINING_PROTOCOL["embargo_sessions"])
    window_count = int(TRAINING_PROTOCOL["walk_forward_windows"])
    first_test_start = len(dates) - test_size - (window_count - 1) * roll_step
    windows: list[dict[str, list[str]]] = []
    for offset in range(window_count):
        test_start = first_test_start + offset * roll_step
        validation_end = test_start - embargo
        validation_start = validation_end - validation_size
        train_end = validation_start - purge
        train_dates = dates[:train_end]
        validation_dates = dates[validation_start:validation_end]
        test_dates = dates[test_start:test_start + test_size]
        if (
            len(train_dates) < train_min
            or len(validation_dates) != validation_size
            or len(test_dates) != test_size
        ):
            raise RuntimeError("正式Walk-forward窗口无法满足756/126/126交易日合同")
        windows.append({
            "train": train_dates,
            "validation": validation_dates,
            "test": test_dates,
        })
    return windows


def _build_plan_training_dataset(
    model_key: str,
    *,
    max_sessions: int = 1_800,
    max_rows: int = 600_000,
    max_symbols_per_session: int = 350,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    from app.data_platform.db import _conn as data_conn

    horizon = _plan_model_horizon(model_key)
    date_limit = max_sessions + horizon + 5
    if on_progress:
        on_progress("dataset", 0.24, f"读取最近 {date_limit} 个交易日训练日历")
    with data_conn() as c:
        date_rows = c.execute(
            """SELECT trade_date
               FROM dc_available_trade_dates
               ORDER BY trade_date DESC
               LIMIT ?""",
            (date_limit,),
        ).fetchall()
        dates = [str(dict(row).get("trade_date") or "") for row in date_rows if dict(row).get("trade_date")]
        if len(dates) < max(80, horizon + 20):
            raise RuntimeError(f"训练日历不足：{len(dates)} sessions")
        start_date = min(dates)
        if on_progress:
            on_progress("dataset", 0.28, f"日历覆盖 {len(dates)} 天，起始 {start_date}，预测周期 {horizon} 天")
        ascending_dates = sorted(dates)
        mapped_dates = [(ascending_dates[idx], ascending_dates[idx + horizon]) for idx in range(0, max(0, len(ascending_dates) - horizon))]
        if not mapped_dates:
            raise RuntimeError(f"训练日历无法构造 {horizon} 天未来收益标签")
        if len(mapped_dates) < _required_training_sessions():
            raise RuntimeError(
                f"可标注训练交易日不足：实际 {len(mapped_dates)}，正式5窗至少需要 {_required_training_sessions()}"
            )
        daily_sample_limit = min(
            max(1, int(max_symbols_per_session)),
            max(1, int(max_rows) // len(mapped_dates)),
        )
        values_sql = ",".join(["(?, ?)"] * len(mapped_dates))
        values_params = [value for pair in mapped_dates for value in pair]
        if getattr(c, "_is_postgres", False):
            c.execute("SELECT set_config('statement_timeout', ?, false)", ("120000",))
        sampled_symbols: list[str] = []
        symbol_sampling = {
            "contract": "not_applicable_for_theme_level_training",
            "selected_symbol_count": 0,
        }
        if model_key != "narrative_transition_multitask_v1":
            sampled_symbols, symbol_sampling = _training_symbol_sample(
                c,
                ascending_dates,
                limit=daily_sample_limit,
            )
            symbol_placeholders = ",".join("?" for _ in sampled_symbols)
        if model_key == "narrative_transition_multitask_v1":
            raw_rows = c.execute(
                f"""
                WITH date_map(trade_date, future_date) AS (VALUES {values_sql})
                SELECT
                    'THEME:' || s.theme_code AS ts_code,
                    s.as_of_date AS trade_date,
                    m.future_date,
                    1.0 AS close,
                    0.0 AS change_pct,
                    0.0 AS turnover,
                    0.0 AS turnover_rate,
                    0.0 AS volume,
                    NULL AS pe_ttm,
                    NULL AS pb,
                    NULL AS total_mv,
                    100.0 AS data_quality_score,
                    1.0 AS future_close,
                    0.0 AS future_change_pct,
                    0.0 AS future_turnover_rate,
                    0.0 AS momentum_20,
                    0.0 AS momentum_60,
                    0.0 AS volatility_20,
                    0.0 AS turnover_slope
                FROM dc_theme_snapshot s
                JOIN date_map m ON m.trade_date=s.as_of_date
                WHERE s.as_of_date>=?
                ORDER BY s.as_of_date,s.theme_code
                """,
                (*values_params, start_date),
            ).fetchall()
        else:
            raw_rows = c.execute(
                f"""
                WITH date_map(trade_date, future_date) AS (VALUES {values_sql}),
                ordered AS (
                    SELECT
                        b.*,
                        LAG(b.close, 20) OVER (
                            PARTITION BY b.ts_code ORDER BY b.trade_date
                        ) AS close_20d_ago,
                        LAG(b.close, 60) OVER (
                            PARTITION BY b.ts_code ORDER BY b.trade_date
                        ) AS close_60d_ago,
                        STDDEV_POP(COALESCE(b.change_pct, 0) / 100.0) OVER (
                            PARTITION BY b.ts_code ORDER BY b.trade_date
                            ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                        ) AS volatility_20d,
                        AVG(COALESCE(b.turnover_rate, 0)) OVER (
                            PARTITION BY b.ts_code ORDER BY b.trade_date
                            ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                        ) AS turnover_avg_20d,
                        AVG(COALESCE(b.turnover_rate, 0)) OVER (
                            PARTITION BY b.ts_code ORDER BY b.trade_date
                            ROWS BETWEEN 59 PRECEDING AND CURRENT ROW
                        ) AS turnover_avg_60d
                    FROM dc_stock_daily_snapshot b
                    WHERE b.ts_code IN ({symbol_placeholders})
                      AND b.trade_date <= ?
                )
                SELECT
                    b.ts_code,
                    b.trade_date,
                    m.future_date,
                    b.close,
                    b.change_pct,
                    b.turnover,
                    b.turnover_rate,
                    b.volume,
                    b.pe_ttm,
                    b.pb,
                    b.total_mv,
                    b.data_quality_score,
                    f.close AS future_close,
                    f.change_pct AS future_change_pct,
                    f.turnover_rate AS future_turnover_rate,
                    CASE WHEN b.close_20d_ago > 0 THEN b.close / b.close_20d_ago - 1.0 END AS momentum_20,
                    CASE WHEN b.close_60d_ago > 0 THEN b.close / b.close_60d_ago - 1.0 END AS momentum_60,
                    b.volatility_20d AS volatility_20,
                    CASE WHEN b.turnover_avg_60d > 0
                         THEN b.turnover_avg_20d / b.turnover_avg_60d - 1.0
                         ELSE 0.0 END AS turnover_slope
                FROM ordered b
                JOIN date_map m ON m.trade_date = b.trade_date
                JOIN dc_stock_daily_snapshot f
                    ON f.ts_code = b.ts_code AND f.trade_date = m.future_date
                WHERE b.trade_date >= ?
                  AND b.close IS NOT NULL AND b.close > 0
                  AND f.close IS NOT NULL AND f.close > 0
                  AND COALESCE(b.is_suspended, 0) = 0
                ORDER BY b.trade_date ASC, b.ts_code ASC
                """,
                (
                    *values_params,
                    *sampled_symbols,
                    ascending_dates[-1],
                    start_date,
                ),
            ).fetchall()
        benchmark_rows = []
        if model_key == "event_surprise_lgbm_v1":
            try:
                benchmark_rows = c.execute(
                    """SELECT trade_date,payload_json,available_time
                       FROM dc_tushare_raw_records
                       WHERE api_name='index_daily'
                         AND ts_code IN ('000300.SH','000300')
                         AND trade_date>=? AND trade_date<=?
                       ORDER BY trade_date""",
                    (ascending_dates[0], ascending_dates[-1]),
                ).fetchall()
            except Exception:
                benchmark_rows = []
        try:
            state_rows = c.execute(
                """SELECT trade_date,market_state,observations_json,result_json
                   FROM dc_market_context_snapshot
                   WHERE trade_date>=? AND trade_date<=?""",
                (ascending_dates[0], ascending_dates[-1]),
            ).fetchall()
        except Exception:
            state_rows = []
        try:
            market_feature_rows = c.execute(
                """SELECT feature_date,features_json
                   FROM model_training_history
                   WHERE feature_set='market_regime_full_market_v2'
                     AND feature_date>=? AND feature_date<=?
                   ORDER BY feature_date""",
                (ascending_dates[0], ascending_dates[-1]),
            ).fetchall()
        except Exception:
            market_feature_rows = []
        try:
            if model_key == "narrative_transition_multitask_v1":
                theme_rows = c.execute(
                    """SELECT as_of_date,'' AS ts_code,theme_code,
                              score/100.0 AS theme_score,
                              lifecycle,payload_json,model_version
                       FROM dc_theme_snapshot
                       WHERE as_of_date>=? AND as_of_date<=?
                       ORDER BY as_of_date,theme_code""",
                    (ascending_dates[0], ascending_dates[-1]),
                ).fetchall()
            else:
                theme_symbol_placeholders = ",".join(
                    "?" for _ in sampled_symbols
                )
                theme_rows = c.execute(
                    f"""SELECT m.as_of_date,m.ts_code,m.theme_code,
                              s.score/100.0 AS theme_score,
                              s.lifecycle,s.payload_json,s.model_version
                       FROM dc_theme_company_mapping m
                       JOIN dc_theme_snapshot s
                         ON s.as_of_date=m.as_of_date AND s.theme_code=m.theme_code
                       WHERE m.as_of_date>=? AND m.as_of_date<=?
                         AND m.ts_code IN ({theme_symbol_placeholders})
                       ORDER BY m.as_of_date,m.ts_code,s.score DESC,m.theme_code""",
                    (
                        ascending_dates[0],
                        ascending_dates[-1],
                        *sampled_symbols,
                    ),
                ).fetchall()
        except Exception as exc:
            raise PlanModelTrainingDataError(
                "theme_mapping_query_failed",
                str(exc)[:300],
            ) from exc
        event_rows = []
        news_rows = []
        if model_key == "event_surprise_lgbm_v1":
            try:
                event_rows = c.execute(
                    """SELECT r.ts_code,REPLACE(SUBSTRING(r.publish_time,1,10),'-','') AS event_date,
                              r.available_time,
                              e.impact_direction,e.severity,e.confidence,e.summary
                       FROM dc_announcement_event e
                       JOIN dc_announcement_raw r ON r.id=e.announcement_id
                       WHERE r.available_time<=? AND r.ts_code<>''""",
                    (f"{ascending_dates[-1][:4]}-{ascending_dates[-1][4:6]}-{ascending_dates[-1][6:]}T23:59:59",),
                ).fetchall()
            except Exception:
                event_rows = []
            try:
                news_rows = c.execute(
                    """SELECT payload_json,available_time
                       FROM dc_tushare_raw_records
                       WHERE api_name='major_news'
                         AND available_time<=?
                       ORDER BY available_time""",
                    (f"{ascending_dates[-1][:4]}-{ascending_dates[-1][4:6]}-{ascending_dates[-1][6:]}T23:59:59",),
                ).fetchall()
            except Exception:
                news_rows = []
        moneyflow_rows = _load_training_moneyflow_rows(
            c,
            model_key=model_key,
            start_date=ascending_dates[0],
            end_date=ascending_dates[-1],
            symbols=sampled_symbols,
        )
    market_state_by_date: dict[str, str] = {}
    market_profile_by_date: dict[str, str] = {}
    for row in state_rows:
        item = dict(row)
        trade_date = str(item.get("trade_date") or "")
        market_state_by_date[trade_date] = str(item.get("market_state") or "unknown")
        result_payload = item.get("result_json") or {}
        observations = item.get("observations_json") or {}
        for name, value in (("result", result_payload), ("observations", observations)):
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except Exception:
                    value = {}
            if name == "result":
                result_payload = value if isinstance(value, dict) else {}
            else:
                observations = value if isinstance(value, dict) else {}
        profile = (result_payload.get("market_profile") or {}) if isinstance(result_payload, dict) else {}
        profile_code = str((profile or {}).get("code") or "")
        if not profile_code and observations:
            try:
                from app.services.market_context import classify_market_profile
                profile_code = str(
                    classify_market_profile(market_state_by_date[trade_date], observations).get("code")
                    or ""
                )
            except Exception:
                profile_code = ""
        market_profile_by_date[trade_date] = profile_code or "unknown"
    if market_feature_rows:
        try:
            from app.services.market_context import (
                _transparent_regime,
                classify_market_profile,
                score_market_sentiment,
            )
            previous_state = ""
            for row in market_feature_rows:
                item = dict(row)
                trade_date = str(item.get("feature_date") or "")
                features = item.get("features_json") or {}
                if isinstance(features, str):
                    try:
                        features = json.loads(features)
                    except Exception:
                        features = {}
                if not isinstance(features, dict):
                    continue
                observations = {
                    "breadth": float(features.get("market_breadth") or 0.5),
                    "average_return": float(features.get("market_average_return") or 0.0),
                    "volume_ratio": float(features.get("market_amount_ratio_20d") or 1.0),
                    "limit_up_ratio": float(features.get("market_limit_up_ratio") or 0.0),
                    "limit_down_ratio": float(features.get("market_limit_down_ratio") or 0.0),
                    "northbound_flow_z": 0.0,
                    "leader_persistence": 0.0,
                    "price_fundamental_divergence": 0.0,
                    "crowding": 0.0,
                }
                sentiment = score_market_sentiment(**observations)
                state = _transparent_regime(
                    float(sentiment.get("score") or 50.0),
                    observations["average_return"],
                    previous_state,
                )
                profile = classify_market_profile(state, observations)
                market_state_by_date[trade_date] = state
                market_profile_by_date[trade_date] = str(profile.get("code") or "unknown")
                previous_state = state
        except Exception:
            pass
    benchmark_close_by_date: dict[str, float] = {}
    for row in benchmark_rows:
        item = dict(row)
        payload = _payload_dict(item.get("payload_json"))
        try:
            close = float(payload.get("close") or 0.0)
        except (TypeError, ValueError):
            close = 0.0
        if close > 0:
            benchmark_close_by_date[str(item.get("trade_date") or "")] = close
    benchmark_return_by_date: dict[str, float] = {}
    for trade_date, future_date in mapped_dates:
        current = benchmark_close_by_date.get(trade_date)
        future = benchmark_close_by_date.get(future_date)
        if current and future:
            benchmark_return_by_date[trade_date] = float(future / current - 1.0)

    primary_theme_by_symbol_date: dict[
        tuple[str, str], tuple[float, str, str]
    ] = {}
    lifecycle_by_theme_date: dict[tuple[str, str], str] = {}
    lifecycle_code_by_theme_date: dict[tuple[str, str], int] = {}
    theme_observation_by_theme_date: dict[tuple[str, str], dict[str, Any]] = {}
    theme_score_by_theme_date: dict[tuple[str, str], float] = {}
    distinct_theme_rows: dict[tuple[str, str], dict[str, Any]] = {}
    persisted_lifecycle_counts: Counter[str] = Counter()
    for row in theme_rows:
        item = dict(row)
        theme_date = str(item.get("as_of_date") or "")
        theme_code = str(item.get("theme_code") or "")
        theme_score = float(item.get("theme_score") or 0.0)
        key = (theme_code, theme_date)
        if key not in distinct_theme_rows:
            distinct_theme_rows[key] = item
            persisted_lifecycle_counts[str(item.get("lifecycle") or "")] += 1
        theme_score_by_theme_date[(theme_code, theme_date)] = theme_score
    candidate_lifecycle_counts: Counter[int] = Counter()
    canonical_lifecycle_counts: Counter[int] = Counter()
    path_adjustment_count = 0
    observability_by_date: dict[str, list[bool]] = {}
    previous_state_by_theme: dict[str, int] = {}
    previous_observation_by_theme: dict[str, dict[str, Any]] = {}
    state_age_by_theme: dict[str, int] = {}
    for (theme_code, theme_date), item in sorted(
        distinct_theme_rows.items(),
        key=lambda value: (value[0][0], value[0][1]),
    ):
        theme_score = float(item.get("theme_score") or 0.0)
        observation = _canonical_narrative_observation(
            lifecycle=str(item.get("lifecycle") or ""),
            score=theme_score * 100.0,
            payload=_payload_dict(item.get("payload_json")),
        )
        candidate = int(observation["candidate_state"])
        observability_by_date.setdefault(theme_date, []).append(
            bool(observation.get("observable"))
        )
        previous_state = previous_state_by_theme.get(theme_code)
        canonical = _constrain_narrative_state(previous_state, candidate)
        candidate_lifecycle_counts[candidate] += 1
        canonical_lifecycle_counts[canonical] += 1
        path_adjustment_count += int(canonical != candidate)
        previous_observation = previous_observation_by_theme.get(theme_code) or {}
        if previous_state == canonical:
            state_age_by_theme[theme_code] = state_age_by_theme.get(theme_code, 0) + 1
        else:
            state_age_by_theme[theme_code] = 1
        observation.update({
            "state": canonical,
            "state_age": state_age_by_theme[theme_code],
            "score_slope": theme_score - float(
                previous_observation.get("theme_score") or theme_score
            ),
            "breadth_slope": float(observation["breadth"]) - float(
                previous_observation.get("breadth") or observation["breadth"]
            ),
            "theme_score": theme_score,
            "path_adjusted": canonical != candidate,
        })
        lifecycle_code_by_theme_date[(theme_code, theme_date)] = canonical
        lifecycle_by_theme_date[(theme_code, theme_date)] = (
            NARRATIVE_LIFECYCLE_CLASSES[canonical]
        )
        theme_observation_by_theme_date[(theme_code, theme_date)] = observation
        previous_state_by_theme[theme_code] = canonical
        previous_observation_by_theme[theme_code] = observation

    unobservable_theme_dates = {
        theme_date
        for theme_date, flags in observability_by_date.items()
        if flags and sum(not value for value in flags) / len(flags) >= .80
    }

    for (theme_code, theme_date), lifecycle in lifecycle_by_theme_date.items():
        primary_theme_by_symbol_date[(f"THEME:{theme_code}", theme_date)] = (
            float(theme_score_by_theme_date.get((theme_code, theme_date), 0.0)),
            lifecycle,
            theme_code,
        )

    for row in sorted(
        theme_rows,
        key=lambda value: (
            str(dict(value).get("as_of_date") or ""),
            str(dict(value).get("theme_code") or ""),
            -float(dict(value).get("theme_score") or 0.0),
        ),
    ):
        item = dict(row)
        theme_symbol = str(item.get("ts_code") or "")
        theme_date = str(item.get("as_of_date") or "")
        theme_code = str(item.get("theme_code") or "")
        theme_score = float(item.get("theme_score") or 0.0)
        lifecycle = lifecycle_by_theme_date.get((theme_code, theme_date), "")
        key = (theme_symbol, theme_date)
        previous = primary_theme_by_symbol_date.get(key)
        if previous is None or theme_score > float(previous[0]):
            primary_theme_by_symbol_date[key] = (
                theme_score,
                lifecycle,
                theme_code,
            )
    theme_pressure_by_theme_date = {
        key: max(-1.0, min(1.0, float(value.get("score_slope") or 0.0)))
        for key, value in theme_observation_by_theme_date.items()
    }
    theme_snapshot_dates = {
        str(dict(row).get("as_of_date") or "")
        for row in theme_rows
        if dict(row).get("as_of_date")
    }
    event_by_symbol_date: dict[tuple[str, str], tuple[float, float, str]] = {}
    direction_score = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}
    severity_score = {"low": 0.25, "medium": 0.5, "high": 0.75, "critical": 1.0}
    for row in event_rows:
        item = dict(row)
        available_time = str(item.get("available_time") or "")
        event_date = str(item.get("event_date") or "")
        usable_date = _event_usable_trade_date(event_date, available_time, ascending_dates)
        key = (str(item.get("ts_code") or ""), usable_date)
        if not usable_date:
            continue
        confidence = max(0.0, min(1.0, float(item.get("confidence") or 0.0)))
        sentiment = direction_score.get(str(item.get("impact_direction") or "neutral").lower(), 0.0)
        sentiment *= severity_score.get(str(item.get("severity") or "medium").lower(), 0.5)
        previous = event_by_symbol_date.get(key, (0.0, 0.0, ""))
        if confidence >= previous[1]:
            event_by_symbol_date[key] = (sentiment, confidence, "announcement")
    if news_rows:
        from app.services.news_theme_cluster import _match_companies
        for row in news_rows:
            item = dict(row)
            payload = _payload_dict(item.get("payload_json"))
            title = str(payload.get("title") or "")
            content = str(payload.get("content") or "")
            text = f"{title} {content}".strip()
            if not text:
                continue
            event_time = str(payload.get("pub_time") or "")
            available_time = str(item.get("available_time") or event_time)
            usable_date = _event_usable_trade_date(event_time, available_time, ascending_dates)
            if not usable_date:
                continue
            direction, severity, confidence = _event_news_direction(text)
            sentiment = direction_score[direction] * severity_score[severity]
            for company in _match_companies(text):
                key = (str(company.get("ts_code") or ""), usable_date)
                previous = event_by_symbol_date.get(key, (0.0, 0.0, ""))
                if key[0] and confidence >= previous[1]:
                    event_by_symbol_date[key] = (sentiment, confidence, "major_news")
    moneyflow_by_symbol_date = _moneyflow_observations(moneyflow_rows, ascending_dates)
    if on_progress:
        on_progress("dataset", 0.31, f"取回候选训练截面 {len(raw_rows)} 行，开始构造特征")
    samples: list[dict[str, Any]] = []
    for row in raw_rows:
        item = dict(row)
        close = float(item.get("close") or 0.0)
        future_close = float(item.get("future_close") or 0.0)
        if close <= 0 or future_close <= 0:
            continue
        if (
            item.get("momentum_20") is None
            or item.get("momentum_60") is None
            or item.get("volatility_20") is None
            or item.get("turnover_slope") is None
        ):
            continue
        momentum_20 = float(item.get("momentum_20") or 0.0)
        momentum_60 = float(item.get("momentum_60") or 0.0)
        volatility_20 = float(item.get("volatility_20") or 0.0)
        turnover_slope = float(item.get("turnover_slope") or 0.0)
        moneyflow = moneyflow_by_symbol_date.get((str(item.get("ts_code") or ""), str(item.get("trade_date") or "")))
        capital_persistence = (
            min(
                3.0,
                max(
                    -3.0,
                    float(moneyflow["net_flow_z"]) * 0.35
                    + float(moneyflow["positive_ratio_5"]) * 0.35
                    + float(moneyflow["positive_ratio_20"]) * 0.2
                    + float(moneyflow["large_order_balance"]) * 0.1,
                ),
            )
            if moneyflow
            else 0.0
        )
        narrative_strength = min(3.0, max(-3.0, momentum_60 - volatility_20))
        symbol = str(item.get("ts_code") or "")
        trade_date = str(item.get("trade_date") or "")
        future_date = str(item.get("future_date") or "")
        if (
            model_key == "narrative_transition_multitask_v1"
            and (
                trade_date not in theme_snapshot_dates
                or future_date not in theme_snapshot_dates
                or trade_date in unobservable_theme_dates
                or future_date in unobservable_theme_dates
            )
        ):
            continue
        theme_score, lifecycle, theme_code = primary_theme_by_symbol_date.get(
            (symbol, trade_date), (0.0, "", "")
        )
        if model_key == "leader_lambdarank_v1" and not theme_code:
            continue
        future_lifecycle = lifecycle_by_theme_date.get((theme_code, future_date), "")
        if model_key == "narrative_transition_multitask_v1" and (
            not theme_code
            or not lifecycle
            or not future_lifecycle
        ):
            continue
        if model_key == "narrative_transition_multitask_v1":
            narrative_strength = theme_score
        current_lifecycle_class = int(
            lifecycle_code_by_theme_date.get((theme_code, trade_date), 0)
        )
        lifecycle_code = float(current_lifecycle_class) / float(
            len(NARRATIVE_LIFECYCLE_CLASSES) - 1
        )
        future_lifecycle_code = int(
            lifecycle_code_by_theme_date.get(
                (theme_code, future_date),
                current_lifecycle_class,
            )
        )
        transition_pressure = theme_pressure_by_theme_date.get(
            (theme_code, trade_date),
            0.0,
        )
        narrative_observation = theme_observation_by_theme_date.get(
            (theme_code, trade_date),
            {},
        )
        if model_key == "narrative_transition_multitask_v1":
            capital_persistence = float(
                narrative_observation.get("capital_persistence") or 0.0
            )
        event_text_sentiment, event_text_confidence, event_source_type = event_by_symbol_date.get(
            (symbol, trade_date), (0.0, 0.0, "")
        )
        if model_key == "event_surprise_lgbm_v1" and event_text_confidence <= 0:
            continue
        event_surprise = min(
            3.0,
            max(-3.0, event_text_sentiment * max(0.0, event_text_confidence)),
        )
        future_return = future_close / close - 1.0
        benchmark_return = benchmark_return_by_date.get(trade_date)
        if model_key == "event_surprise_lgbm_v1" and benchmark_return is None:
            continue
        future_abnormal_return = (
            future_return - float(benchmark_return)
            if benchmark_return is not None
            else future_return
        )
        if model_key == "capital_persistence_multitask_v1" and not moneyflow:
            continue
        future_capital_persistence = min(
            3.0,
            max(-3.0, float((moneyflow or {}).get("future_persistence") or 0.0)),
        )
        samples.append({
            "ts_code": symbol,
            "trade_date": trade_date,
            "features": [
                momentum_20,
                momentum_60,
                volatility_20,
                turnover_slope,
                capital_persistence,
                narrative_strength,
                event_surprise,
                event_text_sentiment,
                event_text_confidence,
                lifecycle_code,
                transition_pressure,
                float(narrative_observation.get("breadth") or 0.0),
                float(narrative_observation.get("crowding") or 0.0),
                float(narrative_observation.get("score_slope") or 0.0),
                float(narrative_observation.get("breadth_slope") or 0.0),
                min(
                    1.0,
                    float(narrative_observation.get("state_age") or 0.0) / 60.0,
                ),
            ],
            "future_return": future_return,
            "future_abnormal_return": future_abnormal_return,
            "future_capital_persistence": future_capital_persistence,
            "event_source_type": event_source_type,
            "theme_code": theme_code,
            "market_state": market_state_by_date.get(str(item.get("trade_date") or ""), "unknown"),
            "current_lifecycle_class": current_lifecycle_class,
            "future_lifecycle_class": future_lifecycle_code,
            "ranking_query": (
                f"{trade_date}:{theme_code}"
                if model_key == "leader_lambdarank_v1"
                else trade_date
            ),
            "market_profile": market_profile_by_date.get(trade_date, "unknown"),
            "environment_id": f"{market_profile_by_date.get(trade_date, 'unknown')}:{lifecycle or '无主题'}",
        })
    if model_key == "narrative_transition_multitask_v1":
        theme_samples: list[dict[str, Any]] = []
        by_theme_date: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in samples:
            key = (
                str(item.get("trade_date") or ""),
                str(item.get("theme_code") or ""),
            )
            if key[0] and key[1]:
                by_theme_date.setdefault(key, []).append(item)
        for (trade_date, theme_code), members in by_theme_date.items():
            first = members[0]
            theme_samples.append({
                **first,
                "ts_code": f"THEME:{theme_code}",
                "features": np.mean(
                    np.asarray([item["features"] for item in members], dtype=float),
                    axis=0,
                ).tolist(),
                "future_return": float(np.mean([
                    float(item["future_return"]) for item in members
                ])),
                "future_abnormal_return": float(np.mean([
                    float(item["future_abnormal_return"]) for item in members
                ])),
                "future_capital_persistence": float(np.mean([
                    float(item["future_capital_persistence"]) for item in members
                ])),
                "ranking_query": f"{trade_date}:{theme_code}",
                "theme_member_count": len(members),
            })
        samples = theme_samples
    if len(samples) < 500:
        raise RuntimeError(f"训练样本不足：{len(samples)} rows")
    if model_key == "leader_lambdarank_v1":
        query_counts = Counter(str(item.get("ranking_query") or "") for item in samples)
        samples = [
            item for item in samples
            if query_counts[str(item.get("ranking_query") or "")] >= 5
        ]
        if len(samples) < 500:
            raise RuntimeError(f"龙头模型主题内有效训练样本不足：{len(samples)} rows")
    if on_progress:
        on_progress("dataset", 0.34, f"有效样本 {len(samples)} 行，特征 {len(PLAN_MODEL_FEATURES)} 个")
    samples.sort(
        key=lambda x: (
            str(x["trade_date"]),
            str(x.get("ranking_query") or ""),
            str(x.get("ts_code") or ""),
        )
    )
    returns = np.asarray([float(x["future_return"]) for x in samples], dtype=float)
    abnormal_returns = np.asarray(
        [float(x["future_abnormal_return"]) for x in samples],
        dtype=float,
    )
    capital_targets = np.asarray(
        [float(x["future_capital_persistence"]) for x in samples],
        dtype=float,
    )
    labels = np.zeros(len(samples), dtype=int)
    daily_label_quantiles: dict[str, list[float]] = {}
    indices_by_date: dict[str, list[int]] = {}
    for index, sample in enumerate(samples):
        label_query = (
            str(sample.get("ranking_query") or "")
            if model_key == "leader_lambdarank_v1"
            else str(sample["trade_date"])
        )
        indices_by_date.setdefault(label_query, []).append(index)
    label_source = (
        capital_targets
        if model_key == "capital_persistence_multitask_v1"
        else abnormal_returns
        if model_key == "event_surprise_lgbm_v1"
        else returns
    )
    for sample_date, indices in indices_by_date.items():
        values = label_source[indices]
        if len(values) < 5:
            continue
        quantiles = np.quantile(values, [0.2, 0.4, 0.6, 0.8])
        labels[indices] = np.digitize(values, quantiles).astype(int)
        daily_label_quantiles[sample_date] = [float(value) for value in quantiles]
    if on_progress:
        label_name = (
            "未来真实资金持续标签"
            if model_key == "capital_persistence_multitask_v1"
            else "沪深300市场调整后异常收益标签"
            if model_key == "event_surprise_lgbm_v1"
            else "未来收益标签"
        )
        on_progress("dataset", 0.37, f"{label_name}完成：按 20/40/60/80 分位分成 5 档")
    x = np.asarray([x["features"] for x in samples], dtype=float)
    dates = [str(x["trade_date"]) for x in samples]
    ranking_queries = [str(x.get("ranking_query") or x["trade_date"]) for x in samples]
    symbols = [str(x.get("ts_code") or "") for x in samples]
    market_states = [str(x.get("market_state") or "unknown") for x in samples]
    environments = [str(x.get("environment_id") or "unknown") for x in samples]
    market_profiles = [str(x.get("market_profile") or "unknown") for x in samples]
    current_lifecycle_labels = np.asarray(
        [int(x.get("current_lifecycle_class") or 0) for x in samples],
        dtype=int,
    )
    lifecycle_labels = np.asarray([int(x.get("future_lifecycle_class") or 0) for x in samples], dtype=int)
    unique_dates = sorted(set(dates))
    n_dates = len(unique_dates)
    final_window = _walk_forward_date_windows(unique_dates)[-1]
    train_dates_set = set(final_window["train"])
    validation_dates_set = set(final_window["validation"])
    test_dates_set = set(final_window["test"])
    train_mask = np.asarray([date in train_dates_set for date in dates], dtype=bool)
    validation_mask = np.asarray([date in validation_dates_set for date in dates], dtype=bool)
    test_mask = np.asarray([date in test_dates_set for date in dates], dtype=bool)
    train_end = final_window["train"][-1]
    validation_start = final_window["validation"][0]
    validation_end = final_window["validation"][-1]
    test_start = final_window["test"][0]
    if on_progress:
        on_progress("dataset", 0.40, f"训练/验证/测试切分完成：训练 {int(train_mask.sum())} 行，验证 {int(validation_mask.sum())} 行，测试 {int(test_mask.sum())} 行")
    def _groups_for(mask: np.ndarray) -> list[int]:
        counts = Counter(
            query
            for query, in_split in zip(ranking_queries, mask)
            if bool(in_split)
        )
        ordered_queries = list(dict.fromkeys(
            query for query, in_split in zip(ranking_queries, mask) if bool(in_split)
        ))
        return [counts[query] for query in ordered_queries if counts[query]]
    return {
        "x_all": x,
        "y_all_rank": labels,
        "y_all_return": returns,
        "y_all_abnormal_return": abnormal_returns,
        "y_all_task_target": label_source,
        "y_all_current_lifecycle": current_lifecycle_labels,
        "y_all_lifecycle": lifecycle_labels,
        "all_dates": dates,
        "all_ranking_queries": ranking_queries,
        "all_symbols": symbols,
        "all_market_states": market_states,
        "all_market_profiles": market_profiles,
        "all_environments": environments,
        "_train_mask": train_mask,
        "_validation_mask": validation_mask,
        "_test_mask": test_mask,
        "x_train": x[train_mask],
        "y_train_rank": labels[train_mask],
        "y_train_return": returns[train_mask],
        "y_train_abnormal_return": abnormal_returns[train_mask],
        "y_train_task_target": label_source[train_mask],
        "y_train_current_lifecycle": current_lifecycle_labels[train_mask],
        "y_train_lifecycle": lifecycle_labels[train_mask],
        "x_val": x[validation_mask],
        "y_val_rank": labels[validation_mask],
        "y_val_return": returns[validation_mask],
        "y_val_abnormal_return": abnormal_returns[validation_mask],
        "y_val_task_target": label_source[validation_mask],
        "y_val_current_lifecycle": current_lifecycle_labels[validation_mask],
        "y_val_lifecycle": lifecycle_labels[validation_mask],
        "x_test": x[test_mask],
        "y_test_rank": labels[test_mask],
        "y_test_return": returns[test_mask],
        "y_test_abnormal_return": abnormal_returns[test_mask],
        "y_test_task_target": label_source[test_mask],
        "y_test_current_lifecycle": current_lifecycle_labels[test_mask],
        "y_test_lifecycle": lifecycle_labels[test_mask],
        "val_dates": [date for date, in_split in zip(dates, validation_mask) if bool(in_split)],
        "val_symbols": [symbol for symbol, in_split in zip(symbols, validation_mask) if bool(in_split)],
        "test_dates": [date for date, in_split in zip(dates, test_mask) if bool(in_split)],
        "test_symbols": [symbol for symbol, in_split in zip(symbols, test_mask) if bool(in_split)],
        "val_market_states": [state for state, in_split in zip(market_states, validation_mask) if bool(in_split)],
        "train_market_states": [state for state, in_split in zip(market_states, train_mask) if bool(in_split)],
        "test_market_states": [state for state, in_split in zip(market_states, test_mask) if bool(in_split)],
        "test_environments": [environment for environment, in_split in zip(environments, test_mask) if bool(in_split)],
        "test_market_profiles": [profile for profile, in_split in zip(market_profiles, test_mask) if bool(in_split)],
        "group_train": _groups_for(train_mask),
        "group_val": _groups_for(validation_mask),
        "sample_rows": len(samples),
        "raw_rows": len(raw_rows),
        "train_rows": int(train_mask.sum()),
        "validation_rows": int(validation_mask.sum()),
        "test_rows": int(test_mask.sum()),
        "train_trade_date_count": len(train_dates_set),
        "validation_trade_date_count": len(validation_dates_set),
        "test_trade_date_count": len(test_dates_set),
        "daily_sample_limit": daily_sample_limit,
        "symbol_sampling": symbol_sampling,
        "sampled_symbol_count": len(sampled_symbols),
        "moneyflow_raw_rows": len(moneyflow_rows),
        "capital_observation_count": len(moneyflow_by_symbol_date),
        "dates": {"start": unique_dates[0], "train_end": train_end, "validation_start": validation_start, "validation_end": validation_end, "test_start": test_start, "end": unique_dates[-1], "split": validation_start},
        "horizon": horizon,
        "label_quantiles": {
            "contract": "per_trade_date_cross_sectional_quintiles_v1",
            "date_count": len(daily_label_quantiles),
        },
        "feature_count": len(PLAN_MODEL_FEATURES),
        "feature_computation_contract": {
            "momentum_20": "point_in_time_close_return_20_sessions_v1",
            "momentum_60": "point_in_time_close_return_60_sessions_v1",
            "volatility_20": "point_in_time_realized_return_volatility_20_sessions_v1",
            "turnover_slope": "point_in_time_turnover_average_20_vs_60_sessions_v1",
            "capital_persistence": "real_stock_moneyflow_5d_20d_persistence_and_large_order_balance_v2",
            "capital_target": "future_10_session_real_stock_moneyflow_persistence_v2",
            "capital_source_availability": "official_trade_date_close_time_v1_ingestion_time_retained_in_updated_at",
            "event_surprise": "point_in_time_announcement_and_strict_company_mapped_major_news_v2",
            "event_target": "csi300_market_adjusted_abnormal_return_5d_v1",
            "narrative_transition_pressure": "current_vs_previous_available_theme_snapshot_v1",
            "narrative_target": "same_theme_three_session_observable_four_state_transition_v2",
            "narrative_lifecycle": "formation_diffusion_mature_crowded_decline_observable_graph_v2",
            "narrative_state_age": "same_theme_consecutive_canonical_state_sessions_capped_60_v1",
            "leader_ranking_query": "trade_date_x_available_theme_code_minimum_five_members_v1",
        },
        "narrative_label_audit": {
            "schema_version": "alis_narrative_label_audit_v2",
            "states": list(NARRATIVE_LIFECYCLE_CLASSES),
            "persisted_lifecycle_counts": dict(persisted_lifecycle_counts),
            "observable_candidate_counts": {
                NARRATIVE_LIFECYCLE_CLASSES[index]: int(
                    candidate_lifecycle_counts.get(index, 0)
                )
                for index in range(len(NARRATIVE_LIFECYCLE_CLASSES))
            },
            "canonical_path_counts": {
                NARRATIVE_LIFECYCLE_CLASSES[index]: int(
                    canonical_lifecycle_counts.get(index, 0)
                )
                for index in range(len(NARRATIVE_LIFECYCLE_CLASSES))
            },
            "path_adjustment_count": int(path_adjustment_count),
            "unobservable_snapshot_date_count": len(
                unobservable_theme_dates
            ),
            "unobservable_snapshot_dates": sorted(
                unobservable_theme_dates
            )[:20],
            "allowed_destinations": {
                NARRATIVE_LIFECYCLE_CLASSES[source]: [
                    NARRATIVE_LIFECYCLE_CLASSES[destination]
                    for destination in destinations
                ]
                for source, destinations
                in NARRATIVE_ALLOWED_DESTINATIONS.items()
            },
            "three_session_reachable_destinations": {
                NARRATIVE_LIFECYCLE_CLASSES[source]: [
                    NARRATIVE_LIFECYCLE_CLASSES[destination]
                    for destination in destinations
                ]
                for source, destinations
                in NARRATIVE_HORIZON_DESTINATIONS.items()
            },
            "no_synthetic_samples": True,
            "oversampling_forbidden": True,
        },
        "trade_date_count": len(unique_dates),
        "training_protocol": TRAINING_PROTOCOL,
    }


def _observation_counts(
    values: list[str],
    *,
    expected: list[str],
) -> dict[str, int]:
    """Record environment coverage without turning it into an alpha metric."""

    counts = Counter(str(value) for value in values)
    return {key: int(counts.get(key, 0)) for key in expected}


def _fit_lightgbm_plan_model(
    model_key: str,
    dataset: dict[str, Any],
    *,
    candidate_spec: dict[str, Any] | None = None,
    quick_diagnostic: bool = False,
) -> dict[str, Any]:
    try:
        import lightgbm as lgb
    except Exception as exc:
        raise RuntimeError(f"LightGBM不可用：{exc}") from exc

    candidate_spec = dict(candidate_spec or {})
    allowed_overrides = {
        "learning_rate", "num_leaves", "max_depth", "min_child_samples",
        "feature_fraction", "bagging_fraction", "reg_alpha", "reg_lambda",
    }
    params = {k: v for k, v in LIGHTGBM_PARAMS.items() if k != "early_stopping_rounds"}
    params.update({
        key: value
        for key, value in dict(candidate_spec.get("param_overrides") or {}).items()
        if key in allowed_overrides
    })
    default_features = (
        NARRATIVE_MODEL_FEATURES
        if model_key == "narrative_transition_multitask_v1"
        else PLAN_MODEL_FEATURES
    )
    selected_features = [
        feature
        for feature in candidate_spec.get("feature_subset") or default_features
        if feature in PLAN_MODEL_FEATURES
    ]
    if (
        model_key != "narrative_transition_multitask_v1"
        and "momentum_20" not in selected_features
    ):
        selected_features.append("momentum_20")
    if (
        model_key == "narrative_transition_multitask_v1"
        and "narrative_lifecycle_code" not in selected_features
    ):
        selected_features.append("narrative_lifecycle_code")
    feature_indices = [PLAN_MODEL_FEATURES.index(feature) for feature in selected_features]
    requested_loss = str(candidate_spec.get("loss_function") or "")
    rank_objective = requested_loss if requested_loss in {"lambdarank", "rank_xendcg"} else "lambdarank"
    regression_objective = requested_loss if requested_loss in {"regression", "huber"} else "regression"
    multiclass_objective = requested_loss if requested_loss in {"multiclass", "multiclassova"} else "multiclass"

    def _select(values: Any) -> np.ndarray:
        return np.asarray(values, dtype=float)[:, feature_indices]

    x_train = _select(dataset["x_train"])
    x_val = _select(dataset["x_val"])
    sample_weight = dataset.get("train_sample_weight")

    def _fit_narrative_bundle(
        train_values: np.ndarray,
        train_current: np.ndarray,
        train_future: np.ndarray,
        *,
        fit_params: dict[str, Any],
        weights: np.ndarray | None = None,
        validation_values: np.ndarray | None = None,
        validation_current: np.ndarray | None = None,
        validation_future: np.ndarray | None = None,
    ) -> dict[str, Any]:
        train_current = np.asarray(train_current, dtype=int)
        train_future = np.asarray(train_future, dtype=int)
        transition_target = (train_future != train_current).astype(int)
        transition_counts = np.bincount(transition_target, minlength=2).astype(float)
        if int(np.count_nonzero(transition_counts)) < 2:
            transition_classifier: Any = _ConstantProbabilityClassifier(
                (transition_counts + 1.0).tolist()
            )
        else:
            transition_classifier = lgb.LGBMClassifier(
                objective="binary",
                metric="binary_logloss",
                **fit_params,
            )
            transition_fit: dict[str, Any] = {}
            if weights is not None:
                transition_fit["sample_weight"] = weights
            if (
                validation_values is not None
                and validation_current is not None
                and validation_future is not None
            ):
                validation_transition = (
                    np.asarray(validation_future, dtype=int)
                    != np.asarray(validation_current, dtype=int)
                ).astype(int)
                if len(np.unique(validation_transition)) >= 2:
                    transition_fit.update({
                        "eval_set": [(validation_values, validation_transition)],
                        "callbacks": [
                            lgb.early_stopping(
                                int(
                                    LIGHTGBM_PARAMS.get(
                                        "early_stopping_rounds",
                                    )
                                    or 50
                                ),
                                verbose=False,
                            )
                        ],
                    })
            transition_classifier.fit(
                train_values,
                transition_target,
                **transition_fit,
            )

        changed = transition_target.astype(bool)
        destination_counts = np.bincount(
            train_future[changed],
            minlength=len(NARRATIVE_LIFECYCLE_CLASSES),
        ).astype(float)
        if (
            int(changed.sum()) < 20
            or int(np.count_nonzero(destination_counts)) < 2
        ):
            destination_classifier: Any = _ConstantProbabilityClassifier(
                (destination_counts + 1.0).tolist()
            )
        else:
            destination_classifier = lgb.LGBMClassifier(
                objective=multiclass_objective,
                num_class=len(NARRATIVE_LIFECYCLE_CLASSES),
                metric="multi_logloss",
                **fit_params,
            )
            destination_fit: dict[str, Any] = {}
            if weights is not None:
                destination_fit["sample_weight"] = weights[changed]
            if (
                validation_values is not None
                and validation_current is not None
                and validation_future is not None
            ):
                validation_changed = (
                    np.asarray(validation_future, dtype=int)
                    != np.asarray(validation_current, dtype=int)
                )
                validation_destinations = np.asarray(
                    validation_future,
                    dtype=int,
                )[validation_changed]
                if (
                    int(validation_changed.sum()) >= 20
                    and len(np.unique(validation_destinations)) >= 2
                ):
                    destination_fit.update({
                        "eval_set": [(
                            validation_values[validation_changed],
                            validation_destinations,
                        )],
                        "callbacks": [
                            lgb.early_stopping(
                                int(
                                    LIGHTGBM_PARAMS.get(
                                        "early_stopping_rounds",
                                    )
                                    or 50
                                ),
                                verbose=False,
                            )
                        ],
                    })
            destination_classifier.fit(
                train_values[changed],
                train_future[changed],
                **destination_fit,
            )
        bundle = {
            "bundle_type": "narrative_lifecycle_two_stage_v2",
            "transition_classifier": transition_classifier,
            "destination_classifier": destination_classifier,
            "classes": list(NARRATIVE_LIFECYCLE_CLASSES),
            "allowed_destinations": {
                int(source): list(destinations)
                for source, destinations
                in NARRATIVE_HORIZON_DESTINATIONS.items()
            },
            "daily_transition_graph": {
                int(source): list(destinations)
                for source, destinations
                in NARRATIVE_ALLOWED_DESTINATIONS.items()
            },
            "training_counts": {
                "observations": int(len(train_values)),
                "transitions": int(changed.sum()),
                "transition_rate": round(float(changed.mean()), 8),
                "destination_support": destination_counts.astype(int).tolist(),
            },
            "no_oversampling": True,
        }
        if (
            validation_values is not None
            and validation_current is not None
            and validation_future is not None
        ):
            validation_current_values = np.asarray(
                validation_current,
                dtype=int,
            )
            validation_future_values = np.asarray(
                validation_future,
                dtype=int,
            )
            validation_transition = (
                validation_future_values != validation_current_values
            ).astype(int)
            transition_probabilities = _canonical_class_probabilities(
                transition_classifier,
                validation_values,
                class_count=2,
            )
            bundle["transition_temperature"] = _validation_temperature(
                transition_probabilities,
                validation_transition,
            )
            validation_changed = validation_transition.astype(bool)
            if int(validation_changed.sum()) >= 20:
                destination_probabilities = _canonical_class_probabilities(
                    destination_classifier,
                    validation_values[validation_changed],
                    class_count=len(NARRATIVE_LIFECYCLE_CLASSES),
                )
                bundle["destination_temperature"] = _validation_temperature(
                    destination_probabilities,
                    validation_future_values[validation_changed],
                )
        bundle.setdefault("transition_temperature", 1.0)
        bundle.setdefault("destination_temperature", 1.0)
        bundle["transition_logit_bias"] = 0.0
        bundle["calibration"] = {
            "contract": "validation_only_temperature_scaling_v2",
            "transition_temperature": bundle["transition_temperature"],
            "destination_temperature": bundle["destination_temperature"],
            "transition_logit_bias": 0.0,
        }
        return bundle

    if model_key in {"medium_horizon_lambdarank_v1", "leader_lambdarank_v1"}:
        model = lgb.LGBMRanker(objective=rank_objective, metric="ndcg", label_gain=PLAN_MODELS[model_key].get("label_gain", [0, 1, 3, 7, 15]), **params)
        fit_kwargs = {"group": dataset["group_train"], "eval_group": [dataset["group_val"]]}
        y_train = dataset["y_train_rank"]
        y_val = dataset["y_val_rank"]
    elif model_key == "capital_persistence_multitask_v1":
        direction = lgb.LGBMClassifier(objective=multiclass_objective, num_class=5, metric="multi_logloss", **params)
        direction.fit(x_train, dataset["y_train_rank"], eval_set=[(x_val, dataset["y_val_rank"])], callbacks=[lgb.early_stopping(int(LIGHTGBM_PARAMS.get("early_stopping_rounds") or 50), verbose=False)], **({"sample_weight": sample_weight} if sample_weight is not None else {}))
        quantile_models: dict[str, Any] = {}
        for name, alpha in (("q10", 0.10), ("q50", 0.50), ("q90", 0.90)):
            quantile = lgb.LGBMRegressor(objective="quantile", alpha=alpha, metric="quantile", **params)
            quantile.fit(x_train, dataset["y_train_task_target"], eval_set=[(x_val, dataset["y_val_task_target"])], callbacks=[lgb.early_stopping(int(LIGHTGBM_PARAMS.get("early_stopping_rounds") or 50), verbose=False)], **({"sample_weight": sample_weight} if sample_weight is not None else {}))
            quantile_models[name] = quantile
        hazard = lgb.LGBMClassifier(objective="binary", metric="binary_logloss", **params)
        hazard.fit(x_train, (np.asarray(dataset["y_train_task_target"]) < 0).astype(int), eval_set=[(x_val, (np.asarray(dataset["y_val_task_target"]) < 0).astype(int))], callbacks=[lgb.early_stopping(int(LIGHTGBM_PARAMS.get("early_stopping_rounds") or 50), verbose=False)], **({"sample_weight": sample_weight} if sample_weight is not None else {}))
        model = {"bundle_type": "capital_persistence_multihead_v1", "direction": direction, **quantile_models, "hazard": hazard}
        fit_kwargs = {}
        y_train = dataset["y_train_rank"]
        y_val = dataset["y_val_rank"]
    elif model_key == "narrative_transition_multitask_v1":
        model = _fit_narrative_bundle(
            x_train,
            dataset["y_train_current_lifecycle"],
            dataset["y_train_lifecycle"],
            fit_params=params,
            weights=sample_weight,
            validation_values=x_val,
            validation_current=dataset["y_val_current_lifecycle"],
            validation_future=dataset["y_val_lifecycle"],
        )
        fit_kwargs = {}
        y_train = dataset["y_train_lifecycle"]
        y_val = dataset["y_val_lifecycle"]
    else:
        model = lgb.LGBMRegressor(objective=regression_objective, metric="l2", **params)
        fit_kwargs = {}
        y_train = (
            dataset["y_train_abnormal_return"]
            if model_key == "event_surprise_lgbm_v1"
            else dataset["y_train_return"]
        )
        y_val = (
            dataset["y_val_abnormal_return"]
            if model_key == "event_surprise_lgbm_v1"
            else dataset["y_val_return"]
        )
    if sample_weight is not None:
        fit_kwargs["sample_weight"] = sample_weight
    callbacks = [lgb.early_stopping(int(LIGHTGBM_PARAMS.get("early_stopping_rounds") or 50), verbose=False)]
    if not isinstance(model, dict) or model.get("bundle_type") == "narrative_transition_classifier_v1":
        fit_model = model["classifier"] if isinstance(model, dict) else model
        fit_model.fit(x_train, y_train, eval_set=[(x_val, y_val)], callbacks=callbacks, **fit_kwargs)

    def _task_native_evaluation(
        fitted: Any,
        train_mask: np.ndarray,
        evaluation_mask: np.ndarray,
    ) -> dict[str, Any]:
        all_values = np.asarray(dataset["x_all"], dtype=float)
        selected_values = _select(all_values)
        evaluation_values = selected_values[evaluation_mask]
        all_dates_values = list(dataset.get("all_dates") or [])
        evaluation_dates = [
            str(value)
            for value, selected in zip(all_dates_values, evaluation_mask)
            if bool(selected)
        ]
        train_dates = [
            str(value)
            for value, selected in zip(all_dates_values, train_mask)
            if bool(selected)
        ]
        if model_key in {
            "medium_horizon_lambdarank_v1",
            "leader_lambdarank_v1",
        }:
            all_queries = list(
                dataset.get("all_ranking_queries") or all_dates_values
            )
            return evaluate_learning_to_rank(
                model_key=model_key,
                relevance=np.asarray(dataset["y_all_rank"])[evaluation_mask],
                model_scores=_model_score(fitted, evaluation_values),
                baseline_scores=all_values[
                    evaluation_mask,
                    PLAN_MODEL_FEATURES.index("momentum_20"),
                ],
                groups=[
                    str(value)
                    for value, selected in zip(all_queries, evaluation_mask)
                    if bool(selected)
                ],
                eval_at=PLAN_MODELS[model_key].get("eval_at") or [5, 10, 20],
            )
        if model_key == "capital_persistence_multitask_v1":
            return evaluate_capital_multitask(
                direction_probabilities=_canonical_class_probabilities(
                    fitted["direction"],
                    evaluation_values,
                    class_count=5,
                ),
                actual_direction=np.asarray(dataset["y_all_rank"])[evaluation_mask],
                current_persistence=all_values[
                    evaluation_mask,
                    PLAN_MODEL_FEATURES.index("capital_persistence"),
                ],
                dates=evaluation_dates,
                train_current_persistence=all_values[
                    train_mask,
                    PLAN_MODEL_FEATURES.index("capital_persistence"),
                ],
                train_dates=train_dates,
                train_future_direction=np.asarray(dataset["y_all_rank"])[train_mask],
                actual_strength=np.asarray(
                    dataset["y_all_task_target"],
                    dtype=float,
                )[evaluation_mask],
                train_strength=np.asarray(
                    dataset["y_all_task_target"],
                    dtype=float,
                )[train_mask],
                q10=_predict_values(fitted["q10"], evaluation_values),
                q50=_predict_values(fitted["q50"], evaluation_values),
                q90=_predict_values(fitted["q90"], evaluation_values),
                failure_probability=_canonical_class_probabilities(
                    fitted["hazard"],
                    evaluation_values,
                    class_count=2,
                )[:, 1],
            )
        if model_key == "narrative_transition_multitask_v1":
            evaluation_current = np.asarray(
                dataset["y_all_current_lifecycle"],
                dtype=int,
            )[evaluation_mask]
            probabilities, transition_probability, destination_probabilities = (
                _narrative_two_stage_probabilities(
                    fitted,
                    evaluation_values,
                    evaluation_current,
                )
            )
            return evaluate_narrative_transition(
                probabilities=probabilities,
                transition_probability=transition_probability,
                destination_probabilities=destination_probabilities,
                actual_future=np.asarray(
                    dataset["y_all_lifecycle"],
                    dtype=int,
                )[evaluation_mask],
                current_state=evaluation_current,
                train_current_state=np.asarray(
                    dataset["y_all_current_lifecycle"],
                    dtype=int,
                )[train_mask],
                train_future_state=np.asarray(
                    dataset["y_all_lifecycle"],
                    dtype=int,
                )[train_mask],
            )
        if model_key == "event_surprise_lgbm_v1":
            return evaluate_event_abnormal_return(
                predictions=_predict_values(fitted, evaluation_values),
                actual_abnormal_return=np.asarray(
                    dataset["y_all_abnormal_return"],
                    dtype=float,
                )[evaluation_mask],
                train_abnormal_return=np.asarray(
                    dataset["y_all_abnormal_return"],
                    dtype=float,
                )[train_mask],
                target_contract="market_adjusted_abnormal_return_v1",
            )
        raise ValueError(f"unsupported task-native evaluation: {model_key}")

    task_metrics = _task_native_evaluation(
        model,
        np.asarray(dataset["_train_mask"], dtype=bool),
        np.asarray(dataset["_test_mask"], dtype=bool),
    )
    task_metric_passed = bool(task_metrics.get("passed"))
    market_state_observations = _observation_counts(
        list(dataset.get("test_market_states") or []),
        expected=["S0", "S1", "S2", "S3"],
    )
    market_profile_observations = _observation_counts(
        list(dataset.get("test_market_profiles") or []),
        expected=[f"P{index:02d}" for index in range(1, 13)],
    )
    market_state_coverage_complete = all(
        int(market_state_observations.get(state) or 0) > 0
        for state in ("S0", "S1", "S2", "S3")
    )
    covered_market_profiles = sum(
        1
        for index in range(1, 13)
        if int(market_profile_observations.get(f"P{index:02d}") or 0) > 0
    )
    market_profile_coverage_complete = covered_market_profiles >= 8
    all_dates = np.asarray(dataset.get("all_dates") or [], dtype=object)
    unique_all_dates = sorted({str(value) for value in all_dates.tolist()})
    fold_task_passes: list[bool] = []
    fold_task_baseline_passes: list[bool] = []
    fold_evidence: list[dict[str, Any]] = []
    fold_models: list[dict[str, Any]] = []
    fold_date_windows = _walk_forward_date_windows(unique_all_dates)
    if quick_diagnostic:
        fold_date_windows = fold_date_windows[-2:]
    fold_params = {
        **params,
        "n_estimators": min(
            200 if quick_diagnostic else 500,
            int(params.get("n_estimators") or 500),
        ),
    }
    for fold_no, fold_window in enumerate(fold_date_windows, start=1):
        train_dates = set(fold_window["train"])
        validation_dates = set(fold_window["validation"])
        evaluation_set = set(fold_window["test"])
        train_mask_fold = np.asarray([str(value) in train_dates for value in all_dates], dtype=bool)
        validation_mask_fold = np.asarray(
            [str(value) in validation_dates for value in all_dates],
            dtype=bool,
        )
        test_mask_fold = np.asarray([str(value) in evaluation_set for value in all_dates], dtype=bool)
        fold_sample_weight = None
        target_states = {
            str(item) for item in candidate_spec.get("target_market_states") or [] if str(item)
        }
        if target_states:
            fold_sample_weight = np.asarray([
                2.0 if str(state) in target_states else 1.0
                for state, selected in zip(dataset.get("all_market_states") or [], train_mask_fold)
                if selected
            ], dtype=float)
        if int(train_mask_fold.sum()) < 100 or int(test_mask_fold.sum()) < 50:
            raise RuntimeError(f"Walk-forward第{fold_no}窗样本行不足")
        x_all = _select(dataset["x_all"])
        if model_key in {"medium_horizon_lambdarank_v1", "leader_lambdarank_v1"}:
            fold_model = lgb.LGBMRanker(objective=rank_objective, metric="ndcg", label_gain=PLAN_MODELS[model_key].get("label_gain", [0, 1, 3, 7, 15]), **fold_params)
            all_queries = list(dataset.get("all_ranking_queries") or all_dates.tolist())
            group_counts = Counter(
                str(value)
                for value, selected in zip(all_queries, train_mask_fold)
                if bool(selected)
            )
            ordered_queries = list(dict.fromkeys(
                str(value)
                for value, selected in zip(all_queries, train_mask_fold)
                if bool(selected)
            ))
            groups = [group_counts[query] for query in ordered_queries if group_counts[query]]
            fold_model.fit(
                x_all[train_mask_fold],
                np.asarray(dataset["y_all_rank"])[train_mask_fold],
                group=[value for value in groups if value],
                **({"sample_weight": fold_sample_weight} if fold_sample_weight is not None else {}),
            )
        elif model_key == "capital_persistence_multitask_v1":
            direction = lgb.LGBMClassifier(
                objective=multiclass_objective,
                num_class=5,
                metric="multi_logloss",
                **fold_params,
            )
            direction.fit(
                x_all[train_mask_fold],
                np.asarray(dataset["y_all_rank"])[train_mask_fold],
                **({"sample_weight": fold_sample_weight} if fold_sample_weight is not None else {}),
            )
            quantile_models: dict[str, Any] = {}
            all_task_target = np.asarray(dataset["y_all_task_target"], dtype=float)
            for name, alpha in (("q10", 0.10), ("q50", 0.50), ("q90", 0.90)):
                quantile = lgb.LGBMRegressor(
                    objective="quantile",
                    alpha=alpha,
                    metric="quantile",
                    **fold_params,
                )
                quantile.fit(
                    x_all[train_mask_fold],
                    all_task_target[train_mask_fold],
                    **(
                        {"sample_weight": fold_sample_weight}
                        if fold_sample_weight is not None else {}
                    ),
                )
                quantile_models[name] = quantile
            hazard = lgb.LGBMClassifier(
                objective="binary",
                metric="binary_logloss",
                **fold_params,
            )
            hazard.fit(
                x_all[train_mask_fold],
                (all_task_target[train_mask_fold] < 0).astype(int),
                **(
                    {"sample_weight": fold_sample_weight}
                    if fold_sample_weight is not None else {}
                ),
            )
            fold_model = {
                "bundle_type": "capital_persistence_multihead_v1",
                "direction": direction,
                **quantile_models,
                "hazard": hazard,
            }
        elif model_key == "narrative_transition_multitask_v1":
            fold_model = _fit_narrative_bundle(
                x_all[train_mask_fold],
                np.asarray(
                    dataset["y_all_current_lifecycle"],
                    dtype=int,
                )[train_mask_fold],
                np.asarray(dataset["y_all_lifecycle"])[train_mask_fold],
                fit_params=fold_params,
                weights=fold_sample_weight,
                validation_values=x_all[validation_mask_fold],
                validation_current=np.asarray(
                    dataset["y_all_current_lifecycle"],
                    dtype=int,
                )[validation_mask_fold],
                validation_future=np.asarray(
                    dataset["y_all_lifecycle"],
                    dtype=int,
                )[validation_mask_fold],
            )
        else:
            fold_model = lgb.LGBMRegressor(objective=regression_objective, metric="l2", **fold_params)
            fold_model.fit(
                x_all[train_mask_fold],
                (
                    np.asarray(dataset["y_all_abnormal_return"], dtype=float)[train_mask_fold]
                    if model_key == "event_surprise_lgbm_v1"
                    else np.asarray(dataset["y_all_return"], dtype=float)[train_mask_fold]
                ),
                **({"sample_weight": fold_sample_weight} if fold_sample_weight is not None else {}),
            )
        fold_task = _task_native_evaluation(
            fold_model,
            train_mask_fold,
            test_mask_fold,
        )
        fold_task_passes.append(bool(fold_task.get("passed")))
        fold_task_baseline_passes.append(
            bool(fold_task.get("baseline_outperformed"))
        )
        fold_states = [
            str(value)
            for value, selected in zip(dataset.get("all_market_states") or [], test_mask_fold)
            if selected
        ]
        if isinstance(fold_model, dict):
            importance_model = (
                fold_model.get("direction")
                if fold_model.get("bundle_type")
                == "capital_persistence_multihead_v1"
                else fold_model.get("transition_classifier")
                if fold_model.get("bundle_type")
                == "narrative_lifecycle_two_stage_v2"
                else fold_model.get("classifier")
            )
        else:
            importance_model = fold_model
        raw_importance = getattr(importance_model, "feature_importances_", None)
        importance = (
            {
                feature: round(float(value), 6)
                for feature, value in zip(selected_features, np.asarray(raw_importance, dtype=float))
            }
            if raw_importance is not None else {}
        )
        fold_evidence.append({
            "fold": fold_no,
            "train_start": min(train_dates),
            "train_end": max(train_dates),
            "train_sessions": len(train_dates),
            "validation_start": min(validation_dates),
            "validation_end": max(validation_dates),
            "validation_sessions": len(validation_dates),
            "purge_sessions": int(TRAINING_PROTOCOL["purge_sessions"]),
            "embargo_sessions": int(TRAINING_PROTOCOL["embargo_sessions"]),
            "test_start": min(evaluation_set),
            "test_end": max(evaluation_set),
            "test_sessions": len(evaluation_set),
            "train_rows": int(train_mask_fold.sum()),
            "test_rows": int(test_mask_fold.sum()),
            "task_native_evaluation": fold_task,
            "task_native_passed": bool(fold_task.get("passed")),
            "market_state_observations": _observation_counts(
                fold_states,
                expected=["S0", "S1", "S2", "S3"],
            ),
            "feature_importance": importance,
        })
        fold_models.append({
            "fold": fold_no,
            "model": fold_model,
        })
    expected_windows = 2 if quick_diagnostic else int(TRAINING_PROTOCOL["walk_forward_windows"])
    if len(fold_task_passes) != expected_windows:
        raise RuntimeError(
            f"Walk-forward窗口不完整：实际 {len(fold_task_passes)}，要求 {expected_windows}"
        )
    minimum_task_windows = len(fold_task_passes) // 2 + 1
    task_native_oos_passed = (
        sum(fold_task_passes) >= minimum_task_windows
    )
    task_native_baseline_oos_passed = (
        sum(fold_task_baseline_passes) >= minimum_task_windows
    )
    model_type = (
        "plan_lightgbm_ranker"
        if model_key in {"medium_horizon_lambdarank_v1", "leader_lambdarank_v1"}
        else "capital_persistence_multihead_v1"
        if model_key == "capital_persistence_multitask_v1"
        else "narrative_lifecycle_two_stage_v2"
        if model_key == "narrative_transition_multitask_v1"
        else "event_surprise_structured_text_lgbm_v1"
    )
    return {
        "model": model,
        "fold_models": fold_models,
        "model_type": model_type,
        "metrics": {
            "model_key": model_key,
            "train_rows": dataset["train_rows"],
            "validation_rows": dataset["validation_rows"],
            "test_rows": dataset["test_rows"],
            "sample_rows": dataset["sample_rows"],
            "training_trade_date_count": dataset["train_trade_date_count"],
            "validation_trade_date_count": dataset["validation_trade_date_count"],
            "test_trade_date_count": dataset["test_trade_date_count"],
            "labeled_trade_date_count": dataset["trade_date_count"],
            "oos_windows": len(fold_task_passes),
            "model_evaluation_contract": MODEL_EVALUATION_CONTRACT,
            "model_evaluation_definition": evaluation_contract(model_key),
            "task_metrics": task_metrics,
            "task_metric_final_window_passed": task_metric_passed,
            "task_metric_passed": task_native_oos_passed,
            "task_native_oos": {
                "contract": "expanding_purged_walk_forward_task_native_majority_v1",
                "window_count": len(fold_task_passes),
                "minimum_passed_windows": minimum_task_windows,
                "passed_windows": sum(fold_task_passes),
                "baseline_outperformed_windows": sum(fold_task_baseline_passes),
                "passed": task_native_oos_passed,
                "baseline_outperformed": task_native_baseline_oos_passed,
                "policy_note": (
                    "指标定义来自模型任务标准；多数窗相对基线门是ALIS治理策略，"
                    "不是论文宣称的通用收益阈值"
                ),
            },
            "walk_forward_windows": len(fold_task_passes),
            "walk_forward_evidence": fold_evidence,
            "task_native_walk_forward_contract": (
                "expanding_756_train_126_validation_126_test_purged_task_native_v1"
            ),
            "evaluation_stage": "quick_two_window_diagnostic" if quick_diagnostic else "formal_five_window",
            "candidate_spec": candidate_spec,
            "selected_features": selected_features,
            "feature_computation_contract": dataset.get("feature_computation_contract") or {},
            "narrative_label_audit": dataset.get("narrative_label_audit") or {},
            "market_state_observations": market_state_observations,
            "market_profile_observations": market_profile_observations,
            "environment_coverage": {
                "parent_state_count": 4,
                "covered_parent_states": sum(
                    int(value) > 0 for value in market_state_observations.values()
                ),
                "profile_count": 12,
                "minimum_required_profiles": 8,
                "covered_profiles": covered_market_profiles,
                "parent_states_complete": market_state_coverage_complete,
                "profiles_complete": market_profile_coverage_complete,
                "complete": market_state_coverage_complete and market_profile_coverage_complete,
                "contract": (
                    "point_in_time_four_parent_states_and_at_least_eight_"
                    "of_twelve_profiles_coverage_v1"
                ),
            },
            "task_baseline": {
                "contract": str(task_metrics.get("contract") or ""),
                "comparison_executed": True,
                "outperformed": task_native_baseline_oos_passed,
                "walk_forward_windows_outperformed": sum(
                    fold_task_baseline_passes
                ),
            },
            "horizon": dataset["horizon"],
            "date_start": dataset["dates"]["start"],
            "date_split": dataset["dates"]["split"],
            "date_end": dataset["dates"]["end"],
            "date_train_end": dataset["dates"].get("train_end"),
            "date_validation_start": dataset["dates"].get("validation_start"),
            "date_validation_end": dataset["dates"].get("validation_end"),
            "date_test_start": dataset["dates"].get("test_start"),
        },
    }


def _dataset_signature(model_key: str, dataset: dict[str, Any]) -> str:
    payload = {
        "model_key": model_key,
        "rows": {
            "sample": dataset.get("sample_rows"),
            "train": dataset.get("train_rows"),
            "validation": dataset.get("validation_rows"),
            "raw": dataset.get("raw_rows"),
        },
        "dates": dataset.get("dates") or {},
        "horizon": dataset.get("horizon"),
        "feature_count": dataset.get("feature_count"),
        "features": PLAN_MODEL_FEATURES,
        "feature_computation_contract": dataset.get("feature_computation_contract") or {},
        "symbol_sampling": dataset.get("symbol_sampling") or {},
        "moneyflow_evidence": {
            "raw_rows": dataset.get("moneyflow_raw_rows"),
            "capital_observation_count": dataset.get("capital_observation_count"),
        },
        "algorithm_contract": {
            "medium_horizon_lambdarank_v1": "grouped_ndcg_spearman_lambdarank_v4",
            "leader_lambdarank_v1": "within_theme_ndcg_spearman_lambdarank_v4",
            "capital_persistence_multitask_v1": "direction_quantile_hazard_task_native_v4",
            "event_surprise_lgbm_v1": "market_adjusted_abnormal_return_task_native_v1",
            "narrative_transition_multitask_v1": "three_session_two_stage_observable_lifecycle_task_native_v2",
        }.get(model_key),
        "model_evaluation_contract": MODEL_EVALUATION_CONTRACT,
        "trade_date_count": dataset.get("trade_date_count"),
        "label_quantiles": dataset.get("label_quantiles") or [],
        "narrative_label_audit": dataset.get("narrative_label_audit") or {},
        "training_protocol": TRAINING_PROTOCOL,
        "params": LIGHTGBM_PARAMS,
        "targeted_training_context": dataset.get("targeted_training_context") or {},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_duplicate_training_dataset(model_key: str, dataset: dict[str, Any]) -> tuple[bool, dict[str, Any] | None, str]:
    signature = _dataset_signature(model_key, dataset)
    latest = next((item for item in list_model_versions(model_key, limit=5) if str(item.get("status") or "") != "rejected"), None)
    metrics = (latest or {}).get("metrics") or {}
    if metrics.get("training_data_signature") == signature:
        return True, latest, signature
    return False, latest, signature


def _base_coverage_snapshot() -> dict[str, int]:
    from app.data_platform.db import _conn as data_conn

    def count_distinct(c: Any, table: str, candidates: list[str]) -> int:
        try:
            rows = c.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name=?",
                (table,),
            ).fetchall()
            columns = {str(dict(row).get("column_name") or "") for row in rows}
        except Exception:
            columns = set(candidates)
        for column in candidates:
            if columns and column not in columns:
                continue
            try:
                row = c.execute(f"SELECT COUNT(DISTINCT {column}) count FROM {table}").fetchone()
                return int(dict(row).get("count") or 0)
            except Exception:
                return 0
        return 0

    with data_conn() as c:
        try:
            trade_dates = int(dict(c.execute("SELECT COUNT(*) count FROM dc_available_trade_dates").fetchone()).get("count") or 0)
        except Exception:
            trade_dates = 0
        if trade_dates <= 0:
            row = c.execute("SELECT COUNT(*) count FROM (SELECT 1 FROM dc_stock_daily_snapshot GROUP BY trade_date LIMIT 5000)").fetchone()
            trade_dates = int(dict(row).get("count") or 0)
        themes = count_distinct(c, "dc_theme_snapshot", ["snapshot_date", "as_of_date", "available_time"])
        try:
            announcement_date_rows = c.execute(
                """SELECT DISTINCT REPLACE(SUBSTRING(r.available_time,1,10),'-','') event_date
                   FROM dc_announcement_event e
                   JOIN dc_announcement_raw r ON r.id=e.announcement_id
                   WHERE r.ts_code<>'' AND r.available_time<>''"""
            ).fetchall()
        except Exception:
            announcement_date_rows = []
        announcement_dates = {
            str(dict(row).get("event_date") or "")
            for row in announcement_date_rows
            if dict(row).get("event_date")
        }
        announcement_events = len(announcement_dates)
        try:
            coverage_news_rows = c.execute(
                """SELECT payload_json,available_time
                   FROM dc_tushare_raw_records
                   WHERE api_name='major_news'"""
            ).fetchall()
        except Exception:
            coverage_news_rows = []
        news_dates: set[str] = set()
        if coverage_news_rows:
            from app.services.news_theme_cluster import _match_companies
            for row in coverage_news_rows:
                item = dict(row)
                payload = _payload_dict(item.get("payload_json"))
                text = f"{payload.get('title') or ''} {payload.get('content') or ''}".strip()
                if not text or not _match_companies(text):
                    continue
                event_time = str(payload.get("pub_time") or item.get("available_time") or "")
                event_date = event_time[:10].replace("-", "")
                if event_date:
                    news_dates.add(event_date)
        news_events = len(news_dates)
        events = len(announcement_dates | news_dates)
        try:
            capital = int(dict(c.execute(
                """SELECT COUNT(*) count
                   FROM dc_available_trade_dates dates
                   WHERE EXISTS (
                       SELECT 1
                       FROM dc_tushare_raw_records raw
                       WHERE raw.api_name='moneyflow'
                         AND raw.trade_date=dates.trade_date
                         AND raw.available_time<>''
                   )"""
            ).fetchone()).get("count") or 0)
        except Exception:
            capital = 0
    return {
        "trade_dates": trade_dates,
        "themes": themes,
        "events": events,
        "announcement_events": announcement_events,
        "major_news_events": news_events,
        "capital": max(0, capital - CAPITAL_FLOW_HISTORY_SESSIONS - 10),
        "capital_raw": capital,
    }


def _coverage_snapshot(model_key: str, base: dict[str, int] | None = None) -> dict[str, Any]:
    base = base or _base_coverage_snapshot()
    trade_dates = int(base.get("trade_dates") or 0)
    themes = int(base.get("themes") or 0)
    events = int(base.get("events") or 0)
    capital = int(base.get("capital") or 0)
    contract = PLAN_MODELS[model_key]
    sessions = themes if "narrative" in model_key else trade_dates
    if model_key == "event_surprise_lgbm_v1":
        sessions = min(trade_dates, events)
    if model_key == "capital_persistence_multitask_v1":
        sessions = min(trade_dates, capital)
    formal_required = _required_training_sessions()
    domain_minimum = int(contract["minimum_sessions"])
    formal_history_ready = sessions >= formal_required
    return {
        "trade_sessions": trade_dates,
        "theme_snapshot_sessions": themes,
        "event_point_in_time_sessions": events,
        "event_rows": events,
        "announcement_event_sessions": int(base.get("announcement_events") or 0),
        "major_news_event_sessions": int(base.get("major_news_events") or 0),
        "capital_point_in_time_sessions": capital,
        "capital_coverage_slices": capital,
        "capital_raw_sessions": int(base.get("capital_raw") or capital),
        "effective_sessions": sessions,
        "minimum_sessions": domain_minimum,
        "formal_walk_forward_sessions": formal_required,
        "domain_minimum_ready": sessions >= domain_minimum,
        "formal_history_ready": formal_history_ready,
        "data_ready": sessions >= domain_minimum and formal_history_ready,
    }


def _latest_run_for_model(model_key: str, runs: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    for run in (runs if runs is not None else list_model_train_runs(limit=100)):
        metrics = run.get("metrics") or {}
        if str(metrics.get("model_key") or "") == model_key:
            return run
    return None


def classify_plan_model_training_failure(error: Any) -> dict[str, Any]:
    raw = str(error or "").strip()
    message = raw.lower()
    if any(value in message for value in ("statement timeout", "query timeout", "canceling statement due to statement timeout")):
        category = "transient_query_timeout"
        retryable = True
        action = "bounded_retry"
    elif any(value in message for value in (
        "capital_moneyflow_query_failed",
        "moneyflow_query_failed",
        "training_symbol_anchor_query_failed",
        "theme_mapping_query_failed",
        "训练样本不足：0 rows",
        "训练样本不足: 0 rows",
    )):
        category = "dataset_construction_failure"
        retryable = True
        action = "bounded_retry"
    elif any(value in message for value in (
        "训练数据不足",
        "训练日历不足",
        "可标注训练交易日不足",
        "data coverage",
        "coverage gap",
    )):
        category = "data_coverage_gap"
        retryable = False
        action = "delegate_data_backfill"
    elif any(value in message for value in (
        "nameerror",
        "not defined",
        "unboundlocalerror",
        "attributeerror",
        "typeerror",
        "syntaxerror",
    )):
        category = "code_contract_error"
        retryable = False
        action = "escalate_owner_review"
    elif "stale_plan_model_training_run" in message or "训练心跳超时" in message:
        category = "interrupted_runtime"
        retryable = True
        action = "bounded_retry"
    elif any(value in message for value in ("memoryerror", "out of memory", "cannot allocate memory")):
        category = "resource_exhaustion"
        retryable = True
        action = "bounded_retry"
    else:
        category = "unknown_training_failure"
        retryable = False
        action = "escalate_owner_review"
    return {
        "contract": TRAINING_FAILURE_RECOVERY_CONTRACT,
        "category": category,
        "retryable": retryable,
        "recommended_action": action,
        "error_excerpt": raw[:500],
    }


def _update_training_failure_recovery(
    run_id: int,
    patch: dict[str, Any],
    *,
    timeline_message: str | None = None,
) -> dict[str, Any]:
    run = get_model_train_run(int(run_id)) or {}
    metrics = dict(run.get("metrics") or {})
    recovery = dict(metrics.get("failure_recovery") or {})
    recovery.update(patch)
    recovery.setdefault("contract", TRAINING_FAILURE_RECOVERY_CONTRACT)
    metrics["failure_recovery"] = recovery
    if timeline_message:
        timeline = list(metrics.get("timeline") or [])
        timeline.append({
            "at": datetime.now().astimezone().isoformat(),
            "phase": "failure_recovery",
            "progress": 1.0,
            "message": str(timeline_message),
        })
        metrics["timeline"] = timeline[-20:]
    update_model_train_run(int(run_id), metrics=metrics)
    _operations_cache.update({"at": 0.0, "value": None})
    _status_cache.update({"at": 0.0, "value": None})
    return recovery


def _candidate_failure_takeover(model_key: str) -> dict[str, Any] | None:
    candidate = _plan_model_candidate_governance_blocker(model_key)
    if not candidate:
        return None
    validation = candidate.get("validation") if isinstance(
        candidate.get("validation"), dict
    ) else {}
    shadow = validation.get("model_shadow") if isinstance(
        validation.get("model_shadow"), dict
    ) else {}
    return {
        "model_version_id": int(candidate.get("id") or 0),
        "version": str(candidate.get("version") or ""),
        "validation_status": str(validation.get("status") or ""),
        "shadow_status": str(shadow.get("status") or ""),
        "observed_return_sessions": int(shadow.get("observed_return_sessions") or 0),
        "required_return_sessions": int(shadow.get("required_return_sessions") or 20),
    }


def _enqueue_plan_model_training_recovery(
    *,
    model_key: str,
    failed_run_id: int,
    root_run_id: int,
    retry_count: int,
    classification: dict[str, Any],
) -> dict[str, Any]:
    try:
        from app.services.evolution_orchestrator import create_evolution_task

        recovery = {
            "contract": TRAINING_FAILURE_RECOVERY_CONTRACT,
            "model_key": model_key,
            "failed_run_id": int(failed_run_id),
            "root_run_id": int(root_run_id),
            "retry_count": max(0, int(retry_count)),
            "max_retries": TRAINING_FAILURE_MAX_RETRIES,
            "classification": dict(classification),
            "queued_at": datetime.now().astimezone().isoformat(),
        }
        task_kwargs = {
            "task_type": "plan_model_autonomy",
            "payload": {
                "trigger": "automatic_training_failure_recovery",
                "training_failure_recovery": recovery,
            },
            "dedupe_key": (
                f"plan_model_training_recovery:v1:{int(root_run_id)}:"
                f"{int(failed_run_id)}:{max(0, int(retry_count))}"
            ),
            "max_attempts": 3,
            "priority": 32,
        }
        task = create_evolution_task(**task_kwargs)
        if str(task.get("status") or "") in {"completed", "failed", "cancelled"}:
            recovery["previous_recovery_task_id"] = int(task.get("id") or 0) or None
            recovery["requeued_after_terminal_coordinator"] = True
            task_kwargs["payload"] = {
                "trigger": "automatic_training_failure_recovery",
                "training_failure_recovery": recovery,
            }
            task = create_evolution_task(**task_kwargs, force=True)
        status = str(task.get("status") or "queued")
        _update_training_failure_recovery(
            int(failed_run_id),
            {
                **recovery,
                "status": "queued_for_automatic_recovery",
                "recovery_task_id": int(task.get("id") or 0),
                "recovery_task_status": status,
            },
            timeline_message="训练故障已交给模型自治任务自动处理",
        )
        return {
            "ok": True,
            "status": status,
            "task_id": int(task.get("id") or 0),
            "recovery": recovery,
        }
    except Exception as exc:
        _update_training_failure_recovery(
            int(failed_run_id),
            {
                "status": "recovery_enqueue_failed",
                "enqueue_error": str(exc)[:300],
                "updated_at": datetime.now().astimezone().isoformat(),
            },
            timeline_message="训练故障自动恢复任务创建失败，保留异常等待下一自治周期重建",
        )
        return {"ok": False, "status": "enqueue_failed", "error": str(exc)[:300]}


def process_plan_model_training_failure(
    recovery_request: dict[str, Any],
) -> dict[str, Any]:
    request = dict(recovery_request or {})
    failed_run_id = int(request.get("failed_run_id") or 0)
    run = get_model_train_run(failed_run_id) if failed_run_id > 0 else None
    if not run:
        return {"ok": False, "status": "failed_run_missing", "failed_run_id": failed_run_id}
    metrics = dict(run.get("metrics") or {})
    model_key = str(request.get("model_key") or metrics.get("model_key") or "")
    if model_key not in PLAN_MODELS:
        return {"ok": False, "status": "unknown_plan_model", "model_key": model_key}
    root_run_id = int(request.get("root_run_id") or failed_run_id)
    retry_count = max(0, int(request.get("retry_count") or 0))
    max_retries = max(0, int(request.get("max_retries") or TRAINING_FAILURE_MAX_RETRIES))
    classification = request.get("classification") if isinstance(
        request.get("classification"), dict
    ) else classify_plan_model_training_failure(run.get("error"))
    takeover = _candidate_failure_takeover(model_key)
    if takeover:
        recovery = _update_training_failure_recovery(
            failed_run_id,
            {
                "contract": TRAINING_FAILURE_RECOVERY_CONTRACT,
                "status": "resolved_by_existing_candidate_observation",
                "classification": classification,
                "root_run_id": root_run_id,
                "retry_count": retry_count,
                "max_retries": max_retries,
                "candidate_takeover": takeover,
                "resolved_at": datetime.now().astimezone().isoformat(),
            },
            timeline_message=(
                f"已有候选版本 #{takeover['model_version_id']} 进入未来观察，"
                "本次训练故障不再重训"
            ),
        )
        return {
            "ok": True,
            "status": recovery["status"],
            "model_key": model_key,
            "failed_run_id": failed_run_id,
            "candidate_takeover": takeover,
            "started": [],
        }
    category = str(classification.get("category") or "unknown_training_failure")
    if category == "data_coverage_gap":
        try:
            from app.data_platform.services.backfill import ensure_plan_model_data_backfill

            work_order = ensure_plan_model_data_backfill()
            status = "delegated_to_data_backfill"
        except Exception as exc:
            work_order = {"ok": False, "error": str(exc)[:300]}
            status = "data_backfill_delegation_failed"
        _update_training_failure_recovery(
            failed_run_id,
            {
                "contract": TRAINING_FAILURE_RECOVERY_CONTRACT,
                "status": status,
                "classification": classification,
                "root_run_id": root_run_id,
                "retry_count": retry_count,
                "max_retries": max_retries,
                "data_backfill": work_order,
                "updated_at": datetime.now().astimezone().isoformat(),
            },
            timeline_message="训练覆盖缺口已委托数据补全工单，数据就绪后再训练",
        )
        return {
            "ok": bool(work_order.get("ok", True)),
            "status": status,
            "model_key": model_key,
            "failed_run_id": failed_run_id,
            "data_backfill": work_order,
            "started": [],
        }
    retryable = bool(classification.get("retryable"))
    if retryable and retry_count < max_retries:
        next_retry_count = retry_count + 1
        result = request_plan_model_training(
            model_key,
            training_context={
                "trigger": "automatic_training_failure_recovery",
                "trigger_reason": f"训练故障自动恢复：{category}",
                "requested_by": "plan_model_failure_coordinator",
                "failure_recovery": {
                    "contract": TRAINING_FAILURE_RECOVERY_CONTRACT,
                    "root_run_id": root_run_id,
                    "previous_run_id": failed_run_id,
                    "retry_count": next_retry_count,
                    "max_retries": max_retries,
                    "classification": classification,
                },
            },
        )
        if result.get("started"):
            status = "automatic_retry_started"
            timeline_message = f"训练故障已启动第 {next_retry_count}/{max_retries} 次自动重试"
        elif result.get("reason") == "candidate_governance_in_progress":
            status = "resolved_by_existing_candidate_observation"
            timeline_message = "候选治理已在进行，自动重试已抑制"
        else:
            status = "automatic_retry_not_started"
            timeline_message = "训练故障自动重试未启动，等待下一自治周期复核"
        _update_training_failure_recovery(
            failed_run_id,
            {
                "contract": TRAINING_FAILURE_RECOVERY_CONTRACT,
                "status": status,
                "classification": classification,
                "root_run_id": root_run_id,
                "retry_count": retry_count,
                "max_retries": max_retries,
                "retry_run_id": int(result.get("run_id") or 0) or None,
                "retry_result": result,
                "updated_at": datetime.now().astimezone().isoformat(),
            },
            timeline_message=timeline_message,
        )
        return {
            "ok": True,
            "status": status,
            "model_key": model_key,
            "failed_run_id": failed_run_id,
            "started": [result] if result.get("started") else [],
            "result": result,
        }
    status = (
        "automatic_retry_exhausted"
        if retryable
        else "escalated_owner_review"
    )
    _update_training_failure_recovery(
        failed_run_id,
        {
            "contract": TRAINING_FAILURE_RECOVERY_CONTRACT,
            "status": status,
            "classification": classification,
            "root_run_id": root_run_id,
            "retry_count": retry_count,
            "max_retries": max_retries,
            "owner_intervention_required": True,
            "resolved_at": datetime.now().astimezone().isoformat(),
        },
        timeline_message=(
            "训练故障自动重试已耗尽，已升级为所有者异常"
            if retryable
            else "训练故障属于代码或未知合同异常，已停止重复重试并升级"
        ),
    )
    return {
        "ok": False,
        "status": status,
        "model_key": model_key,
        "failed_run_id": failed_run_id,
        "owner_intervention_required": True,
        "started": [],
    }


def reconcile_unhandled_plan_model_training_failures() -> dict[str, Any]:
    latest_by_model: dict[str, dict[str, Any]] = {}
    for run in list_model_train_runs(limit=200):
        metrics = run.get("metrics") or {}
        model_key = str(metrics.get("model_key") or "")
        if model_key in PLAN_MODELS and model_key not in latest_by_model:
            latest_by_model[model_key] = run
    queued: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    terminal_statuses = {
        "resolved_by_existing_candidate_observation",
        "delegated_to_data_backfill",
        "automatic_retry_started",
        "automatic_retry_exhausted",
        "escalated_owner_review",
    }
    for model_key, run in latest_by_model.items():
        if str(run.get("status") or "") != "failed":
            continue
        metrics = dict(run.get("metrics") or {})
        existing = metrics.get("failure_recovery") if isinstance(
            metrics.get("failure_recovery"), dict
        ) else {}
        if str(existing.get("status") or "") in terminal_statuses or str(
            existing.get("status") or ""
        ) == "queued_for_automatic_recovery":
            continue
        takeover = _candidate_failure_takeover(model_key)
        if takeover:
            _update_training_failure_recovery(
                int(run["id"]),
                {
                    "contract": TRAINING_FAILURE_RECOVERY_CONTRACT,
                    "status": "resolved_by_existing_candidate_observation",
                    "classification": classify_plan_model_training_failure(run.get("error")),
                    "root_run_id": int(run["id"]),
                    "retry_count": 0,
                    "max_retries": TRAINING_FAILURE_MAX_RETRIES,
                    "candidate_takeover": takeover,
                    "resolved_at": datetime.now().astimezone().isoformat(),
                },
                timeline_message=(
                    f"已有候选版本 #{takeover['model_version_id']} 自治观察中，"
                    "历史训练故障已关闭"
                ),
            )
            resolved.append({
                "model_key": model_key,
                "failed_run_id": int(run["id"]),
                "candidate_takeover": takeover,
            })
            continue
        context = metrics.get("targeted_training_context") if isinstance(
            metrics.get("targeted_training_context"), dict
        ) else {}
        prior_recovery = context.get("failure_recovery") if isinstance(
            context.get("failure_recovery"), dict
        ) else {}
        root_run_id = int(prior_recovery.get("root_run_id") or run["id"])
        retry_count = int(prior_recovery.get("retry_count") or 0)
        classification = classify_plan_model_training_failure(run.get("error"))
        queued.append(_enqueue_plan_model_training_recovery(
            model_key=model_key,
            failed_run_id=int(run["id"]),
            root_run_id=root_run_id,
            retry_count=retry_count,
            classification=classification,
        ))
    return {
        "ok": True,
        "schema_version": TRAINING_FAILURE_RECOVERY_CONTRACT,
        "queued": queued,
        "resolved": resolved,
        "managed_model_keys": sorted({
            str((item.get("recovery") or {}).get("model_key") or "")
            for item in queued
            if str((item.get("recovery") or {}).get("model_key") or "")
        }),
    }


def _metrics_satisfy_current_training_protocol(metrics: dict[str, Any]) -> bool:
    dataset = metrics.get("dataset") or {}
    labeled_sessions = int(
        metrics.get("labeled_trade_date_count")
        or dataset.get("trade_date_count")
        or 0
    )
    return bool(
        str(metrics.get("model_evaluation_contract") or "") == MODEL_EVALUATION_CONTRACT
        and isinstance(metrics.get("task_native_oos"), dict)
        and int((metrics.get("task_native_oos") or {}).get("window_count") or 0)
        >= int(TRAINING_PROTOCOL["walk_forward_windows"])
        and str(metrics.get("evaluation_stage") or "") == "formal_five_window"
        and labeled_sessions >= _required_training_sessions()
        and int(metrics.get("training_trade_date_count") or dataset.get("train_trade_date_count") or 0)
        >= int(TRAINING_PROTOCOL["minimum_train_sessions"])
        and int(metrics.get("validation_trade_date_count") or dataset.get("validation_trade_date_count") or 0)
        >= int(TRAINING_PROTOCOL["validation_sessions"])
        and int(metrics.get("test_trade_date_count") or dataset.get("test_trade_date_count") or 0)
        >= int(TRAINING_PROTOCOL["test_sessions"])
    )


def _with_train_timeline(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if not run:
        return None
    metrics = dict(run.get("metrics") or {})
    if not isinstance(metrics.get("timeline"), list) or not metrics.get("timeline"):
        metrics["timeline"] = [{
            "at": run.get("updated_at") or run.get("created_at"),
            "phase": run.get("phase") or run.get("status") or "queued",
            "progress": run.get("progress") or 0,
            "message": run.get("message") or "训练任务已创建，等待阶段更新",
        }]
    dataset = metrics.get("dataset") or {}
    labeled_sessions = int(
        metrics.get("labeled_trade_date_count")
        or dataset.get("trade_date_count")
        or 0
    )
    current_protocol = _metrics_satisfy_current_training_protocol(metrics)
    timeline = metrics.get("timeline") or []
    current_protocol_attempt_failed = bool(
        str(run.get("status") or "") == "failed"
        and any(
            str(item.get("phase") or "") == "dataset"
            and "日历覆盖" in str(item.get("message") or "")
            for item in timeline
            if isinstance(item, dict)
        )
    )
    return {
        **run,
        "metrics": metrics,
        "training_protocol_status": (
            "current" if current_protocol
            else "current_failed" if current_protocol_attempt_failed
            else "legacy_invalid"
        ),
        "labeled_trade_date_count": labeled_sessions or None,
    }


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _mark_stale_train_run(run: dict[str, Any] | None, *, stale_minutes: int = 20) -> dict[str, Any] | None:
    if not run or str(run.get("status") or "") not in {"running", "queued"}:
        return run
    updated_at = _parse_dt(run.get("updated_at") or run.get("created_at"))
    if not updated_at or datetime.now() - updated_at < timedelta(minutes=max(5, stale_minutes)):
        return run
    metrics = dict(run.get("metrics") or {})
    timeline = list(metrics.get("timeline") or [])
    timeline.append({
        "at": datetime.now().isoformat(),
        "phase": "failed",
        "progress": 1.0,
        "message": "训练心跳超时：后端进程重启或训练线程中断，已标记为可重训",
    })
    metrics["timeline"] = timeline[-20:]
    update_model_train_run(
        int(run["id"]),
        status="failed",
        phase="failed",
        progress=1.0,
        message="训练心跳超时，已标记为可重训",
        metrics=metrics,
        error="stale_plan_model_training_run",
        finished_at=datetime.now().isoformat(),
    )
    model_key = str(metrics.get("model_key") or "")
    context = metrics.get("targeted_training_context") if isinstance(
        metrics.get("targeted_training_context"), dict
    ) else {}
    prior_recovery = context.get("failure_recovery") if isinstance(
        context.get("failure_recovery"), dict
    ) else {}
    if model_key in PLAN_MODELS:
        _enqueue_plan_model_training_recovery(
            model_key=model_key,
            failed_run_id=int(run["id"]),
            root_run_id=int(prior_recovery.get("root_run_id") or run["id"]),
            retry_count=int(prior_recovery.get("retry_count") or 0),
            classification=classify_plan_model_training_failure(
                "stale_plan_model_training_run"
            ),
        )
    refreshed = dict(run)
    refreshed.update({
        "status": "failed",
        "phase": "failed",
        "progress": 1.0,
        "message": "训练心跳超时，已标记为可重训",
        "metrics": metrics,
        "error": "stale_plan_model_training_run",
        "finished_at": datetime.now().isoformat(),
    })
    return refreshed


def reconcile_stale_plan_model_runs(*, stale_minutes: int = 20) -> dict[str, Any]:
    """Close every orphaned run, not only the latest run of each model family."""
    reconciled: list[int] = []
    for run in list_model_train_runs(limit=1000):
        if str(run.get("status") or "") not in {"running", "queued"}:
            continue
        refreshed = _mark_stale_train_run(run, stale_minutes=stale_minutes)
        if refreshed and str(refreshed.get("error") or "") == "stale_plan_model_training_run":
            reconciled.append(int(refreshed["id"]))
    return {
        "ok": True,
        "schema_version": "plan_model_stale_run_reconciliation_v1",
        "reconciled_run_ids": reconciled,
        "count": len(reconciled),
    }


def _environment_coverage_summary(metrics: dict[str, Any]) -> dict[str, Any]:
    recorded = metrics.get("environment_coverage")
    if isinstance(recorded, dict) and recorded:
        return recorded
    state_observations = metrics.get("market_state_observations") or {}
    profile_observations = metrics.get("market_profile_observations") or {}
    covered_states = sum(
        1 for state in ("S0", "S1", "S2", "S3")
        if int(state_observations.get(state) or 0) > 0
    )
    covered_profiles = sum(
        1 for index in range(1, 13)
        if int(profile_observations.get(f"P{index:02d}") or 0) > 0
    )
    return {
        "parent_state_count": 4,
        "covered_parent_states": covered_states,
        "profile_count": 12,
        "minimum_required_profiles": 8,
        "covered_profiles": covered_profiles,
        "parent_states_complete": covered_states == 4,
        "profiles_complete": covered_profiles >= 8,
        "complete": covered_states == 4 and covered_profiles >= 8,
        "contract": (
            "point_in_time_four_parent_states_and_at_least_eight_"
            "of_twelve_profiles_coverage_v1"
        ),
    }


def _current_production_gate_audit(
    version: dict[str, Any] | None,
    artifact_summary: dict[str, Any],
) -> dict[str, Any]:
    if not version:
        return {"status": "not_applicable", "passed": False, "blocked_reasons": ["no_active_version"]}
    metrics = version.get("metrics") or {}
    validation = version.get("validation") or {}
    environment_coverage = _environment_coverage_summary(metrics)
    task_native_oos = metrics.get("task_native_oos") or {}
    task_baseline = metrics.get("task_baseline") or {}
    strategy_impact = metrics.get("strategy_impact_ab") or {}
    eligibility = validation.get("production_eligibility") or {}
    recorded_gates = eligibility.get("gates") or {}
    gates = {
        "artifact_ready": bool(
            artifact_summary.get("artifact_ready")
            and artifact_summary.get("model_path_exists")
        ),
        "data_ready": bool(recorded_gates.get("data_ready")),
        "current_task_native_evaluation_contract": (
            str(metrics.get("model_evaluation_contract") or "")
            == MODEL_EVALUATION_CONTRACT
        ),
        "task_metric_passed": bool(metrics.get("task_metric_passed")),
        "task_native_walk_forward_oos": bool(task_native_oos.get("passed")),
        "task_native_oos_baseline_outperformed": bool(
            task_native_oos.get("baseline_outperformed")
        ),
        "task_baseline_comparison_executed": bool(
            task_baseline.get("comparison_executed")
        ),
        "task_native_baseline_outperformed": bool(
            task_baseline.get("outperformed")
        ),
        "environment_coverage_complete": bool(environment_coverage.get("complete")),
        "walk_forward_windows": int(
            metrics.get("walk_forward_windows") or metrics.get("oos_windows") or 0
        ) >= 5,
        "training_protocol": bool(recorded_gates.get("training_protocol")),
        "controlled_strategy_ab": controlled_strategy_ab_passed_current_contract(
            str(version.get("model_key") or ""),
            strategy_impact,
        ),
        "live_observation_20_sessions": bool(
            recorded_gates.get("live_observation_20_sessions")
        ),
        "human_review": bool(recorded_gates.get("human_review")),
        "recorded_production_eligible": bool(validation.get("production_eligible")),
        "recorded_eligibility_consistent": bool(eligibility.get("eligible")),
    }
    blocked = [key for key, passed in gates.items() if not passed]
    return {
        "schema_version": "plan_model_current_gate_audit_v3",
        "status": "current_gate_passed" if not blocked else "legacy_production_current_gate_not_passed",
        "passed": not blocked,
        "gates": gates,
        "blocked_reasons": blocked,
        "non_destructive": True,
    }


def audit_plan_model_production_eligibility(
    version: dict[str, Any] | None,
) -> dict[str, Any]:
    """Recompute production authority from current evidence, never from active state."""

    return _current_production_gate_audit(version, _artifact_summary(version))


def active_production_plan_model_bindings() -> dict[str, dict[str, Any] | None]:
    """Return only active versions that still pass every current production gate."""

    bindings: dict[str, dict[str, Any] | None] = {}
    for model_key in PLAN_MODELS:
        version = get_active_model_version(model_key)
        audit = audit_plan_model_production_eligibility(version)
        bindings[model_key] = (
            {
                "id": int(version["id"]),
                "model_key": model_key,
                "version": str(version.get("version") or ""),
                "status": str(version.get("status") or ""),
                "production_eligible": True,
            }
            if version and audit.get("passed")
            else None
        )
    return bindings


def reconcile_plan_model_production_gates() -> dict[str, Any]:
    """Deactivate active plan models whose current release evidence is incomplete."""

    items: list[dict[str, Any]] = []
    for model_key in PLAN_MODELS:
        version = get_active_model_version(model_key)
        if not version:
            items.append(
                {
                    "model_key": model_key,
                    "action": "no_active_version",
                    "active": False,
                }
            )
            continue
        audit = audit_plan_model_production_eligibility(version)
        if audit.get("passed"):
            items.append(
                {
                    "model_key": model_key,
                    "model_version_id": int(version["id"]),
                    "version": str(version.get("version") or ""),
                    "action": "kept_active",
                    "active": True,
                    "audit": audit,
                }
            )
            continue
        metrics = version.get("metrics") or {}
        legacy = (
            str(metrics.get("model_evaluation_contract") or "")
            != MODEL_EVALUATION_CONTRACT
        )
        validation = dict(version.get("validation") or {})
        eligibility = dict(validation.get("production_eligibility") or {})
        eligibility["eligible"] = False
        eligibility["blocked_reasons"] = sorted(
            set(
                [
                    *list(eligibility.get("blocked_reasons") or []),
                    *list(audit.get("blocked_reasons") or []),
                ]
            )
        )
        validation["production_eligible"] = False
        validation["production_eligibility"] = eligibility
        validation["production_gate_reconciliation"] = {
            "schema_version": "plan_model_production_gate_reconciliation_v1",
            "reconciled_at": datetime.now().isoformat(timespec="seconds"),
            "action": "retired_legacy" if legacy else "blocked_production_gate",
            "audit": audit,
        }
        update_model_version_validation(int(version["id"]), validation=validation)
        status = "retired_legacy" if legacy else "blocked_production_gate"
        update_model_version_status(
            int(version["id"]),
            status=status,
            is_active=False,
        )
        items.append(
            {
                "model_key": model_key,
                "model_version_id": int(version["id"]),
                "version": str(version.get("version") or ""),
                "action": status,
                "active": False,
                "audit": audit,
            }
        )
    _status_cache.update({"at": 0.0, "value": None})
    _operations_cache.update({"at": 0.0, "value": None})
    return {
        "ok": True,
        "schema_version": "plan_model_production_gate_reconciliation_v1",
        "deactivated_count": sum(
            item.get("action") in {"retired_legacy", "blocked_production_gate"}
            for item in items
        ),
        "active_count": sum(bool(item.get("active")) for item in items),
        "items": items,
    }


def _model_live_health_snapshot() -> dict[str, dict[str, Any]]:
    try:
        with business_conn() as c:
            rows = c.execute(
                """SELECT model_key,active_version,status,live_ic,hit_rate,turnover,
                          drawdown_contribution,decay,weight_multiplier,reason,metrics_json,updated_at
                     FROM model_live_health"""
            ).fetchall()
    except Exception:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        metrics = row.pop("metrics_json", None)
        if isinstance(metrics, str):
            try:
                metrics = json.loads(metrics)
            except Exception:
                metrics = {}
        evidence_maturity = (metrics or {}).get("evidence_maturity") or {}
        result[str(row.get("model_key") or "")] = {
            **row,
            "metrics": metrics or {},
            "evidence_maturity": evidence_maturity,
        }
    return result


def _latest_market_state_code() -> str:
    try:
        from app.data_platform.db import _conn as data_conn
        with data_conn() as c:
            row = c.execute(
                """SELECT market_state FROM dc_market_context_snapshot
                   ORDER BY trade_date DESC LIMIT 1"""
            ).fetchone()
        return str(dict(row).get("market_state") or "S1") if row else "S1"
    except Exception:
        return "S1"


def plan_model_operations_status(*, cache_seconds: int = 3) -> dict[str, Any]:
    """Lightweight polling contract: no coverage scans or artifact loading."""
    now_monotonic = time.monotonic()
    cached = _operations_cache.get("value")
    if isinstance(cached, dict) and now_monotonic - float(_operations_cache.get("at") or 0) < max(1, cache_seconds):
        return cached
    runs = list_model_train_runs(limit=200)
    health = _model_live_health_snapshot()
    latest_by_model = {
        key: _with_train_timeline(_latest_run_for_model(key, runs))
        for key in PLAN_MODELS
    }
    active_by_model = {key: get_active_model_version(key) for key in PLAN_MODELS}
    governance_by_model = {
        key: _select_plan_model_governance_version(
            list_model_versions(key, limit=10)
        )
        for key in PLAN_MODELS
    }
    try:
        from app.services.model_signal_fusion import (
            ENVIRONMENT_MODEL_MULTIPLIERS,
            MODEL_WEIGHTS,
        )
    except Exception:
        MODEL_WEIGHTS = {key: 0.0 for key in PLAN_MODELS}
        ENVIRONMENT_MODEL_MULTIPLIERS = {}
    state_code = _latest_market_state_code()
    environment_weights = ENVIRONMENT_MODEL_MULTIPLIERS.get(state_code, {})
    next_check_at = None
    items: list[dict[str, Any]] = []
    for key, contract in PLAN_MODELS.items():
        active = active_by_model.get(key)
        live = health.get(key) or {}
        if active and str(live.get("active_version") or "") != str(active.get("version") or ""):
            live = {}
        maturity = live.get("evidence_maturity") or {}
        mature_outcomes = int(maturity.get("mature_20d_outcomes") or 0)
        minimum_required = int(maturity.get("minimum_required") or 5)
        raw_health_status = str(live.get("status") or "not_started")
        observation_status = (
            "not_applicable"
            if not active
            else raw_health_status
            if raw_health_status in {"degraded", "watch"}
            else "observing"
            if mature_outcomes < minimum_required
            else raw_health_status
        )
        production_observation = {
            "scope": "production_runtime",
            "status": observation_status,
            "production_active": bool(active),
            "active_version": (active or {}).get("version"),
            "mature_20d_outcomes": mature_outcomes,
            "minimum_required": minimum_required,
            "updated_at": live.get("updated_at") if active else None,
        }
        governance_version = governance_by_model.get(key) or {}
        governance_validation = governance_version.get("validation") if isinstance(
            governance_version.get("validation"), dict
        ) else {}
        shadow = governance_validation.get("model_shadow") if isinstance(
            governance_validation.get("model_shadow"), dict
        ) else {}
        latest_shadow_observation = (
            list(shadow.get("observations") or [])[-1]
            if shadow.get("observations")
            else {}
        )
        candidate_applicable = bool(
            governance_version
            and str(governance_version.get("status") or "") == "candidate"
            and shadow
        )
        candidate_observation = {
            "scope": "candidate_future_shadow",
            "status": (
                str(shadow.get("status") or "pending_enrollment")
                if candidate_applicable
                else "not_applicable"
            ),
            "candidate_active": candidate_applicable,
            "model_version_id": (
                int(governance_version.get("id") or 0) or None
                if candidate_applicable
                else None
            ),
            "version": (
                str(governance_version.get("version") or "") or None
                if candidate_applicable
                else None
            ),
            "signal_start_trade_date": shadow.get("signal_start_trade_date"),
            "observed_return_sessions": int(
                shadow.get("observed_return_sessions") or 0
            ),
            "required_return_sessions": int(
                shadow.get("required_return_sessions") or 20
            ),
            "minimum_effect_days": int(shadow.get("minimum_effect_days") or 4),
            "candidate_model_effect_days": int(
                latest_shadow_observation.get("candidate_model_effect_days") or 0
            ),
            "last_observed_trade_date": shadow.get("last_observed_trade_date"),
            "autonomous_management": bool(candidate_applicable),
            "manager": "model_shadow_observation" if candidate_applicable else None,
            "production_change_forbidden": bool(
                shadow.get("production_change_forbidden", True)
            ) if candidate_applicable else True,
        }
        health_multiplier = float(live.get("weight_multiplier") or 1.0) if active else 0.0
        maturity_multiplier = 0.85 if observation_status in {"observing", "watch"} else 1.0
        environment_multiplier = float(environment_weights.get(key, 1.0))
        base_weight = float(MODEL_WEIGHTS.get(key, 0.0)) if active else 0.0
        raw_contribution = base_weight * environment_multiplier * health_multiplier * maturity_multiplier
        run = latest_by_model.get(key)
        latest_run_metrics = (run or {}).get("metrics") or {}
        failure_recovery = latest_run_metrics.get("failure_recovery") if isinstance(
            latest_run_metrics.get("failure_recovery"), dict
        ) else {}
        active_metrics = (active or {}).get("metrics") or {}
        evaluation_metrics = latest_run_metrics or active_metrics
        context = ((run or {}).get("metrics") or {}).get("targeted_training_context") or {}
        trigger_reason = (
            context.get("trigger_reason")
            or context.get("reason")
            or ("首次训练" if not run else "定期模型治理")
        )
        items.append({
            "model_key": key,
            "name": contract.get("name"),
            "latest_train_run": run,
            "trigger_reason": trigger_reason,
            "requested_by": context.get("requested_by") or ("autonomous_daily" if run else None),
            "next_check_at": next_check_at,
            "candidate_observation": candidate_observation,
            "production_observation": production_observation,
            "observation": production_observation,
            "training_failure_recovery": failure_recovery,
            "evaluation": {
                "status": (
                    "current"
                    if str(
                        evaluation_metrics.get("model_evaluation_contract") or ""
                    )
                    == MODEL_EVALUATION_CONTRACT
                    else "outdated"
                    if evaluation_metrics
                    else "not_evaluated"
                ),
                "recorded_contract": evaluation_metrics.get(
                    "model_evaluation_contract"
                ),
                "current_contract": MODEL_EVALUATION_CONTRACT,
                "task_metric_passed": bool(
                    evaluation_metrics.get("task_metric_passed")
                ),
                "task_native_oos": evaluation_metrics.get(
                    "task_native_oos"
                ),
                "strategy_impact_status": str(
                    (
                        evaluation_metrics.get("strategy_impact_ab")
                        or {}
                    ).get("status")
                    or "not_run"
                ),
                "definition": evaluation_contract(key),
            },
            "weight_breakdown": {
                "base": round(base_weight, 4),
                "environment": round(environment_multiplier, 4),
                "health": round(health_multiplier, 4),
                "maturity": round(maturity_multiplier, 4),
                "raw_contribution": round(raw_contribution, 6),
                "market_state": state_code,
            },
        })
    raw_total = sum(float((item.get("weight_breakdown") or {}).get("raw_contribution") or 0.0) for item in items)
    scale = min(1.0, 0.55 / raw_total) if raw_total > 0 else 0.0
    for item in items:
        breakdown = item["weight_breakdown"]
        raw_contribution = float(breakdown.get("raw_contribution") or 0.0)
        breakdown["normalized_model_weight"] = round(
            raw_contribution / raw_total if raw_total > 0 else 0.0, 6
        )
        breakdown["final_fusion_weight"] = round(raw_contribution * scale, 6)
        breakdown["non_model_base_weight"] = round(max(0.45, 1.0 - raw_total * scale), 6)
    result = {
        "ok": True,
        "schema_version": "plan_model_operations_status_v3",
        "items": items,
        "scheduler": {
            "status": "retired",
            "heartbeat_at": None,
            "next_check_at": next_check_at,
            "interval_seconds": None,
            "replacement": "autonomous_daily_plan_model_governance",
        },
        "evaluation_contracts": supported_model_contracts(),
        "generated_at": datetime.now().isoformat(),
        "cache_seconds": max(1, cache_seconds),
    }
    _operations_cache.update({"at": now_monotonic, "value": result})
    return result


def _select_plan_model_governance_version(
    versions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    newest = versions[0] if versions else None
    shadow_candidate = next(
        (
            version for version in versions
            if str(version.get("status") or "") == "candidate"
            and str(
                (
                    (version.get("validation") or {}).get("model_shadow")
                    or {}
                ).get("status")
                or ""
            )
            in {
                "awaiting_first_future_signal",
                "observing",
                "extended_for_inconclusive_evidence",
                "passed",
            }
        ),
        None,
    )
    ab_passed_candidate = next(
        (
            version for version in versions
            if str(version.get("status") or "") == "candidate"
            and str(
                (
                    (version.get("metrics") or {}).get("strategy_impact_ab")
                    or {}
                ).get("status")
                or ""
            )
            == "passed"
            and str(
                (
                    (version.get("validation") or {}).get("review")
                    or {}
                ).get("status")
                or ""
            )
            != "rejected"
        ),
        None,
    )
    return shadow_candidate or ab_passed_candidate or newest


def _plan_model_candidate_governance_blocker(
    model_key: str,
) -> dict[str, Any] | None:
    for version in list_model_versions(model_key, limit=10):
        if str(version.get("status") or "") != "candidate":
            continue
        validation = version.get("validation") if isinstance(
            version.get("validation"), dict
        ) else {}
        review = validation.get("review") if isinstance(
            validation.get("review"), dict
        ) else {}
        if str(review.get("status") or "") == "rejected":
            continue
        model_shadow = validation.get("model_shadow") if isinstance(
            validation.get("model_shadow"), dict
        ) else {}
        impact = (version.get("metrics") or {}).get("strategy_impact_ab")
        if (
            str(validation.get("status") or "")
            in {"pending_strategy_ab", "pending_live_observation", "shadow_passed"}
            or str(model_shadow.get("status") or "")
            in {
                "awaiting_first_future_signal",
                "observing",
                "extended_for_inconclusive_evidence",
                "passed",
            }
            or (
                isinstance(impact, dict)
                and str(impact.get("status") or "") == "passed"
            )
        ):
            return version
    return None


@_serialize_status_refresh
def model_family_status() -> dict[str, Any]:
    stale_reconciliation = reconcile_stale_plan_model_runs()
    now = time.monotonic()
    cached = _status_cache.get("value")
    if isinstance(cached, dict) and now - float(_status_cache.get("at") or 0) < 30:
        return cached
    items = []
    base_coverage = _base_coverage_snapshot()
    train_runs = list_model_train_runs(limit=100)
    live_health = _model_live_health_snapshot()
    for key, contract in PLAN_MODELS.items():
        versions = list_model_versions(key, limit=10)
        active = get_active_model_version(key)
        newest = versions[0] if versions else None
        latest = _select_plan_model_governance_version(versions)
        if active:
            active_metrics = dict(active.get("metrics") or {})
            active_metrics["environment_coverage"] = _environment_coverage_summary(active_metrics)
            active = {**active, "metrics": active_metrics}
        if latest:
            latest_metrics_payload = dict(latest.get("metrics") or {})
            latest_metrics_payload["environment_coverage"] = _environment_coverage_summary(latest_metrics_payload)
            latest = {**latest, "metrics": latest_metrics_payload}
        coverage = _coverage_snapshot(key, base_coverage)
        latest_metrics = (latest or {}).get("metrics") or {}
        latest_training_sessions = int(
            latest_metrics.get("labeled_trade_date_count")
            or latest_metrics.get("training_trade_date_count")
            or 0
        )
        coverage["available_sessions"] = coverage["effective_sessions"]
        coverage["latest_training_sessions"] = latest_training_sessions or None
        coverage["coverage_basis"] = "current_database_point_in_time_availability"
        run = _with_train_timeline(_mark_stale_train_run(_latest_run_for_model(key, train_runs)))
        if latest and not latest_training_sessions and run:
            run_training_sessions = int(run.get("labeled_trade_date_count") or 0)
            if run_training_sessions:
                latest_training_sessions = run_training_sessions
                coverage["latest_training_sessions"] = run_training_sessions
        trained = bool(latest)
        latest_uses_current_protocol = bool(
            latest and _metrics_satisfy_current_training_protocol(latest_metrics)
        )
        active_artifact = _artifact_summary(active)
        current_gate_audit = _current_production_gate_audit(active, active_artifact)
        production_ready = bool(active and current_gate_audit.get("passed"))
        if active and not production_ready:
            status = "legacy_production"
        elif production_ready:
            status = "production_current_standard"
        elif trained and not latest_uses_current_protocol:
            status = "legacy_invalid_candidate"
        elif trained:
            status = "candidate"
        elif coverage["data_ready"]:
            status = "trainable"
        else:
            status = "proxy"
        latest_validation = (latest or {}).get("validation") or {}
        eligibility = latest_validation.get("production_eligibility") or {}
        blocked_reasons = list(eligibility.get("blocked_reasons") or [])
        domain_minimum_ready = bool(
            coverage.get(
                "domain_minimum_ready",
                int(coverage.get("effective_sessions") or 0)
                >= int(coverage.get("minimum_sessions") or 0),
            )
        )
        formal_history_ready = bool(
            coverage.get("formal_history_ready", coverage.get("data_ready"))
        )
        if not domain_minimum_ready:
            blocked_reasons.append(
                f"domain_coverage_{coverage['effective_sessions']}_lt_{coverage['minimum_sessions']}"
            )
        if not formal_history_ready:
            blocked_reasons.append(
                f"formal_walk_forward_coverage_{coverage['effective_sessions']}_lt_"
                f"{coverage.get('formal_walk_forward_sessions', coverage['minimum_sessions'])}"
            )
        if status == "legacy_invalid_candidate":
            blocked_reasons.append("legacy_truncated_training_dataset_invalid")
        if status == "candidate" and not production_ready and not blocked_reasons:
            blocked_reasons.append("candidate_requires_review_or_live_observation")
        data_actions = []
        if key == "capital_persistence_multitask_v1":
            data_actions.append({"label": "补资金与席位历史", "api": "/api/data-center/backfill/capital-structure", "reason": "margin_detail/hk_hold/top_list/top_inst"})
        if key in {"event_surprise_lgbm_v1", "narrative_transition_multitask_v1"}:
            data_actions.append({"label": "补新闻/公告/主题历史", "api": "/api/data-center/backfill/major-news", "reason": "3年文本与250日主题快照"})
        if key in {"medium_horizon_lambdarank_v1", "leader_lambdarank_v1"}:
            data_actions.append({"label": "刷新市场与指数训练数据", "api": "/api/model-strategy/market-context/materialize-training-history", "reason": "滚动OOS与基准回放"})
        health = live_health.get(key) or {}
        if not active:
            health = {
                "model_key": key,
                "status": "not_applicable",
                "reason": "没有生产激活版本，不进入生产运行观察",
                "weight_multiplier": 0.0,
                "evidence_maturity": {
                    "status": "not_applicable",
                    "mature_20d_outcomes": 0,
                    "minimum_required": 5,
                },
            }
        elif (
            not health
            or str(health.get("active_version") or "") != str(active.get("version") or "")
        ):
            health = {
                "model_key": key,
                "active_version": active.get("version"),
                "status": "observing",
                "reason": "当前生产版本尚未形成足够的未来表现观察",
                "weight_multiplier": 1.0,
                "evidence_maturity": {
                    "status": "insufficient",
                    "mature_20d_outcomes": 0,
                    "minimum_required": 5,
                },
            }
        else:
            maturity = health.get("evidence_maturity") or {}
            mature_outcomes = int(maturity.get("mature_20d_outcomes") or 0)
            minimum_required = int(maturity.get("minimum_required") or 5)
            if str(health.get("status") or "") not in {"degraded", "watch"} and mature_outcomes < minimum_required:
                health = {
                    **health,
                    "status": "observing",
                    "reason": "生产版本未来20日成熟结果不足",
                }
        items.append({
            "model_key": key,
            **contract,
            "training_contract": {
                "family": contract.get("family"),
                "objective": contract.get("objective"),
                "metric": contract.get("metric"),
                "tasks": contract.get("tasks") or [],
                "eval_at": contract.get("eval_at") or [],
                "label_gain": contract.get("label_gain") or [],
                "params": LIGHTGBM_PARAMS if "LightGBM" in str(contract.get("family") or "") else {},
                "promotion_policy": "candidate -> human_review -> production；模拟系统可豁免20-session真实观察",
            },
            "evaluation_contract": evaluation_contract(key),
            "evaluation_contract_status": (
                "current"
                if str(latest_metrics.get("model_evaluation_contract") or "")
                == MODEL_EVALUATION_CONTRACT
                else "outdated"
                if latest_metrics
                else "not_evaluated"
            ),
            "status": status,
            "coverage": coverage,
            "active_version": active,
            "latest_version": latest,
            "newest_recorded_version": newest,
            "latest_training_protocol_status": (
                "current" if latest_uses_current_protocol else "legacy_invalid"
            ) if latest else "not_trained",
            "active_artifact": active_artifact,
            "latest_artifact": _artifact_summary(latest),
            "current_gate_audit": current_gate_audit,
            "live_health": health,
            "latest_train_run": run,
            "versions": versions,
            "blocked_reasons": blocked_reasons,
            "data_actions": data_actions,
        })
    counts = {
        status: sum(1 for item in items if item["status"] == status)
        for status in (
            "production_current_standard",
            "legacy_production",
            "candidate",
            "trainable",
            "proxy",
            "legacy_invalid_candidate",
        )
    }
    result = {
        "ok": True,
        "schema_version": "plan_model_control_v2",
        "items": items,
        "counts": counts,
        "lightgbm_params": LIGHTGBM_PARAMS,
        "evaluation_contracts": supported_model_contracts(),
        "stale_reconciliation": stale_reconciliation,
    }
    _status_cache.update({"at": now, "value": result})
    return result


def _enqueue_continuous_research_for_model_version(
    *, model_key: str, model_version_id: int, version: str,
) -> dict[str, Any]:
    try:
        from app.services.evolution_orchestrator import (
            enqueue_factor_strategy_research_event,
        )

        task = enqueue_factor_strategy_research_event(
            trigger="plan_model_training_completed",
            evidence_type="model_version",
            evidence_id=int(model_version_id),
            evidence={"model_key": model_key, "version": version},
            priority=75,
        )
        return {
            "status": str(task.get("status") or "queued"),
            "task_id": int(task.get("id") or 0),
        }
    except Exception as exc:  # Training evidence remains valid if the queue is unavailable.
        return {"status": "enqueue_failed", "error": str(exc)[:300]}


def _enqueue_model_candidate_research(
    *, model_key: str, model_version_id: int, version: str,
) -> dict[str, Any]:
    return {
        "status": "retired_legacy_evaluation_protocol",
        "model_key": model_key,
        "model_version_id": int(model_version_id),
        "version": version,
        "replacement": MODEL_EVALUATION_CONTRACT,
        "production_change_forbidden": True,
    }


def _enqueue_model_strategy_impact_validation(
    *, model_key: str, model_version_id: int, version: str,
) -> dict[str, Any]:
    try:
        from app.services.evolution_orchestrator import create_evolution_task

        task = create_evolution_task(
            task_type="model_strategy_impact_validation",
            payload={
                "model_key": model_key,
                "model_version_id": int(model_version_id),
                "version": version,
                "production_change_forbidden": True,
            },
            dedupe_key=f"model_strategy_impact_validation:v2:{int(model_version_id)}",
            priority=76,
        )
        return {"status": str(task.get("status") or "queued"), "task_id": int(task.get("id") or 0)}
    except Exception as exc:
        return {"status": "enqueue_failed", "error": str(exc)[:300]}


def _enqueue_plan_model_autonomy_continuation(
    *, model_key: str, model_version_id: int,
) -> dict[str, Any]:
    try:
        from app.services.evolution_orchestrator import create_evolution_task

        task = create_evolution_task(
            task_type="plan_model_autonomy",
            payload={
                "trigger": "previous_task_native_model_training_completed",
                "predecessor_model_key": model_key,
                "predecessor_model_version_id": int(model_version_id),
            },
            dedupe_key=f"plan_model_autonomy:task-native-v1:after-{int(model_version_id)}",
            priority=73,
        )
        return {"status": str(task.get("status") or "queued"), "task_id": int(task.get("id") or 0)}
    except Exception as exc:
        return {"status": "enqueue_failed", "error": str(exc)[:300]}


def evaluate_plan_model_candidate_quick(
    model_key: str,
    candidate_spec: dict[str, Any],
    *,
    dataset: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the cheap, non-promotable two-window model diagnostic."""
    dataset = dataset or _build_plan_training_dataset(model_key)
    target_states = {
        str(item) for item in candidate_spec.get("target_market_states") or [] if str(item)
    }
    if target_states:
        dataset["train_sample_weight"] = np.asarray([
            2.0 if str(state) in target_states else 1.0
            for state in dataset.get("train_market_states") or []
        ], dtype=float)
    trained = _fit_lightgbm_plan_model(
        model_key,
        dataset,
        candidate_spec=candidate_spec,
        quick_diagnostic=True,
    )
    return dict(trained.get("metrics") or {})


def prepare_plan_model_quick_evaluator(
    model_key: str,
) -> tuple[Callable[[str, dict[str, Any]], dict[str, Any]], dict[str, Any]]:
    """Freeze one point-in-time dataset for the complete quick tournament."""
    dataset = _build_plan_training_dataset(model_key)

    def evaluate(key: str, spec: dict[str, Any]) -> dict[str, Any]:
        if key != model_key:
            raise ValueError("快速诊断模型与冻结数据集不一致")
        return evaluate_plan_model_candidate_quick(key, spec, dataset=dataset)

    frozen = {
        "date_start": dataset["dates"]["start"],
        "date_end": dataset["dates"]["end"],
        "trade_date_count": dataset["trade_date_count"],
        "sample_rows": dataset["sample_rows"],
        "dataset_signature": _dataset_signature(model_key, dataset),
    }
    return evaluate, frozen


def _run_training_sync(
    model_key: str,
    run_id: int,
    training_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started_at = datetime.now().isoformat()
    contract = PLAN_MODELS[model_key]
    timeline: list[dict[str, Any]] = []
    failure_response: dict[str, Any] | None = None

    def progress(phase: str, pct: float, message: str, metrics: dict[str, Any] | None = None) -> None:
        now = datetime.now().isoformat()
        timeline.append({"at": now, "phase": phase, "progress": pct, "message": message})
        previous = (get_model_train_run(run_id) or {}).get("metrics") or {}
        payload = {
            **previous,
            "model_key": model_key,
            "contract": contract["family"],
            **(metrics or {}),
            "timeline": timeline[-20:],
        }
        update_model_train_run(run_id, phase=phase, progress=pct, message=message, metrics=payload, started_at=started_at)

    try:
        progress("coverage", 0.1, "检查计划书数据覆盖")
        coverage = _coverage_snapshot(model_key)
        if not coverage["data_ready"]:
            raise RuntimeError(
                "训练数据不足："
                f"领域最低 {coverage['effective_sessions']}/{coverage['minimum_sessions']}，"
                f"正式 Walk-forward 历史 "
                f"{coverage['effective_sessions']}/{coverage['formal_walk_forward_sessions']} sessions"
            )
        progress("dataset", 0.22, "开始构造点时训练样本与query标签", {"coverage": coverage})
        dataset = _build_plan_training_dataset(model_key, on_progress=progress)
        context = dict(training_context or {})
        target_states = {
            str(item) for item in (
                ((context.get("training_objective") or {}).get("target_market_states") or [])
            ) if str(item)
        }
        if target_states:
            dataset["train_sample_weight"] = np.asarray([
                2.0 if str(state) in target_states else 1.0
                for state in (dataset.get("train_market_states") or [])
            ], dtype=float)
        dataset["targeted_training_context"] = context
        progress(
            "dataset",
            0.43,
            f"数据集就绪：{dataset['sample_rows']} 行，{dataset['trade_date_count']} 个交易日，{dataset['feature_count']} 个特征",
            {"coverage": coverage, "dataset": {
                "sample_rows": dataset["sample_rows"],
                "raw_rows": dataset["raw_rows"],
                "train_rows": dataset["train_rows"],
                "validation_rows": dataset["validation_rows"],
                "test_rows": dataset["test_rows"],
                "trade_date_count": dataset["trade_date_count"],
                "feature_count": dataset["feature_count"],
                "horizon": dataset["horizon"],
                "dates": dataset["dates"],
                "label_quantiles": dataset["label_quantiles"],
                "feature_computation_contract": dataset.get("feature_computation_contract") or {},
                "symbol_sampling": dataset.get("symbol_sampling") or {},
                "moneyflow_raw_rows": dataset.get("moneyflow_raw_rows"),
                "capital_observation_count": dataset.get("capital_observation_count"),
                "training_protocol": dataset["training_protocol"],
            }},
        )
        duplicate, duplicate_version, data_signature = _is_duplicate_training_dataset(model_key, dataset)
        if duplicate:
            duplicate_id = (duplicate_version or {}).get("id")
            duplicate_name = (duplicate_version or {}).get("version") or "最近版本"
            update_model_train_run(
                run_id,
                status="completed",
                phase="skipped_duplicate_dataset",
                progress=1.0,
                message=f"训练数据与 {duplicate_name} 完全一致，已跳过重复训练",
                model_version_id=int(duplicate_id) if duplicate_id else None,
                metrics={
                    "model_key": model_key,
                    "coverage": coverage,
                    "dataset": {
                        "sample_rows": dataset["sample_rows"],
                        "raw_rows": dataset["raw_rows"],
                        "train_rows": dataset["train_rows"],
                        "validation_rows": dataset["validation_rows"],
                        "test_rows": dataset["test_rows"],
                        "trade_date_count": dataset["trade_date_count"],
                        "feature_count": dataset["feature_count"],
                        "horizon": dataset["horizon"],
                        "dates": dataset["dates"],
                        "label_quantiles": dataset["label_quantiles"],
                        "symbol_sampling": dataset.get("symbol_sampling") or {},
                        "moneyflow_raw_rows": dataset.get("moneyflow_raw_rows"),
                        "capital_observation_count": dataset.get("capital_observation_count"),
                        "training_protocol": dataset["training_protocol"],
                    },
                    "training_data_signature": data_signature,
                    "duplicate_of_version_id": duplicate_id,
                    "timeline": [
                        *timeline[-19:],
                        {"at": datetime.now().isoformat(), "phase": "skipped_duplicate_dataset", "progress": 1.0, "message": f"数据指纹未变化，复用 {duplicate_name}，不生成新版本"},
                    ],
                },
                finished_at=datetime.now().isoformat(),
            )
            return {"ok": True, "run_id": run_id, "skipped": True, "reason": "duplicate_training_dataset", "duplicate_of_version_id": duplicate_id}
        progress("training", 0.55, f"训练真实LightGBM模型：训练 {dataset['train_rows']} 行，验证 {dataset['validation_rows']} 行", {"coverage": coverage, "train_rows": dataset["train_rows"], "validation_rows": dataset["validation_rows"]})
        candidate_spec = (
            context.get("model_candidate_spec")
            if isinstance(context.get("model_candidate_spec"), dict)
            else None
        )
        trained = _fit_lightgbm_plan_model(
            model_key,
            dataset,
            candidate_spec=candidate_spec,
        )
        progress("validation", 0.80, "执行任务原生滚动样本外评价")
        metrics = {
            "effective_sessions": dataset["trade_date_count"],
            "available_sessions": coverage["effective_sessions"],
            "training_data_signature": data_signature,
            "targeted_training_context": context,
            **trained["metrics"],
        }
        protocol_validation = validate_training_run_manifest({
            "model_key": model_key,
            "calendar": {
                "train_sessions": metrics.get("training_trade_date_count", 0),
                "validation_sessions": metrics.get("validation_trade_date_count", 0),
                "test_sessions": metrics.get("test_trade_date_count", 0),
                "purge_sessions": int(TRAINING_PROTOCOL["purge_sessions"]),
                "embargo_sessions": int(TRAINING_PROTOCOL["embargo_sessions"]),
            },
            "oos_windows": metrics.get("oos_windows", 0),
            "model_evaluation_contract": metrics.get(
                "model_evaluation_contract"
            ),
            "task_native_metrics_recorded": bool(metrics.get("task_metrics")),
            "task_baseline_comparison_executed": bool(
                (metrics.get("task_baseline") or {}).get(
                    "comparison_executed"
                )
            ),
            "task_baseline_outperformed": bool(
                (metrics.get("task_baseline") or {}).get("outperformed")
            ),
            "calibration_checked": True,
        })
        metric_gates = {
            "data_ready": True,
            "task_metric_passed": bool(metrics.get("task_metric_passed")),
            "current_task_native_evaluation_contract": (
                str(metrics.get("model_evaluation_contract") or "") == MODEL_EVALUATION_CONTRACT
            ),
            "task_native_walk_forward_oos": bool(
                (metrics.get("task_native_oos") or {}).get("passed")
            ),
            "task_native_baseline_outperformed": bool(
                (metrics.get("task_native_oos") or {}).get(
                    "baseline_outperformed"
                )
            ),
            "task_baseline_comparison_executed": bool(
                (metrics.get("task_baseline") or {}).get(
                    "comparison_executed"
                )
            ),
            "environment_coverage_complete": bool(
                (metrics.get("environment_coverage") or {}).get("complete")
            ),
            "training_protocol": bool(protocol_validation.get("ok")),
            "controlled_strategy_ab": False,
            "human_review": False,
            "live_observation_20_sessions": False,
        }
        metric_blocked = [key for key, passed in metric_gates.items() if key not in {"human_review", "live_observation_20_sessions"} and not passed]
        validation = {
            "status": (
                "pending_strategy_ab"
                if metric_blocked == ["controlled_strategy_ab"]
                else "failed"
            ),
            "production_eligible": False,
            "production_eligibility": {
                "eligible": False,
                "blocked_reasons": metric_blocked,
                "gates": metric_gates,
            },
        }
        validation["training_protocol"] = protocol_validation
        version = f"{model_key}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        root = models_root() / model_key / version
        root.mkdir(parents=True, exist_ok=True)
        model_path = root / "model.pkl"
        with model_path.open("wb") as handle:
            pickle.dump(trained["model"], handle)
        model_type = trained["model_type"]
        fold_models_by_number = {
            int(item["fold"]): item["model"]
            for item in trained.get("fold_models") or []
        }
        fold_artifacts: list[dict[str, Any]] = []
        for fold_evidence in metrics.get("walk_forward_evidence") or []:
            fold_no = int(fold_evidence.get("fold") or 0)
            fold_model = fold_models_by_number.get(fold_no)
            if fold_model is None:
                raise RuntimeError(f"Walk-forward第{fold_no}窗缺少冻结模型")
            fold_root = root / "walk_forward" / f"fold_{fold_no}"
            fold_root.mkdir(parents=True, exist_ok=True)
            fold_model_path = fold_root / "model.pkl"
            with fold_model_path.open("wb") as handle:
                pickle.dump(fold_model, handle)
            model_sha256 = hashlib.sha256(fold_model_path.read_bytes()).hexdigest()
            available_at = (
                f"{str(fold_evidence['validation_end'])[:4]}-"
                f"{str(fold_evidence['validation_end'])[4:6]}-"
                f"{str(fold_evidence['validation_end'])[6:8]}T15:00:00+08:00"
            )
            fold_artifact = {
                "artifact_type": "plan_model_walk_forward_fold_artifact",
                "binding_contract": "point_in_time_fold_model_v1",
                "model_key": model_key,
                "version": version,
                "fold": fold_no,
                "model_type": model_type,
                "model_path": str(fold_model_path),
                "model_sha256": model_sha256,
                "features": list(
                    trained["metrics"].get("selected_features") or PLAN_MODEL_FEATURES
                ),
                "training_dataset": {
                    "train_start": fold_evidence["train_start"],
                    "train_end": fold_evidence["train_end"],
                    "train_sessions": fold_evidence["train_sessions"],
                    "validation_start": fold_evidence["validation_start"],
                    "validation_end": fold_evidence["validation_end"],
                    "validation_sessions": fold_evidence["validation_sessions"],
                    "test_start": fold_evidence["test_start"],
                    "test_end": fold_evidence["test_end"],
                    "test_sessions": fold_evidence["test_sessions"],
                    "purge_sessions": fold_evidence["purge_sessions"],
                    "embargo_sessions": fold_evidence["embargo_sessions"],
                },
                "available_at": available_at,
                "created_at": datetime.now().isoformat(),
            }
            fold_artifact_path = fold_root / "artifact.json"
            fold_artifact_path.write_text(
                json.dumps(fold_artifact, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            frozen_binding = {
                "binding_contract": "point_in_time_fold_model_v1",
                "fold": fold_no,
                "artifact_path": str(fold_artifact_path),
                "model_sha256": model_sha256,
                "available_at": available_at,
                "train_end": fold_evidence["train_end"],
                "validation_end": fold_evidence["validation_end"],
                "test_start": fold_evidence["test_start"],
                "test_end": fold_evidence["test_end"],
            }
            fold_evidence["point_in_time_artifact"] = frozen_binding
            fold_artifacts.append(frozen_binding)
        artifact = {
            "model_key": model_key,
            "version": version,
            "contract": contract,
            "lightgbm_params": {
                **LIGHTGBM_PARAMS,
                **dict((candidate_spec or {}).get("param_overrides") or {}),
            },
            "features": list(
                trained["metrics"].get("selected_features") or PLAN_MODEL_FEATURES
            ),
            "model_path": str(model_path),
            "model_type": model_type,
            "predict_contract": {
                "input": {
                    feature: "number"
                    for feature in trained["metrics"].get("selected_features") or PLAN_MODEL_FEATURES
                },
                "output": {"score": "number"},
                "scorer": "app.services.plan_model_control.score_plan_model_features",
            },
            "output_contract": MODEL_OUTPUT_CONTRACTS.get(model_key, {}),
            "training_protocol": training_protocol_contract(model_key),
            "training_runtime_protocol": TRAINING_PROTOCOL,
            "coverage": coverage,
            "metrics": metrics,
            "created_at": datetime.now().isoformat(),
            "artifact_type": "plan_model_lightgbm_artifact",
            "walk_forward_fold_artifacts": fold_artifacts,
            "training_dataset": {
                "horizon": dataset["horizon"],
                "rows": dataset["sample_rows"],
                "train_rows": dataset["train_rows"],
                "validation_rows": dataset["validation_rows"],
                "test_rows": dataset["test_rows"],
                "labeled_trade_date_count": dataset["trade_date_count"],
                "train_trade_date_count": dataset["train_trade_date_count"],
                "validation_trade_date_count": dataset["validation_trade_date_count"],
                "test_trade_date_count": dataset["test_trade_date_count"],
                "daily_sample_limit": dataset["daily_sample_limit"],
                "symbol_sampling": dataset.get("symbol_sampling") or {},
                "moneyflow_raw_rows": dataset.get("moneyflow_raw_rows"),
                "capital_observation_count": dataset.get("capital_observation_count"),
                "feature_computation_contract": dataset.get("feature_computation_contract") or {},
                "narrative_label_audit": dataset.get("narrative_label_audit") or {},
                **dataset["dates"],
            },
        }
        artifact_path = root / "artifact.json"
        artifact_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        model_version_id = create_model_version(
            model_key=model_key,
            version=version,
            status="candidate",
            artifact_path=str(artifact_path),
            config={
                "contract": contract,
                "lightgbm_params": {
                    **LIGHTGBM_PARAMS,
                    **dict((candidate_spec or {}).get("param_overrides") or {}),
                },
                "model_candidate_spec": candidate_spec or {},
                "targeted_training_context": context,
            },
            metrics=metrics,
            validation=validation,
            is_active=False,
        )
        research_trigger = _enqueue_continuous_research_for_model_version(
            model_key=model_key,
            model_version_id=int(model_version_id),
            version=version,
        )
        model_candidate_trigger = (
            _enqueue_model_candidate_research(
                model_key=model_key,
                model_version_id=int(model_version_id),
                version=version,
            )
            if validation["status"] == "failed"
            and str(context.get("evaluation_stage") or "") != "formal_five_window"
            else {"status": "not_required"}
        )
        strategy_impact_trigger = (
            _enqueue_model_strategy_impact_validation(
                model_key=model_key,
                model_version_id=int(model_version_id),
                version=version,
            )
            if validation["status"] == "pending_strategy_ab"
            else {"status": "not_required"}
        )
        autonomy_continuation = _enqueue_plan_model_autonomy_continuation(
            model_key=model_key,
            model_version_id=int(model_version_id),
        )
        update_model_train_run(
            run_id,
            status="completed",
            phase="completed",
            progress=1.0,
            message="计划书专门模型真实LightGBM训练完成，等待上线观察与评审",
            model_version_id=model_version_id,
            metrics={
                "model_key": model_key,
                **metrics,
                "coverage": coverage,
                "continuous_research_trigger": research_trigger,
                "model_candidate_research_trigger": model_candidate_trigger,
                "model_strategy_impact_trigger": strategy_impact_trigger,
                "plan_model_autonomy_continuation": autonomy_continuation,
                "timeline": [
                    *timeline[-19:],
                    {"at": datetime.now().isoformat(), "phase": "completed", "progress": 1.0, "message": "训练完成，候选版本已生成"},
                ],
            },
            finished_at=datetime.now().isoformat(),
        )
        return {
            "ok": True,
            "run_id": run_id,
            "model_version_id": model_version_id,
            "version": version,
            "continuous_research_trigger": research_trigger,
            "model_candidate_research_trigger": model_candidate_trigger,
            "model_strategy_impact_trigger": strategy_impact_trigger,
            "plan_model_autonomy_continuation": autonomy_continuation,
        }
    except Exception as exc:
        previous = (get_model_train_run(run_id) or {}).get("metrics") or {}
        context = dict(training_context or {})
        prior_recovery = context.get("failure_recovery") if isinstance(
            context.get("failure_recovery"), dict
        ) else {}
        root_run_id = int(prior_recovery.get("root_run_id") or run_id)
        retry_count = int(prior_recovery.get("retry_count") or 0)
        classification = classify_plan_model_training_failure(exc)
        recovery = {
            "contract": TRAINING_FAILURE_RECOVERY_CONTRACT,
            "status": "detected",
            "classification": classification,
            "root_run_id": root_run_id,
            "failed_run_id": int(run_id),
            "retry_count": retry_count,
            "max_retries": int(
                prior_recovery.get("max_retries")
                or TRAINING_FAILURE_MAX_RETRIES
            ),
            "detected_at": datetime.now().astimezone().isoformat(),
        }
        update_model_train_run(
            run_id,
            status="failed",
            phase="failed",
            progress=1.0,
            message="计划书专门模型训练失败",
            metrics={
                **previous,
                "model_key": model_key,
                "targeted_training_context": context,
                "failure_recovery": recovery,
                "timeline": [
                    *timeline[-19:],
                    {"at": datetime.now().isoformat(), "phase": "failed", "progress": 1.0, "message": f"训练失败：{str(exc)[:160]}"},
                ],
            },
            error=str(exc)[:500],
            finished_at=datetime.now().isoformat(),
        )
        failure_response = {
            "ok": False,
            "run_id": run_id,
            "error": str(exc),
            "failure_recovery": recovery,
        }
    finally:
        _status_cache.update({"at": 0.0, "value": None})
        with _lock:
            _locks.discard(model_key)
    if failure_response is not None:
        recovery = failure_response["failure_recovery"]
        recovery_queue = _enqueue_plan_model_training_recovery(
            model_key=model_key,
            failed_run_id=int(run_id),
            root_run_id=int(recovery["root_run_id"]),
            retry_count=int(recovery["retry_count"]),
            classification=dict(recovery["classification"]),
        )
        failure_response["automatic_recovery"] = recovery_queue
        return failure_response
    return {"ok": False, "run_id": run_id, "error": "training_failed_without_evidence"}


def request_plan_model_training(
    model_key: str,
    *,
    training_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _status_cache.update({"at": 0.0, "value": None})
    _operations_cache.update({"at": 0.0, "value": None})
    if model_key not in PLAN_MODELS:
        return {"ok": False, "error": "unknown_plan_model", "model_key": model_key}
    blocker = _plan_model_candidate_governance_blocker(model_key)
    if blocker:
        return {
            "ok": True,
            "started": False,
            "reason": "candidate_governance_in_progress",
            "model_key": model_key,
            "model_version_id": int(blocker["id"]),
        }
    with _lock:
        if model_key in _locks:
            latest = _latest_run_for_model(model_key)
            return {"ok": True, "started": False, "reason": "training_in_progress", "run_id": (latest or {}).get("id")}
        _locks.add(model_key)
    run_id = create_model_train_run(f"计划书模型训练已创建：{model_key}")
    update_model_train_run(run_id, metrics={
        "model_key": model_key,
        "contract": PLAN_MODELS[model_key]["family"],
        "targeted_training_context": training_context or {},
    })
    threading.Thread(
        target=_run_training_sync,
        args=(model_key, run_id, training_context),
        daemon=True,
        name=f"plan-model-{model_key}",
    ).start()
    return {"ok": True, "started": True, "run_id": run_id, "model_key": model_key, "status": "running"}


def train_all_trainable_plan_models() -> dict[str, Any]:
    status = model_family_status()
    started = []
    skipped = []
    for item in status.get("items") or []:
        if item.get("status") != "trainable":
            skipped.append({"model_key": item.get("model_key"), "reason": f"status_{item.get('status')}"})
            continue
        result = request_plan_model_training(str(item.get("model_key") or ""))
        if result.get("started"):
            started.append(result)
        else:
            skipped.append({"model_key": item.get("model_key"), "reason": result.get("reason") or result.get("error")})
    return {"ok": True, "started": started, "skipped": skipped, "started_count": len(started), "skipped_count": len(skipped)}


def review_all_candidate_plan_models(*, reviewer: str = "operator", comment: str = "批量上线评审通过", waive_live_observation: bool = False) -> dict[str, Any]:
    reviewed = []
    skipped = []
    for item in model_family_status().get("items") or []:
        latest = item.get("latest_version") or {}
        if item.get("status") != "candidate" or not latest.get("id"):
            skipped.append({"model_key": item.get("model_key"), "reason": f"status_{item.get('status')}"})
            continue
        reviewed.append(review_plan_model_version(int(latest["id"]), reviewer=reviewer, decision="approved", comment=comment, waive_live_observation=waive_live_observation))
    return {"ok": True, "reviewed": reviewed, "skipped": skipped, "reviewed_count": len(reviewed), "skipped_count": len(skipped)}


def review_plan_model_version(
    model_version_id: int,
    *,
    reviewer: str,
    decision: str,
    comment: str = "",
    waive_live_observation: bool = False,
) -> dict[str, Any]:
    _status_cache.update({"at": 0.0, "value": None})
    version = get_model_version(model_version_id)
    if not version:
        return {"ok": False, "error": "model_version_not_found", "model_version_id": model_version_id}
    model_key = str(version.get("model_key") or "")
    if model_key not in PLAN_MODELS:
        return {"ok": False, "error": "not_plan_model_version", "model_key": model_key}
    if decision not in {"approved", "rejected"}:
        return {"ok": False, "error": "invalid_review_decision"}
    validation = dict(version.get("validation") or {})
    metrics = dict(version.get("metrics") or {})
    gates = dict((validation.get("production_eligibility") or {}).get("gates") or {})
    gates["current_model_evaluation_contract"] = (
        str(metrics.get("model_evaluation_contract") or "") == MODEL_EVALUATION_CONTRACT
    )
    gates["controlled_strategy_ab"] = controlled_strategy_ab_passed_current_contract(
        model_key,
        metrics.get("strategy_impact_ab"),
    )
    gates["human_review"] = decision == "approved"
    gates["live_observation_20_sessions"] = bool(
        gates.get("live_observation_20_sessions")
    )
    gates["simulation_only_live_observation_waived"] = False
    review = {
        "status": decision,
        "reviewer": reviewer.strip() or "operator",
        "comment": comment.strip(),
        "waive_live_observation_requested": bool(waive_live_observation),
        "waive_live_observation": False,
        "waiver_policy": "forbidden",
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "policy": "plan_model_release_review_v2",
    }
    blocked = []
    if decision != "approved":
        blocked.append("human_review_rejected")
    if waive_live_observation:
        blocked.append("live_observation_waiver_forbidden")
    for key, passed in gates.items():
        if key == "simulation_only_live_observation_waived":
            continue
        if not bool(passed):
            blocked.append(key)
    eligible = decision == "approved" and not blocked
    validation["review"] = review
    validation["production_eligible"] = eligible
    validation["production_eligibility"] = {
        "eligible": eligible,
        "blocked_reasons": blocked,
        "gates": gates,
        "review": review,
    }
    release_audit = audit_plan_model_production_eligibility(
        {**version, "validation": validation}
    )
    if eligible and not release_audit.get("passed"):
        blocked = sorted(
            set(
                [
                    *blocked,
                    *[
                        f"current_production_gate:{reason}"
                        for reason in release_audit.get("blocked_reasons") or []
                    ],
                ]
            )
        )
        eligible = False
        validation["production_eligible"] = False
        validation["production_eligibility"].update(
            {
                "eligible": False,
                "blocked_reasons": blocked,
            }
        )
    validation["production_eligibility"]["current_gate_audit"] = release_audit
    update_model_version_validation(model_version_id, validation=validation)
    if eligible:
        set_active_model_version(model_version_id)
    return {
        "ok": True,
        "model_version_id": model_version_id,
        "model_key": model_key,
        "version": version.get("version"),
        "review": review,
        "production_eligible": eligible,
        "production_eligibility": validation["production_eligibility"],
    }


def artifact_file_exists(path: str) -> bool:
    return bool(path) and Path(path).exists()
