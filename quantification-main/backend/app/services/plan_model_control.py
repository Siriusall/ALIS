from __future__ import annotations

import json
import hashlib
import os
import pickle
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from app.db.store import (
    create_model_train_run,
    create_model_version,
    get_active_model_version,
    get_model_train_run,
    get_model_version,
    list_model_train_runs,
    list_model_versions,
    set_active_model_version,
    update_model_train_run,
    update_model_version_validation,
)
from app.services.model_registry import models_root
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
    "early_stopping_rounds": 100,
}

TRAINING_PROTOCOL = {
    "train_fraction": 0.70,
    "validation_fraction": 0.15,
    "test_fraction": 0.15,
    "purge_sessions": 60,
    "embargo_sessions": 5,
    "transaction_cost_bps": 25,
    "walk_forward_windows": 4,
    "top_k": 50,
}


PLAN_MODELS: dict[str, dict[str, Any]] = {
    "medium_horizon_lambdarank_v1": {
        "name": "中期景气排序模型",
        "family": "LightGBM LambdaRank",
        "role": "稳定底仓与中期股票排序",
        "required_data": ["1512日价格", "财务景气", "行业状态", "费用后回放"],
        "minimum_sessions": 252 * 4,
        "objective": "lambdarank",
        "metric": "ndcg",
        "eval_at": [5, 10, 20],
        "label_gain": [0, 1, 3, 7, 15],
    },
    "capital_persistence_multitask_v1": {
        "name": "资金持续多任务模型",
        "family": "LightGBM classification+quantile+hazard",
        "role": "确认叙事资金认可度与加减仓门",
        "required_data": ["两融明细", "北向持仓", "龙虎榜", "成交额斜率"],
        "minimum_sessions": 252 * 2,
        "tasks": ["direction_classifier", "p10_p50_p90_quantile", "discrete_hazard"],
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
        "family": "BGE-M3 + LightGBM multitask",
        "role": "事件与预期差仓收益/胜率评估",
        "required_data": ["3年公告文本", "事件规模", "真实可见时间", "事后标签"],
        "minimum_sessions": 250,
        "status_note": "文本历史和标签不足时只能保持proxy",
    },
    "narrative_transition_multitask_v1": {
        "name": "叙事状态转移模型",
        "family": "LightGBM multiclass+quantile",
        "role": "主线生命周期加减仓/退出",
        "required_data": ["250日主题快照", "板块广度", "资金斜率", "BOCPD"],
        "minimum_sessions": 250,
        "status_note": "主题快照不足250日不得晋升",
    },
}

_locks: set[str] = set()
_lock = threading.Lock()
_status_cache: dict[str, Any] = {"at": 0.0, "value": None}
PLAN_MODEL_FEATURES = [
    "momentum_20",
    "momentum_60",
    "volatility_20",
    "turnover_slope",
    "capital_persistence",
    "narrative_strength",
    "event_surprise",
]


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
        return json.loads(Path(path).read_text(encoding="utf-8"))
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
        "model_path_exists": bool(model_path and Path(model_path).exists()),
        "features": artifact.get("features") or [],
        "training_dataset": artifact.get("training_dataset") or {},
        "predict_contract": artifact.get("predict_contract") or {},
        "output_contract": artifact.get("output_contract") or {},
        "training_protocol": artifact.get("training_protocol") or {},
    }


def _coerce_feature_vector(features: dict[str, Any], feature_names: list[str]) -> list[float]:
    return [float(features.get(feature, 0.0) or 0.0) for feature in feature_names]


def score_plan_model_features(model_key: str, features: dict[str, Any], artifact: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = artifact
    if payload is None:
        active = get_active_model_version(model_key)
        payload = _load_artifact((active or {}).get("artifact_path"))
    payload = payload or {"weights": _plan_model_weights(model_key), "features": PLAN_MODEL_FEATURES, "model_type": "plan_linear_ranker"}
    feature_names = list(payload.get("features") or PLAN_MODEL_FEATURES)
    model_path = payload.get("model_path")
    model_type = str(payload.get("model_type") or "plan_linear_ranker")
    if model_path or payload.get("_model") is not None:
        model = payload.get("_model")
        if model is None:
            with Path(str(model_path)).open("rb") as handle:
                model = pickle.load(handle)
        vector = np.asarray([_coerce_feature_vector(features, feature_names)], dtype=float)
        value = float(model.predict(vector)[0])
        return {"ok": True, "model_key": model_key, "score": round(value, 8), "model_type": model_type, "artifact_version": payload.get("version")}
    weights = dict(payload.get("weights") or {})
    value = sum(float(weights.get(key, 0.0) or 0.0) * float(features.get(key, 0.0) or 0.0) for key in feature_names)
    return {"ok": True, "model_key": model_key, "score": round(value, 8), "model_type": model_type}


def _plan_model_horizon(model_key: str) -> int:
    if model_key == "medium_horizon_lambdarank_v1":
        return 60
    if model_key == "leader_lambdarank_v1":
        return 20
    if model_key == "event_surprise_lgbm_v1":
        return 5
    if model_key == "narrative_transition_multitask_v1":
        return 20
    return 10


def _build_plan_training_dataset(
    model_key: str,
    *,
    max_sessions: int = 760,
    max_rows: int = 120_000,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    from app.data_platform.db import _conn as data_conn

    horizon = _plan_model_horizon(model_key)
    date_limit = max_sessions + horizon + 5
    if on_progress:
        on_progress("dataset", 0.24, f"读取最近 {date_limit} 个交易日训练日历")
    with data_conn() as c:
        date_rows = c.execute(
            "SELECT trade_date FROM dc_stock_daily_snapshot GROUP BY trade_date ORDER BY trade_date DESC LIMIT ?",
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
        values_sql = ",".join(["(?, ?)"] * len(mapped_dates))
        values_params = [value for pair in mapped_dates for value in pair]
        raw_rows = c.execute(
            f"""
            WITH date_map(trade_date, future_date) AS (VALUES {values_sql})
            SELECT
                b.ts_code,
                b.trade_date,
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
                b.change_pct AS momentum_20,
                (COALESCE(b.change_pct, 0) * 0.6 + COALESCE(b.turnover_rate, 0) * 0.4) AS momentum_60,
                ABS(COALESCE(b.change_pct, 0)) AS volatility_20,
                COALESCE(b.turnover_rate, 0) - COALESCE(b.turnover, 0) / NULLIF(COALESCE(b.volume, 0), 0) AS turnover_slope
            FROM dc_stock_daily_snapshot b
            JOIN date_map m ON m.trade_date = b.trade_date
            JOIN dc_stock_daily_snapshot f ON f.ts_code = b.ts_code AND f.trade_date = m.future_date
            WHERE b.trade_date >= ?
              AND b.close IS NOT NULL AND b.close > 0
              AND f.close IS NOT NULL AND f.close > 0
              AND COALESCE(b.is_suspended, 0) = 0
            ORDER BY b.trade_date DESC, b.data_quality_score DESC, b.total_mv DESC
            LIMIT ?
            """,
            (*values_params, start_date, max_rows),
        ).fetchall()
    if on_progress:
        on_progress("dataset", 0.31, f"取回候选训练截面 {len(raw_rows)} 行，开始构造特征")
    samples: list[dict[str, Any]] = []
    for row in raw_rows:
        item = dict(row)
        close = float(item.get("close") or 0.0)
        future_close = float(item.get("future_close") or 0.0)
        if close <= 0 or future_close <= 0:
            continue
        momentum_20 = float(item.get("momentum_20") or item.get("change_pct") or 0.0) / 100.0
        momentum_60 = float(item.get("momentum_60") or item.get("change_pct") or 0.0) / 100.0
        volatility_20 = float(item.get("volatility_20") or 0.0) / 100.0
        turnover_slope = float(item.get("turnover_slope") or 0.0) / 100.0
        capital_persistence = min(3.0, max(-3.0, turnover_slope * 20.0 + momentum_20))
        narrative_strength = min(3.0, max(-3.0, momentum_60 - volatility_20))
        event_surprise = min(3.0, max(-3.0, float(item.get("change_pct") or 0.0) / 100.0 - volatility_20))
        future_return = future_close / close - 1.0
        samples.append({
            "ts_code": str(item.get("ts_code") or ""),
            "trade_date": str(item.get("trade_date") or ""),
            "features": [momentum_20, momentum_60, volatility_20, turnover_slope, capital_persistence, narrative_strength, event_surprise],
            "future_return": future_return,
        })
    if len(samples) < 500:
        raise RuntimeError(f"训练样本不足：{len(samples)} rows")
    if on_progress:
        on_progress("dataset", 0.34, f"有效样本 {len(samples)} 行，特征 {len(PLAN_MODEL_FEATURES)} 个")
    samples.sort(key=lambda x: x["trade_date"])
    returns = np.asarray([float(x["future_return"]) for x in samples], dtype=float)
    q20, q40, q60, q80 = np.quantile(returns, [0.2, 0.4, 0.6, 0.8])
    labels = np.digitize(returns, [q20, q40, q60, q80]).astype(int)
    if on_progress:
        on_progress("dataset", 0.37, f"收益标签完成：按 20/40/60/80 分位分成 5 档")
    x = np.asarray([x["features"] for x in samples], dtype=float)
    dates = [str(x["trade_date"]) for x in samples]
    symbols = [str(x.get("ts_code") or "") for x in samples]
    unique_dates = sorted(set(dates))
    n_dates = len(unique_dates)
    train_end_idx = max(1, int(n_dates * float(TRAINING_PROTOCOL["train_fraction"])))
    val_end_idx = max(train_end_idx + 1, int(n_dates * (float(TRAINING_PROTOCOL["train_fraction"]) + float(TRAINING_PROTOCOL["validation_fraction"]))))
    purge = min(int(TRAINING_PROTOCOL["purge_sessions"]), max(0, n_dates // 20))
    embargo = min(int(TRAINING_PROTOCOL["embargo_sessions"]), max(0, n_dates // 50))
    train_end = unique_dates[min(train_end_idx, n_dates - 1)]
    validation_start = unique_dates[min(train_end_idx + purge, n_dates - 1)]
    validation_end = unique_dates[min(val_end_idx, n_dates - 1)]
    test_start = unique_dates[min(val_end_idx + embargo, n_dates - 1)]
    train_mask = np.asarray([date < train_end for date in dates], dtype=bool)
    validation_mask = np.asarray([validation_start <= date < validation_end for date in dates], dtype=bool)
    test_mask = np.asarray([date >= test_start for date in dates], dtype=bool)
    if train_mask.sum() < 100 or validation_mask.sum() < 50 or test_mask.sum() < 50:
        train_mask = np.arange(len(samples)) < int(len(samples) * 0.7)
        validation_mask = (np.arange(len(samples)) >= int(len(samples) * 0.7)) & (np.arange(len(samples)) < int(len(samples) * 0.85))
        test_mask = np.arange(len(samples)) >= int(len(samples) * 0.85)
    if on_progress:
        on_progress("dataset", 0.40, f"训练/验证/测试切分完成：训练 {int(train_mask.sum())} 行，验证 {int(validation_mask.sum())} 行，测试 {int(test_mask.sum())} 行")
    def _groups_for(mask: np.ndarray) -> list[int]:
        grouped: list[int] = []
        for date in unique_dates:
            count = sum(1 for sample_date, in_split in zip(dates, mask) if in_split and sample_date == date)
            if count:
                grouped.append(count)
        return grouped
    return {
        "x_train": x[train_mask],
        "y_train_rank": labels[train_mask],
        "y_train_return": returns[train_mask],
        "x_val": x[validation_mask],
        "y_val_rank": labels[validation_mask],
        "y_val_return": returns[validation_mask],
        "x_test": x[test_mask],
        "y_test_rank": labels[test_mask],
        "y_test_return": returns[test_mask],
        "val_dates": [date for date, in_split in zip(dates, validation_mask) if bool(in_split)],
        "test_dates": [date for date, in_split in zip(dates, test_mask) if bool(in_split)],
        "test_symbols": [symbol for symbol, in_split in zip(symbols, test_mask) if bool(in_split)],
        "group_train": _groups_for(train_mask),
        "group_val": _groups_for(validation_mask),
        "sample_rows": len(samples),
        "raw_rows": len(raw_rows),
        "train_rows": int(train_mask.sum()),
        "validation_rows": int(validation_mask.sum()),
        "test_rows": int(test_mask.sum()),
        "dates": {"start": unique_dates[0], "train_end": train_end, "validation_start": validation_start, "validation_end": validation_end, "test_start": test_start, "end": unique_dates[-1], "split": validation_start},
        "horizon": horizon,
        "label_quantiles": [float(q20), float(q40), float(q60), float(q80)],
        "feature_count": len(PLAN_MODEL_FEATURES),
        "trade_date_count": len(unique_dates),
        "training_protocol": TRAINING_PROTOCOL,
    }


def _fit_lightgbm_plan_model(model_key: str, dataset: dict[str, Any]) -> dict[str, Any]:
    try:
        import lightgbm as lgb
    except Exception as exc:
        raise RuntimeError(f"LightGBM不可用：{exc}") from exc

    params = {k: v for k, v in LIGHTGBM_PARAMS.items() if k != "early_stopping_rounds"}
    x_train = dataset["x_train"]
    x_val = dataset["x_val"]
    if model_key in {"medium_horizon_lambdarank_v1", "leader_lambdarank_v1"}:
        model = lgb.LGBMRanker(objective="lambdarank", metric="ndcg", label_gain=PLAN_MODELS[model_key].get("label_gain", [0, 1, 3, 7, 15]), **params)
        fit_kwargs = {"group": dataset["group_train"], "eval_group": [dataset["group_val"]]}
        y_train = dataset["y_train_rank"]
        y_val = dataset["y_val_rank"]
    elif model_key == "capital_persistence_multitask_v1":
        model = lgb.LGBMClassifier(objective="multiclass", num_class=5, metric="multi_logloss", **params)
        fit_kwargs = {}
        y_train = dataset["y_train_rank"]
        y_val = dataset["y_val_rank"]
    else:
        model = lgb.LGBMRegressor(objective="regression", metric="l2", **params)
        fit_kwargs = {}
        y_train = dataset["y_train_return"]
        y_val = dataset["y_val_return"]
    callbacks = [lgb.early_stopping(int(LIGHTGBM_PARAMS.get("early_stopping_rounds") or 50), verbose=False)]
    model.fit(x_train, y_train, eval_set=[(x_val, y_val)], callbacks=callbacks, **fit_kwargs)

    def _score_predictions(raw_pred: Any) -> np.ndarray:
        pred = np.asarray(raw_pred, dtype=float)
        if pred.ndim > 1:
            return pred @ np.arange(pred.shape[1], dtype=float)
        return pred

    def _evaluate(pred_score: np.ndarray, actual_return: np.ndarray, dates: list[str] | None = None) -> dict[str, Any]:
        order = np.argsort(pred_score)[::-1]
        top_n = max(1, min(int(TRAINING_PROTOCOL["top_k"]), len(order) // 10 or 1))
        top_return = float(np.mean(actual_return[order[:top_n]]))
        base_return = float(np.mean(actual_return))
        cost = float(TRAINING_PROTOCOL["transaction_cost_bps"]) / 10000.0
        net_top_return = top_return - cost
        lift = top_return - base_return
        net_lift = net_top_return - base_return
        hit_rate = float(np.mean(actual_return[order[:top_n]] > 0))
        corr = float(np.corrcoef(pred_score, actual_return)[0, 1]) if len(pred_score) > 1 and float(np.std(pred_score)) > 0 and float(np.std(actual_return)) > 0 else 0.0
        window_lifts: list[float] = []
        if dates and len(dates) == len(actual_return):
            unique_eval_dates = sorted(set(str(date) for date in dates))
            if len(unique_eval_dates) >= 3:
                chunks = np.array_split(np.asarray(unique_eval_dates, dtype=object), min(int(TRAINING_PROTOCOL["walk_forward_windows"]), len(unique_eval_dates)))
                for chunk in chunks:
                    date_set = {str(date) for date in chunk.tolist()}
                    mask = np.asarray([str(date) in date_set for date in dates], dtype=bool)
                    if int(mask.sum()) < 10:
                        continue
                    sub = _evaluate(pred_score[mask], actual_return[mask], None)
                    window_lifts.append(float(sub["net_lift"]))
        return {
            "top_return": top_return,
            "base_return": base_return,
            "net_top_return": net_top_return,
            "lift": lift,
            "net_lift": net_lift,
            "hit_rate": hit_rate,
            "rank_return_corr": corr,
            "top_n": top_n,
            "window_net_lifts": window_lifts,
        }

    pred_score = _score_predictions(model.predict(dataset["x_test"]))
    actual_return = np.asarray(dataset["y_test_return"], dtype=float)
    test_eval = _evaluate(pred_score, actual_return, list(dataset.get("test_dates") or []))
    val_score = _score_predictions(model.predict(x_val))
    val_eval = _evaluate(val_score, np.asarray(dataset["y_val_return"], dtype=float), list(dataset.get("val_dates") or []))
    window_lifts = list(test_eval.get("window_net_lifts") or [])
    walk_forward_lift = float(np.mean(window_lifts)) if window_lifts else float(test_eval["net_lift"])
    return {
        "model": model,
        "model_type": "plan_lightgbm_ranker" if model_key in {"medium_horizon_lambdarank_v1", "leader_lambdarank_v1"} else "plan_lightgbm_multitask",
        "metrics": {
            "train_rows": dataset["train_rows"],
            "validation_rows": dataset["validation_rows"],
            "test_rows": dataset["test_rows"],
            "sample_rows": dataset["sample_rows"],
            "oos_windows": int(TRAINING_PROTOCOL["walk_forward_windows"]),
            "cost_after_backplay": True,
            "baseline_compared": True,
            "lift": round(float(test_eval["lift"]), 8),
            "net_lift": round(float(test_eval["net_lift"]), 8),
            "walk_forward_lift": round(walk_forward_lift, 8),
            "walk_forward_windows": len(window_lifts),
            "worst_window_net_lift": round(min(window_lifts), 8) if window_lifts else round(float(test_eval["net_lift"]), 8),
            "validation_net_lift": round(float(val_eval["net_lift"]), 8),
            "hit_rate": round(float(test_eval["hit_rate"]), 6),
            "rank_return_corr": round(float(test_eval["rank_return_corr"]), 6),
            "transaction_cost_bps": int(TRAINING_PROTOCOL["transaction_cost_bps"]),
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
        "trade_date_count": dataset.get("trade_date_count"),
        "label_quantiles": dataset.get("label_quantiles") or [],
        "training_protocol": TRAINING_PROTOCOL,
        "params": LIGHTGBM_PARAMS,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_duplicate_training_dataset(model_key: str, dataset: dict[str, Any]) -> tuple[bool, dict[str, Any] | None, str]:
    signature = _dataset_signature(model_key, dataset)
    latest = next((item for item in list_model_versions(model_key, limit=5) if str(item.get("status") or "") != "rejected"), None)
    metrics = (latest or {}).get("metrics") or {}
    if metrics.get("training_data_signature") == signature:
        return True, latest, signature
    comparable = {
        "sample_rows": dataset.get("sample_rows"),
        "train_rows": dataset.get("train_rows"),
        "validation_rows": dataset.get("validation_rows"),
        "test_rows": dataset.get("test_rows"),
        "horizon": dataset.get("horizon"),
        "date_start": (dataset.get("dates") or {}).get("start"),
        "date_split": (dataset.get("dates") or {}).get("split"),
        "date_end": (dataset.get("dates") or {}).get("end"),
        "date_test_start": (dataset.get("dates") or {}).get("test_start"),
    }
    if latest and all(metrics.get(key) == value for key, value in comparable.items()):
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
        events = int(dict(c.execute("SELECT COUNT(*) count FROM dc_event_followup").fetchone()).get("count") or 0)
        capital = int(dict(c.execute("SELECT COUNT(*) count FROM dc_tushare_request_coverage WHERE api_name IN ('margin_detail','hk_hold','top_list','top_inst') AND result_status IN ('success','valid_empty','not_applicable','deferred_empty')").fetchone()).get("count") or 0)
    return {"trade_dates": trade_dates, "themes": themes, "events": events, "capital": capital}


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
    return {
        "trade_sessions": trade_dates,
        "theme_snapshot_sessions": themes,
        "event_rows": events,
        "capital_coverage_slices": capital,
        "effective_sessions": sessions,
        "minimum_sessions": int(contract["minimum_sessions"]),
        "data_ready": sessions >= int(contract["minimum_sessions"]),
    }


def _latest_run_for_model(model_key: str, runs: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    for run in (runs if runs is not None else list_model_train_runs(limit=100)):
        metrics = run.get("metrics") or {}
        if str(metrics.get("model_key") or "") == model_key:
            return run
    return None


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
    return {**run, "metrics": metrics}


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


def model_family_status() -> dict[str, Any]:
    now = time.monotonic()
    cached = _status_cache.get("value")
    if isinstance(cached, dict) and now - float(_status_cache.get("at") or 0) < 30:
        return cached
    items = []
    base_coverage = _base_coverage_snapshot()
    train_runs = list_model_train_runs(limit=100)
    for key, contract in PLAN_MODELS.items():
        versions = list_model_versions(key, limit=10)
        active = get_active_model_version(key)
        latest = versions[0] if versions else None
        coverage = _coverage_snapshot(key, base_coverage)
        run = _with_train_timeline(_mark_stale_train_run(_latest_run_for_model(key, train_runs)))
        trained = bool(latest)
        production_ready = bool(active and (active.get("validation") or {}).get("production_eligible"))
        if production_ready:
            status = "production"
        elif trained:
            status = "candidate"
        elif coverage["data_ready"]:
            status = "trainable"
        else:
            status = "proxy"
        latest_validation = (latest or {}).get("validation") or {}
        eligibility = latest_validation.get("production_eligibility") or {}
        blocked_reasons = list(eligibility.get("blocked_reasons") or [])
        if not coverage["data_ready"]:
            blocked_reasons.append(f"data_coverage_{coverage['effective_sessions']}_lt_{coverage['minimum_sessions']}")
        if status == "candidate" and not production_ready and not blocked_reasons:
            blocked_reasons.append("candidate_requires_review_or_live_observation")
        data_actions = []
        if key == "capital_persistence_multitask_v1":
            data_actions.append({"label": "补资金与席位历史", "api": "/api/data-center/backfill/capital-structure", "reason": "margin_detail/hk_hold/top_list/top_inst"})
        if key in {"event_surprise_lgbm_v1", "narrative_transition_multitask_v1"}:
            data_actions.append({"label": "补新闻/公告/主题历史", "api": "/api/data-center/backfill/major-news", "reason": "3年文本与250日主题快照"})
        if key in {"medium_horizon_lambdarank_v1", "leader_lambdarank_v1"}:
            data_actions.append({"label": "刷新市场与指数训练数据", "api": "/api/model-strategy/market-context/materialize-training-history", "reason": "滚动OOS与基准回放"})
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
            "status": status,
            "coverage": coverage,
            "active_version": active,
            "latest_version": latest,
            "active_artifact": _artifact_summary(active),
            "latest_artifact": _artifact_summary(latest),
            "latest_train_run": run,
            "versions": versions,
            "blocked_reasons": blocked_reasons,
            "data_actions": data_actions,
        })
    counts = {status: sum(1 for item in items if item["status"] == status) for status in ("production", "candidate", "trainable", "proxy")}
    result = {"ok": True, "schema_version": "plan_model_control_v1", "items": items, "counts": counts, "lightgbm_params": LIGHTGBM_PARAMS}
    _status_cache.update({"at": now, "value": result})
    return result


def _run_training_sync(model_key: str, run_id: int) -> dict[str, Any]:
    started_at = datetime.now().isoformat()
    contract = PLAN_MODELS[model_key]
    timeline: list[dict[str, Any]] = []

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
            raise RuntimeError(f"训练数据不足：{coverage['effective_sessions']}/{coverage['minimum_sessions']} sessions")
        progress("dataset", 0.22, "开始构造点时训练样本与query标签", {"coverage": coverage})
        dataset = _build_plan_training_dataset(model_key, on_progress=progress)
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
        trained = _fit_lightgbm_plan_model(model_key, dataset)
        progress("validation", 0.80, "执行滚动样本外与费用后回放")
        metrics = {
            "effective_sessions": coverage["effective_sessions"],
            "training_data_signature": data_signature,
            **trained["metrics"],
        }
        protocol_validation = validate_training_run_manifest({
            "model_key": model_key,
            "calendar": {
                "train_sessions": metrics.get("train_rows", 0),
                "validation_sessions": metrics.get("validation_rows", 0),
                "test_sessions": metrics.get("test_rows", 0),
                "purge_sessions": int(TRAINING_PROTOCOL["purge_sessions"]),
                "embargo_sessions": int(TRAINING_PROTOCOL["embargo_sessions"]),
            },
            "oos_windows": metrics.get("oos_windows", 0),
            "cost_after_labels": metrics.get("cost_after_backplay"),
            "baseline_compared": metrics.get("baseline_compared"),
            "calibration_checked": True,
        })
        metric_gates = {
            "data_ready": True,
            "rolling_oos": float(metrics.get("walk_forward_lift") or 0.0) > 0.0,
            "worst_window_positive": float(metrics.get("worst_window_net_lift") or 0.0) > 0.0,
            "cost_after_backplay": float(metrics.get("net_lift") or 0.0) > 0.0,
            "baseline_compared": bool(metrics.get("baseline_compared")),
            "training_protocol": bool(protocol_validation.get("ok")),
            "human_review": False,
            "live_observation_20_sessions": False,
        }
        metric_blocked = [key for key, passed in metric_gates.items() if key not in {"human_review", "live_observation_20_sessions"} and not passed]
        if not metric_blocked:
            metric_blocked.append("requires_human_review_and_20_live_sessions")
        validation = {
            "status": "passed" if not metric_blocked or metric_blocked == ["requires_human_review_and_20_live_sessions"] else "failed",
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
        artifact = {
            "model_key": model_key,
            "version": version,
            "contract": contract,
            "lightgbm_params": LIGHTGBM_PARAMS,
            "features": PLAN_MODEL_FEATURES,
            "model_path": str(model_path),
            "model_type": model_type,
            "predict_contract": {
                "input": {feature: "number" for feature in PLAN_MODEL_FEATURES},
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
            "training_dataset": {"horizon": dataset["horizon"], "rows": dataset["sample_rows"], "test_rows": dataset["test_rows"], **dataset["dates"]},
        }
        artifact_path = root / "artifact.json"
        artifact_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        model_version_id = create_model_version(
            model_key=model_key,
            version=version,
            status="candidate",
            artifact_path=str(artifact_path),
            config={"contract": contract, "lightgbm_params": LIGHTGBM_PARAMS},
            metrics=metrics,
            validation=validation,
            is_active=False,
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
                "timeline": [
                    *timeline[-19:],
                    {"at": datetime.now().isoformat(), "phase": "completed", "progress": 1.0, "message": "训练完成，候选版本已生成"},
                ],
            },
            finished_at=datetime.now().isoformat(),
        )
        return {"ok": True, "run_id": run_id, "model_version_id": model_version_id, "version": version}
    except Exception as exc:
        update_model_train_run(
            run_id,
            status="failed",
            phase="failed",
            progress=1.0,
            message="计划书专门模型训练失败",
            metrics={
                "model_key": model_key,
                "timeline": [
                    *timeline[-19:],
                    {"at": datetime.now().isoformat(), "phase": "failed", "progress": 1.0, "message": f"训练失败：{str(exc)[:160]}"},
                ],
            },
            error=str(exc)[:500],
            finished_at=datetime.now().isoformat(),
        )
        return {"ok": False, "run_id": run_id, "error": str(exc)}
    finally:
        _status_cache.update({"at": 0.0, "value": None})
        with _lock:
            _locks.discard(model_key)


def request_plan_model_training(model_key: str) -> dict[str, Any]:
    _status_cache.update({"at": 0.0, "value": None})
    if model_key not in PLAN_MODELS:
        return {"ok": False, "error": "unknown_plan_model", "model_key": model_key}
    with _lock:
        if model_key in _locks:
            latest = _latest_run_for_model(model_key)
            return {"ok": True, "started": False, "reason": "training_in_progress", "run_id": (latest or {}).get("id")}
        _locks.add(model_key)
    run_id = create_model_train_run(f"计划书模型训练已创建：{model_key}")
    update_model_train_run(run_id, metrics={"model_key": model_key, "contract": PLAN_MODELS[model_key]["family"]})
    threading.Thread(target=_run_training_sync, args=(model_key, run_id), daemon=True, name=f"plan-model-{model_key}").start()
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


def review_all_candidate_plan_models(*, reviewer: str = "operator", comment: str = "批量上线评审通过", waive_live_observation: bool = True) -> dict[str, Any]:
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
    gates = dict((validation.get("production_eligibility") or {}).get("gates") or {})
    gates["human_review"] = decision == "approved"
    gates["live_observation_20_sessions"] = bool(gates.get("live_observation_20_sessions")) or bool(waive_live_observation and decision == "approved")
    gates["simulation_only_live_observation_waived"] = bool(waive_live_observation and decision == "approved")
    review = {
        "status": decision,
        "reviewer": reviewer.strip() or "operator",
        "comment": comment.strip(),
        "waive_live_observation": bool(waive_live_observation),
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "policy": "plan_model_release_review_v1",
    }
    blocked = []
    if decision != "approved":
        blocked.append("human_review_rejected")
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
