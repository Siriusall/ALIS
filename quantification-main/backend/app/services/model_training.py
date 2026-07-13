from __future__ import annotations

import json
import math
import os
import pickle
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Callable

from app.core.exchange_groups import matches_exchange_group, normalize_exchange_group
from app.db.store import (
    create_model_train_run,
    create_model_version,
    set_active_model_version,
    update_model_train_run,
)
from app.services.backtest_data_source import BacktestDataSource, SnapshotRow
from app.services.backtest_engine import _factor_value
from app.services.model_registry import MODEL_KEY, models_root

FEATURES = [
    "momentum_5",
    "momentum_20",
    "volatility_20",
    "volume_ratio_5",
    "turnover",
    "float_mv",
    "total_mv",
    "pe_ttm",
    "pb",
    "change_pct",
]


def _safe_float(v: Any) -> float:
    try:
        x = float(v or 0)
        return x if math.isfinite(x) else 0.0
    except Exception:
        return 0.0


def _future_label(row: SnapshotRow, future_row: SnapshotRow | None) -> float | None:
    if row.close <= 0:
        return None
    future_close = _safe_float(getattr(future_row, "close", 0) if future_row else 0)
    if future_close <= 0:
        return None
    raw_return = future_close / row.close - 1.0
    # 风险调整标签：避免纯追涨，轻微惩罚当日高波动/极端涨跌。
    risk_penalty = min(abs(float(row.change_pct or 0)) / 100.0, 0.2) * 0.15
    return raw_return - risk_penalty


def _feature_row(ds: BacktestDataSource, row: SnapshotRow, date: str, daily_basic: dict[str, dict[str, float]]) -> list[float]:
    hist = ds.get_hist_window(row.symbol, date, 60)
    return [_safe_float(_factor_value(f, row, hist, daily_basic)) for f in FEATURES]


def _build_dataset(
    *,
    ds: BacktestDataSource,
    dates: list[str],
    horizon: int,
    exchange_group: str,
    progress: Callable[[str, float, str], None],
) -> tuple[list[list[float]], list[float], list[dict[str, Any]]]:
    x_rows: list[list[float]] = []
    y_rows: list[float] = []
    meta: list[dict[str, Any]] = []
    usable = dates[:-horizon] if horizon > 0 else dates
    total = max(1, len(usable))
    for idx, date in enumerate(usable):
        progress("building_dataset", 0.05 + 0.45 * (idx / total), f"构建样本 {idx + 1}/{total} {date}")
        future_date = dates[idx + horizon]
        future_map = {r.symbol: r for r in ds.load_snapshot(future_date.replace("-", ""))}
        snapshot = ds.load_snapshot(date.replace("-", ""))
        if exchange_group != "ALL":
            snapshot = [r for r in snapshot if matches_exchange_group(r.symbol, exchange_group)]
        daily_basic = ds.get_daily_basic_map(date.replace("-", ""))
        for row in snapshot:
            if row.volume <= 0 or row.close <= 0:
                continue
            label = _future_label(row, future_map.get(row.symbol))
            if label is None:
                continue
            x_rows.append(_feature_row(ds, row, date, daily_basic))
            y_rows.append(label)
            meta.append({"date": date, "symbol": row.symbol, "future_date": future_date})
    return x_rows, y_rows, meta


def _train_sklearn_model(x_train: list[list[float]], y_train: list[float], *, max_iter: int = 80, learning_rate: float = 0.06, max_leaf_nodes: int = 31):
    try:
        from sklearn.ensemble import HistGradientBoostingRegressor
    except Exception:
        return None
    model = HistGradientBoostingRegressor(
        max_iter=max_iter,
        learning_rate=learning_rate,
        max_leaf_nodes=max_leaf_nodes,
        l2_regularization=0.01,
        random_state=42,
    )
    model.fit(x_train, y_train)
    return model


def _train_lightgbm_model(x_train: list[list[float]], y_train: list[float]):
    """Use the fixed LightGBM cross-sectional model when the native runtime is available."""
    # macOS ARM wheels may block while resolving libomp.  Keep production
    # startup/training safe until the native runtime is explicitly validated.
    if str(os.getenv("ALIS_ENABLE_LIGHTGBM", "0")).lower() not in {"1", "true", "yes", "on"}:
        return None
    try:
        import lightgbm as lgb
    except Exception:
        return None
    model = lgb.LGBMRegressor(
        n_estimators=120,
        learning_rate=0.04,
        num_leaves=31,
        max_depth=-1,
        reg_lambda=0.1,
        random_state=42,
        verbosity=-1,
    )
    model.fit(x_train, y_train)
    return model


def _linear_weights(x_train: list[list[float]], y_train: list[float]) -> dict[str, float]:
    if not x_train or not y_train:
        return {f: 0.0 for f in FEATURES}
    y_mean = mean(y_train)
    weights: dict[str, float] = {}
    width = min(len(FEATURES), max((len(row) for row in x_train), default=0))
    for col, feature in enumerate(FEATURES[:width]):
        vals = [row[col] for row in x_train]
        x_mean = mean(vals)
        cov = sum((x - x_mean) * (y - y_mean) for x, y in zip(vals, y_train))
        var = sum((x - x_mean) ** 2 for x in vals) or 1.0
        weights[feature] = cov / var
    scale = sum(abs(v) for v in weights.values()) or 1.0
    return {k: round(v / scale, 8) for k, v in weights.items()}


def _predict_linear(x_rows: list[list[float]], weights: dict[str, float]) -> list[float]:
    return [
        sum(float(weights.get(FEATURES[i], 0) or 0) * _safe_float(v) for i, v in enumerate(row))
        for row in x_rows
    ]


def _eval_predictions(pred: list[float], y_true: list[float], top_ratio: float = 0.1) -> dict[str, float]:
    if not pred or not y_true:
        return {"top_mean_label": 0.0, "all_mean_label": 0.0, "lift": 0.0, "hit_rate": 0.0}
    pairs = sorted(zip(pred, y_true), key=lambda x: x[0], reverse=True)
    k = max(1, int(len(pairs) * top_ratio))
    top = [y for _, y in pairs[:k]]
    all_mean = mean(y_true)
    top_mean = mean(top)
    return {
        "top_mean_label": round(top_mean, 6),
        "all_mean_label": round(all_mean, 6),
        "lift": round(top_mean - all_mean, 6),
        "hit_rate": round(sum(1 for y in top if y > 0) / len(top), 4),
    }


def _walk_forward_lift(x_rows: list[list[float]], y_rows: list[float], model_type: str, folds: int = 3) -> dict[str, float]:
    """Expanding-window walk-forward validation used for promotion evidence."""
    n = len(x_rows)
    if n < 60:
        return {"walk_forward_folds": 0, "walk_forward_lift": 0.0, "walk_forward_hit_rate": 0.0}
    lifts: list[float] = []
    hits: list[float] = []
    for fold in range(1, folds + 1):
        train_end = int(n * (0.45 + 0.12 * fold))
        val_end = int(n * (0.45 + 0.12 * (fold + 1))) if fold < folds else n
        if train_end < 30 or val_end <= train_end:
            continue
        model = _train_lightgbm_model(x_rows[:train_end], y_rows[:train_end]) if model_type == "lightgbm" else _train_sklearn_model(x_rows[:train_end], y_rows[:train_end])
        if model is not None:
            pred = [float(x) for x in model.predict(x_rows[train_end:val_end]).tolist()]
        else:
            pred = _predict_linear(x_rows[train_end:val_end], _linear_weights(x_rows[:train_end], y_rows[:train_end]))
        metrics = _eval_predictions(pred, y_rows[train_end:val_end])
        lifts.append(float(metrics.get("lift", 0.0)))
        hits.append(float(metrics.get("hit_rate", 0.0)))
    return {
        "walk_forward_folds": len(lifts),
        "walk_forward_lift": round(sum(lifts) / len(lifts), 6) if lifts else 0.0,
        "walk_forward_hit_rate": round(sum(hits) / len(hits), 4) if hits else 0.0,
    }


def run_model_training(
    *,
    run_id: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    exchange_group: str = "HS",
) -> dict[str, Any]:
    rid = run_id or create_model_train_run("开始模型训练")
    started_at = datetime.utcnow().isoformat()

    def progress(phase: str, pct: float, msg: str) -> None:
        update_model_train_run(
            rid,
            phase=phase,
            progress=max(0.0, min(1.0, float(pct))),
            message=msg,
            started_at=started_at,
        )

    try:
        ds = BacktestDataSource()
        exchange_group = normalize_exchange_group(exchange_group)
        dates = ds.get_available_trade_dates(start_date or "2020-01-01", end_date or "2099-12-31")
        if len(dates) < 15:
            raise ValueError("可用交易日不足，至少需要 15 个交易日训练模型")

        horizons = [5, 10, 20]
        horizons = [h for h in horizons if len(dates) > h + 5] or [min(5, len(dates) - 2)]
        best: dict[str, Any] | None = None

        for h_idx, horizon in enumerate(horizons):
            progress("dataset", 0.05 + 0.1 * h_idx, f"准备 {horizon} 日标签")
            x_rows, y_rows, meta = _build_dataset(
                ds=ds,
                dates=dates,
                horizon=horizon,
                exchange_group=exchange_group,
                progress=progress,
            )
            if len(x_rows) < 200:
                continue
            split = max(1, int(len(x_rows) * 0.8))
            x_train, y_train = x_rows[:split], y_rows[:split]
            x_val, y_val = x_rows[split:], y_rows[split:]
            progress("training", 0.55, f"训练机器学习 ranking 模型 horizon={horizon}")
            # Small fixed grid: enough to tune bias/variance without turning
            # the training job into an unbounded search.
            grid = [(80, 0.06, 31), (120, 0.04, 31), (60, 0.08, 15)]
            candidate_models: list[tuple[Any, str, tuple[int, float, int]]] = []
            lightgbm_model = _train_lightgbm_model(x_train, y_train)
            if lightgbm_model is not None:
                candidate_models.append((lightgbm_model, "lightgbm", (120, 0.04, 31)))
            for params in grid:
                candidate = _train_sklearn_model(x_train, y_train, max_iter=params[0], learning_rate=params[1], max_leaf_nodes=params[2])
                if candidate is not None:
                    candidate_models.append((candidate, "sklearn_hgbr", params))
            if not candidate_models:
                candidate_models.append((None, "linear_fallback", (0, 0.0, 0)))
            model, model_type, model_params = candidate_models[0]
            best_candidate_score = float("-inf")
            for candidate, candidate_type, candidate_params in candidate_models:
                candidate_pred = [float(x) for x in candidate.predict(x_val).tolist()] if candidate is not None else _predict_linear(x_val, _linear_weights(x_train, y_train))
                candidate_metrics = _eval_predictions(candidate_pred, y_val)
                candidate_score = float(candidate_metrics.get("lift", 0.0)) + float(candidate_metrics.get("hit_rate", 0.0)) * 0.01
                if candidate_score > best_candidate_score:
                    model, model_type, model_params = candidate, candidate_type, candidate_params
                    best_candidate_score = candidate_score
            artifact: dict[str, Any] = {
                "model_key": MODEL_KEY,
                "features": FEATURES,
                "horizon": horizon,
                "exchange_group": exchange_group,
                "train_rows": len(x_train),
                "validation_rows": len(x_val),
                "created_at": datetime.utcnow().isoformat(),
                "tuning_grid": grid,
                "selected_params": model_params,
            }
            artifact_files: dict[str, str] = {}
            if model is not None:
                pred = [float(x) for x in model.predict(x_val).tolist()] if x_val else []
                artifact["model_type"] = model_type
            else:
                weights = _linear_weights(x_train, y_train)
                pred = _predict_linear(x_val, weights)
                artifact["model_type"] = model_type
                artifact["weights"] = weights
            metrics = _eval_predictions(pred, y_val)
            metrics.update({"horizon": horizon, "train_rows": len(x_train), "validation_rows": len(x_val)})
            metrics.update(_walk_forward_lift(x_rows, y_rows, model_type))
            score = float(metrics.get("lift", 0)) + float(metrics.get("hit_rate", 0)) * 0.01
            if best is None or score > float(best["score"]):
                best = {
                    "horizon": horizon,
                    "artifact": artifact,
                    "metrics": metrics,
                    "score": score,
                    "model": model,
                    "artifact_files": artifact_files,
                    "sample_meta": meta[-5:],
                }

        if best is None:
            raise ValueError("训练样本不足，无法生成模型版本")

        version = datetime.utcnow().strftime("v%Y%m%d%H%M%S")
        root = models_root() / version
        root.mkdir(parents=True, exist_ok=True)
        artifact = dict(best["artifact"])
        if best.get("model") is not None:
            model_path = root / "model.pkl"
            with model_path.open("wb") as f:
                pickle.dump(best["model"], f)
            artifact["model_path"] = str(model_path)
        artifact["metrics"] = best["metrics"]
        artifact["sample_meta"] = best["sample_meta"]
        artifact_path = root / "artifact.json"
        artifact_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")

        validation = {
            "auto_score": round(float(best["score"]), 6),
            "status": "passed" if float(best["metrics"].get("lift", 0)) > 0 and float(best["metrics"].get("walk_forward_lift", 0)) > 0 else "watch",
            "method": "holdout_plus_expanding_walk_forward",
            "walk_forward_folds": int(best["metrics"].get("walk_forward_folds", 0) or 0),
        }
        model_version_id = create_model_version(
            model_key=MODEL_KEY,
            version=version,
            status="active" if validation["status"] == "passed" else "candidate",
            artifact_path=str(artifact_path),
            config={
                "features": FEATURES,
                "horizon": best["horizon"],
                "exchange_group": exchange_group,
                "model_type": artifact.get("model_type"),
            },
            metrics=best["metrics"],
            validation=validation,
            is_active=validation["status"] == "passed",
        )
        if validation["status"] == "passed":
            set_active_model_version(model_version_id)
        update_model_train_run(
            rid,
            status="completed",
            phase="completed",
            progress=1.0,
            message=f"训练完成，生成模型版本 {version}",
            model_version_id=model_version_id,
            metrics=best["metrics"],
            finished_at=datetime.utcnow().isoformat(),
        )
        return {"ok": True, "run_id": rid, "model_version_id": model_version_id, "version": version, "metrics": best["metrics"]}
    except Exception as e:
        update_model_train_run(
            rid,
            status="failed",
            phase="failed",
            progress=1.0,
            message="训练失败",
            error=str(e),
            finished_at=datetime.utcnow().isoformat(),
        )
        raise
