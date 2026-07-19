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
    update_model_train_run,
)
from app.services.backtest_data_source import BacktestDataSource, SnapshotRow
from app.services.backtest_engine import _factor_value
from app.services.model_registry import MODEL_KEY, models_root
from app.services.streaming_training import build_training_slices, consume_api_training_slice

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


def _require_compact_api_training_history(refresh: dict[str, Any]) -> dict[str, Any]:
    """Validate the plan's on-demand 6-10 year training data contract.

    Broad indices are refreshed from the API while the persisted full-market
    daily history is aggregated locally into stable regime observations.
    """
    sources = list(refresh.get("sources") or [])
    source_ok = bool(sources) and all(bool(item.get("ok")) for item in sources if isinstance(item, dict))
    materialized = refresh.get("materialized") or {}
    written = int(materialized.get("written") or 0)
    if not bool(refresh.get("ok")) or not source_ok or written <= 0:
        raise RuntimeError("训练所需的6-10年紧凑市场特征API刷新失败；未使用本地旧数据替代训练")
    return {
        "contract": str(refresh.get("contract") or "full_market_daily_aggregates_plus_broad_index_anchors_v2"),
        "range": list(refresh.get("range") or []),
        "sources": [{"ts_code": item.get("ts_code"), "rows_written": item.get("rows_written")} for item in sources if isinstance(item, dict)],
        "features_written": written,
        "full_market_history_persisted": True,
    }


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


def _build_multi_horizon_dataset(
    *,
    ds: BacktestDataSource,
    dates: list[str],
    horizons: list[int],
    exchange_group: str,
    progress: Callable[[float, str], None],
) -> dict[int, tuple[list[list[float]], list[float], list[dict[str, Any]]]]:
    """Build all label horizons in one pass so expensive features are computed once."""
    result = {h: ([], [], []) for h in horizons}
    if not dates or not horizons:
        return result
    total = max(1, len(dates) - min(horizons))
    for idx, date in enumerate(dates):
        valid_horizons = [h for h in horizons if idx + h < len(dates)]
        if not valid_horizons:
            break
        progress(idx / total, f"处理交易日 {idx + 1}/{total} · {date}")
        snapshot = ds.load_snapshot(date.replace("-", ""))
        if exchange_group != "ALL":
            snapshot = [row for row in snapshot if matches_exchange_group(row.symbol, exchange_group)]
        usable_rows = [row for row in snapshot if row.volume > 0 and row.close > 0]
        if not usable_rows:
            continue
        # A single datasource shares snapshot, daily-basic and history caches
        # across every horizon. Feature rows are horizon-independent.
        daily_basic = ds.get_daily_basic_map(date.replace("-", ""))
        feature_by_symbol = {row.symbol: _feature_row(ds, row, date, daily_basic) for row in usable_rows}
        for horizon in valid_horizons:
            future_date = dates[idx + horizon]
            future_map = {row.symbol: row for row in ds.load_snapshot(future_date.replace("-", ""))}
            x_rows, y_rows, meta = result[horizon]
            for row in usable_rows:
                label = _future_label(row, future_map.get(row.symbol))
                if label is None:
                    continue
                x_rows.append(feature_by_symbol[row.symbol])
                y_rows.append(label)
                meta.append({"date": date, "symbol": row.symbol, "future_date": future_date})
    progress(1.0, f"本地样本构建完成 · {len(dates)} 个交易日")
    return result


def _compact_index_trade_dates(*, start_date: str, end_date: str) -> list[str]:
    """Read the small, training-approved index calendar, never stock history."""
    from app.data_platform.db import _conn as data_conn

    with data_conn() as conn:
        rows = conn.execute(
            """SELECT DISTINCT trade_date FROM dc_tushare_raw_records
               WHERE api_name='index_daily' AND ts_code='000300.SH'
                 AND trade_date BETWEEN ? AND ? ORDER BY trade_date""",
            (start_date.replace("-", ""), end_date.replace("-", "")),
        ).fetchall()
    return [str(dict(row).get("trade_date") or "") for row in rows if str(dict(row).get("trade_date") or "")]


def _stream_historical_datasets(
    *,
    historical_dates: list[str],
    label_bridge_dates: list[str],
    online_window_start: str,
    horizons: list[int],
    exchange_group: str,
    progress: Callable[[str, float, str], None],
) -> dict[int, tuple[list[list[float]], list[float], list[dict[str, Any]]]]:
    """Build old samples slice-by-slice without retaining old market rows.

    Each slice reads daily/daily_basic/stk_limit from PostgreSQL, materializes
    missing derived snapshots locally and emits feature/label rows. Network
    market-data calls are forbidden in this training path.
    """
    result = {h: ([], [], []) for h in horizons}
    if not historical_dates:
        return result
    slices = build_training_slices(historical_dates, chunk_sessions=60, lookback_sessions=80, label_horizon=max(horizons))
    # The final old dates need future closes for their labels.  The first
    # current-window sessions are read-only bridge data and are protected by
    # the cleanup floor below.
    if slices and label_bridge_dates:
        last = slices[-1]
        slices[-1] = type(last)(
            core_dates=last.core_dates,
            fetch_dates=tuple(sorted(set(last.fetch_dates + tuple(label_bridge_dates[:max(horizons)])))),
        )
    for index, training_slice in enumerate(slices):
        progress("local_training_data", 0.12 + 0.30 * (index / max(1, len(slices))), f"本地训练切片 {index + 1}/{len(slices)} · 准备数据")

        def consume(core_dates: tuple[str, ...]) -> None:
            core = set(core_dates)
            slice_dates = [f"{date[:4]}-{date[4:6]}-{date[6:8]}" for date in training_slice.fetch_dates]
            chunk_ds = BacktestDataSource()
            datasets = _build_multi_horizon_dataset(
                ds=chunk_ds, dates=slice_dates, horizons=horizons,
                exchange_group=exchange_group,
                progress=lambda fraction, message: progress(
                    "local_training_data",
                    0.12 + 0.30 * ((index + fraction) / max(1, len(slices))),
                    f"本地训练切片 {index + 1}/{len(slices)} · {message}",
                ),
            )
            for horizon in horizons:
                x_rows, y_rows, meta = datasets[horizon]
                kept = [(x, y, item) for x, y, item in zip(x_rows, y_rows, meta) if item["date"].replace("-", "") in core]
                if kept:
                    xs, ys, items = zip(*kept)
                    result[horizon][0].extend(xs)
                    result[horizon][1].extend(ys)
                    result[horizon][2].extend(items)

        consume_api_training_slice(
            training_slice,
            online_window_start=online_window_start,
            consume=consume,
            retain_history=True,
        )
    return result


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


def _production_eligibility(*, dates: list[str], metrics: dict[str, Any], artifact_path: Path) -> dict[str, Any]:
    sessions = len(dates)
    gates = {
        "artifact_exists": artifact_path.exists() and artifact_path.is_file(),
        "minimum_history_sessions": sessions >= 252 * 6,
        "holdout_lift_positive": float(metrics.get("lift", 0) or 0) > 0,
        "walk_forward_lift_positive": float(metrics.get("walk_forward_lift", 0) or 0) > 0,
        "walk_forward_folds": int(metrics.get("walk_forward_folds", 0) or 0) >= 3,
    }
    reasons = [name for name, passed in gates.items() if not passed]
    return {
        "eligible": not reasons,
        "gates": gates,
        "blocked_reasons": reasons,
        "history_sessions": sessions,
        "minimum_sessions_required": 252 * 6,
        "policy": "candidate_only_until_plan_gates_pass",
    }


def _model_selection_rank(metrics: dict[str, Any], score: float) -> tuple[int, float, float, float]:
    """Prefer models that can satisfy production gates before secondary score."""
    lift = float(metrics.get("lift", 0) or 0)
    walk_forward_lift = float(metrics.get("walk_forward_lift", 0) or 0)
    folds = int(metrics.get("walk_forward_folds", 0) or 0)
    eligible_metrics = lift > 0 and walk_forward_lift > 0 and folds >= 3
    return (1 if eligible_metrics else 0, float(score), lift, walk_forward_lift)


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
        # User-approved boundary: only a model-training trigger can request
        # 6-10 years, using only persisted PostgreSQL full-market aggregates
        # and broad-index anchors. This path never calls the market-data API.
        from app.services.hsmm_shadow import refresh_market_regime_training_history
        progress("local_regime_features", 0.01, "校验 PostgreSQL 本地市场状态训练数据")
        training_input = _require_compact_api_training_history(refresh_market_regime_training_history(years=10))
        ds = BacktestDataSource()
        exchange_group = normalize_exchange_group(exchange_group)
        dates = ds.get_available_trade_dates(start_date or "2020-01-01", end_date or "2099-12-31")
        if len(dates) < 15:
            raise ValueError("可用交易日不足，至少需要 15 个交易日训练模型")

        horizons = [5, 10, 20]
        horizons = [h for h in horizons if len(dates) > h + 5] or [min(5, len(dates) - 2)]
        # The online product keeps its normal rolling window.  For a training
        # click, extend only the *calendar* from the broad-index anchors and
        # stream any missing old stock slices one at a time.  No old stock raw
        # table or snapshot survives a slice.
        online_window_start = dates[0].replace("-", "")
        historical_candidates = _compact_index_trade_dates(start_date="2016-01-01", end_date=dates[-1])
        target_sessions = 252 * 6
        historical_dates = [date for date in historical_candidates if date < online_window_start]
        training_calendar = (historical_dates + [date.replace("-", "") for date in dates])[-target_sessions:]
        streamed_dates = [date for date in training_calendar if date < online_window_start]
        streamed = _stream_historical_datasets(
            historical_dates=streamed_dates,
            label_bridge_dates=[date.replace("-", "") for date in dates],
            online_window_start=online_window_start,
            horizons=horizons,
            exchange_group=exchange_group,
            progress=progress,
        )
        training_input["cross_sectional_history"] = {
            "contract": "postgresql_local_full_market_history_retained_v2",
            "online_window_sessions": len(dates),
            "target_sessions": target_sessions,
            "training_calendar_sessions": len(training_calendar),
            "streamed_sessions": len(streamed_dates),
            "chunk_sessions": 60,
            "raw_full_market_history_persisted": True,
            "latest_trade_date": training_calendar[-1] if training_calendar else "",
        }
        best: dict[str, Any] | None = None

        for h_idx, horizon in enumerate(horizons):
            progress("dataset", 0.05 + 0.1 * h_idx, f"准备 {horizon} 日标签")
            local_x, local_y, local_meta = _build_dataset(
                ds=ds,
                dates=dates,
                horizon=horizon,
                exchange_group=exchange_group,
                progress=progress,
            )
            x_rows = streamed[horizon][0] + local_x
            y_rows = streamed[horizon][1] + local_y
            meta = streamed[horizon][2] + local_meta
            from app.services.review_learning import training_feedback_rows
            review_feedback = training_feedback_rows(horizon)
            feedback_added = 0
            for feedback in review_feedback:
                feedback_date = str(feedback.get("entry_date") or "")
                snapshot = ds.load_snapshot(feedback_date)
                symbol = str(feedback.get("ts_code") or "").split(".")[0]
                source_row = next((row for row in snapshot if str(row.symbol).split(".")[0] == symbol), None)
                if source_row is None:
                    continue
                daily_basic = ds.get_daily_basic_map(feedback_date)
                directional = float(feedback.get("directional_return") or 0)
                reviewed_label = float(feedback.get("label") or 0)
                target = directional if reviewed_label > 0 else -abs(directional)
                repeats = max(1, min(3, round(float(feedback.get("sample_weight") or 1))))
                for _ in range(repeats):
                    x_rows.append(_feature_row(ds, source_row, feedback_date, daily_basic))
                    y_rows.append(target)
                    meta.append({"date": feedback_date, "symbol": symbol, "source": "human_review_feedback"})
                    feedback_added += 1
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
                "review_feedback_rows": feedback_added,
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
            rank = _model_selection_rank(metrics, score)
            if best is None or rank > best["rank"]:
                best = {
                    "horizon": horizon,
                    "artifact": artifact,
                    "metrics": metrics,
                    "score": score,
                    "rank": rank,
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
        eligibility = _production_eligibility(dates=training_calendar, metrics=best["metrics"], artifact_path=artifact_path)
        validation["production_eligible"] = bool(eligibility["eligible"])
        validation["production_eligibility"] = eligibility
        model_version_id = create_model_version(
            model_key=MODEL_KEY,
            version=version,
            status="candidate" if validation["status"] == "passed" else "watch",
            artifact_path=str(artifact_path),
            config={
                "features": FEATURES,
                "horizon": best["horizon"],
                "exchange_group": exchange_group,
                "model_type": artifact.get("model_type"),
                "production_eligible": bool(eligibility["eligible"]),
                "training_input": training_input,
            },
            metrics=best["metrics"],
            validation=validation,
            is_active=False,
        )
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
        return {"ok": True, "run_id": rid, "model_version_id": model_version_id, "version": version, "metrics": best["metrics"], "training_input": training_input}
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
