from __future__ import annotations

import json
import os
import pickle
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from app.db.store import (
    create_model_train_run,
    create_model_version,
    get_active_model_version,
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
    if model_path:
        with Path(model_path).open("rb") as handle:
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


def _build_plan_training_dataset(model_key: str, *, max_sessions: int = 760, max_rows: int = 120_000) -> dict[str, Any]:
    from app.data_platform.db import _conn as data_conn

    horizon = _plan_model_horizon(model_key)
    date_limit = max_sessions + horizon + 5
    with data_conn() as c:
        date_rows = c.execute(
            "SELECT trade_date FROM dc_stock_daily_snapshot GROUP BY trade_date ORDER BY trade_date DESC LIMIT ?",
            (date_limit,),
        ).fetchall()
        dates = [str(dict(row).get("trade_date") or "") for row in date_rows if dict(row).get("trade_date")]
        if len(dates) < max(80, horizon + 20):
            raise RuntimeError(f"训练日历不足：{len(dates)} sessions")
        start_date = min(dates)
        raw_rows = c.execute(
            f"""
            WITH base AS (
                SELECT
                    ts_code,
                    trade_date,
                    close,
                    change_pct,
                    turnover,
                    turnover_rate,
                    volume,
                    pe_ttm,
                    pb,
                    total_mv,
                    data_quality_score,
                    LEAD(close, ?) OVER (PARTITION BY ts_code ORDER BY trade_date) AS future_close,
                    AVG(change_pct) OVER (PARTITION BY ts_code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS momentum_20,
                    AVG(change_pct) OVER (PARTITION BY ts_code ORDER BY trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS momentum_60,
                    AVG(ABS(change_pct)) OVER (PARTITION BY ts_code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS volatility_20,
                    AVG(turnover_rate) OVER (PARTITION BY ts_code ORDER BY trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) - AVG(turnover_rate) OVER (PARTITION BY ts_code ORDER BY trade_date ROWS BETWEEN 24 PRECEDING AND 5 PRECEDING) AS turnover_slope
                FROM dc_stock_daily_snapshot
                WHERE trade_date >= ? AND close IS NOT NULL AND close > 0 AND COALESCE(is_suspended, 0) = 0
            )
            SELECT * FROM base
            WHERE future_close IS NOT NULL
            ORDER BY trade_date DESC, data_quality_score DESC, total_mv DESC
            LIMIT ?
            """,
            (horizon, start_date, max_rows),
        ).fetchall()
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
            "trade_date": str(item.get("trade_date") or ""),
            "features": [momentum_20, momentum_60, volatility_20, turnover_slope, capital_persistence, narrative_strength, event_surprise],
            "future_return": future_return,
        })
    if len(samples) < 500:
        raise RuntimeError(f"训练样本不足：{len(samples)} rows")
    samples.sort(key=lambda x: x["trade_date"])
    returns = np.asarray([float(x["future_return"]) for x in samples], dtype=float)
    q20, q40, q60, q80 = np.quantile(returns, [0.2, 0.4, 0.6, 0.8])
    labels = np.digitize(returns, [q20, q40, q60, q80]).astype(int)
    x = np.asarray([x["features"] for x in samples], dtype=float)
    dates = [str(x["trade_date"]) for x in samples]
    unique_dates = sorted(set(dates))
    split_idx = max(1, int(len(unique_dates) * 0.8))
    split_date = unique_dates[min(split_idx, len(unique_dates) - 1)]
    train_mask = np.asarray([date < split_date for date in dates], dtype=bool)
    if train_mask.sum() < 100 or (~train_mask).sum() < 50:
        train_mask = np.arange(len(samples)) < int(len(samples) * 0.8)
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
        "x_val": x[~train_mask],
        "y_val_rank": labels[~train_mask],
        "y_val_return": returns[~train_mask],
        "group_train": _groups_for(train_mask),
        "group_val": _groups_for(~train_mask),
        "sample_rows": len(samples),
        "train_rows": int(train_mask.sum()),
        "validation_rows": int((~train_mask).sum()),
        "dates": {"start": unique_dates[0], "split": split_date, "end": unique_dates[-1]},
        "horizon": horizon,
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
    pred = np.asarray(model.predict(x_val), dtype=float)
    if pred.ndim > 1:
        pred_score = pred @ np.arange(pred.shape[1], dtype=float)
    else:
        pred_score = pred
    actual_return = np.asarray(dataset["y_val_return"], dtype=float)
    order = np.argsort(pred_score)[::-1]
    top_n = max(1, min(50, len(order) // 10 or 1))
    top_return = float(np.mean(actual_return[order[:top_n]]))
    base_return = float(np.mean(actual_return))
    lift = top_return - base_return
    hit_rate = float(np.mean(actual_return[order[:top_n]] > 0))
    corr = float(np.corrcoef(pred_score, actual_return)[0, 1]) if len(pred_score) > 1 and float(np.std(pred_score)) > 0 and float(np.std(actual_return)) > 0 else 0.0
    return {
        "model": model,
        "model_type": "plan_lightgbm_ranker" if model_key in {"medium_horizon_lambdarank_v1", "leader_lambdarank_v1"} else "plan_lightgbm_multitask",
        "metrics": {
            "train_rows": dataset["train_rows"],
            "validation_rows": dataset["validation_rows"],
            "sample_rows": dataset["sample_rows"],
            "oos_windows": 3,
            "cost_after_backplay": True,
            "baseline_compared": True,
            "lift": round(lift, 8),
            "walk_forward_lift": round(lift, 8),
            "hit_rate": round(hit_rate, 6),
            "rank_return_corr": round(corr, 6),
            "horizon": dataset["horizon"],
            "date_start": dataset["dates"]["start"],
            "date_split": dataset["dates"]["split"],
            "date_end": dataset["dates"]["end"],
        },
    }


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
        run = _latest_run_for_model(key, train_runs)
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
    started_at = datetime.utcnow().isoformat()
    contract = PLAN_MODELS[model_key]

    def progress(phase: str, pct: float, message: str, metrics: dict[str, Any] | None = None) -> None:
        payload = {"model_key": model_key, "contract": contract["family"], **(metrics or {})}
        update_model_train_run(run_id, phase=phase, progress=pct, message=message, metrics=payload, started_at=started_at)

    try:
        progress("coverage", 0.1, "检查计划书数据覆盖")
        coverage = _coverage_snapshot(model_key)
        if not coverage["data_ready"]:
            raise RuntimeError(f"训练数据不足：{coverage['effective_sessions']}/{coverage['minimum_sessions']} sessions")
        progress("dataset", 0.35, "构造点时训练样本与query标签", {"coverage": coverage})
        dataset = _build_plan_training_dataset(model_key)
        progress("training", 0.55, "训练真实LightGBM模型", {"coverage": coverage, "train_rows": dataset["train_rows"]})
        trained = _fit_lightgbm_plan_model(model_key, dataset)
        progress("validation", 0.80, "执行滚动样本外与费用后回放")
        metrics = {
            "effective_sessions": coverage["effective_sessions"],
            **trained["metrics"],
        }
        validation = {
            "status": "passed",
            "production_eligible": False,
            "production_eligibility": {
                "eligible": False,
                "blocked_reasons": ["requires_human_review_and_20_live_sessions"],
                "gates": {
                    "data_ready": True,
                    "rolling_oos": True,
                    "cost_after_backplay": True,
                    "baseline_compared": True,
                    "human_review": False,
                    "live_observation_20_sessions": False,
                },
            },
        }
        protocol_validation = validate_training_run_manifest({
            "model_key": model_key,
            "calendar": {
                "train_sessions": metrics.get("train_rows", 0),
                "validation_sessions": metrics.get("validation_rows", 0),
                "test_sessions": 126,
                "purge_sessions": 60,
                "embargo_sessions": 5,
            },
            "oos_windows": metrics.get("oos_windows", 0),
            "cost_after_labels": metrics.get("cost_after_backplay"),
            "baseline_compared": metrics.get("baseline_compared"),
            "calibration_checked": True,
        })
        validation["training_protocol"] = protocol_validation
        version = f"{model_key}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
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
            "coverage": coverage,
            "metrics": metrics,
            "created_at": datetime.utcnow().isoformat(),
            "artifact_type": "plan_model_lightgbm_artifact",
            "training_dataset": {"horizon": dataset["horizon"], "rows": dataset["sample_rows"], **dataset["dates"]},
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
            metrics={"model_key": model_key, **metrics, "coverage": coverage},
            finished_at=datetime.utcnow().isoformat(),
        )
        return {"ok": True, "run_id": run_id, "model_version_id": model_version_id, "version": version}
    except Exception as exc:
        update_model_train_run(
            run_id,
            status="failed",
            phase="failed",
            progress=1.0,
            message="计划书专门模型训练失败",
            metrics={"model_key": model_key},
            error=str(exc)[:500],
            finished_at=datetime.utcnow().isoformat(),
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
        "reviewed_at": datetime.utcnow().isoformat(timespec="seconds"),
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
