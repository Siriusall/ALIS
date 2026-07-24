from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from scipy.special import gammaln
from scipy.stats import multivariate_t

from app.data_platform.db import _conn as data_conn
from app.db.store import create_model_version


def refresh_market_regime_training_history(*, years: int = 10) -> dict[str, Any]:
    """Rebuild training features strictly from locally stored PostgreSQL history."""
    from datetime import date, timedelta
    from app.services.market_context import materialize_market_training_history

    end = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=max(6, min(int(years), 10)) * 366)).strftime("%Y%m%d")
    results = []
    with data_conn() as c:
        for code in ("000300.SH", "000905.SH", "000852.SH"):
            row = c.execute(
                """SELECT COUNT(*) count,MIN(trade_date) first_date,MAX(trade_date) last_date
                   FROM dc_tushare_raw_records WHERE api_name='index_daily' AND ts_code=?
                     AND trade_date BETWEEN ? AND ?""", (code, start, end),
            ).fetchone()
            item = dict(row)
            count = int(item.get("count") or 0)
            results.append({"ts_code": code, "ok": count > 0, "rows": count, "first_date": item.get("first_date"), "last_date": item.get("last_date"), "source": "postgresql_local", "error": "" if count else "local_index_history_missing"})
    materialized = materialize_market_training_history()
    return {
        "ok": bool(materialized.get("ok")) and all(item["ok"] for item in results),
        "range": [start, end], "sources": results, "materialized": materialized,
        "contract": "postgresql_local_full_market_plus_broad_index_anchors_v3",
    }


def refresh_compact_training_history_from_api(*, years: int = 10) -> dict[str, Any]:
    """Refresh the compact index anchors used by older HSMM callers."""
    from datetime import date, timedelta
    from app.data_platform.services.data_center import sync_tushare_api_raw
    from app.services.market_context import materialize_market_training_history

    end = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=max(1, min(int(years), 10)) * 366)).strftime("%Y%m%d")
    results = [
        {
            "ts_code": code,
            **sync_tushare_api_raw("index_daily", {"ts_code": code, "start_date": start, "end_date": end}),
        }
        for code in ("000300.SH", "000905.SH", "000852.SH")
    ]
    materialized = materialize_market_training_history()
    return {
        "ok": all(int(item.get("rows_written") or 0) > 0 for item in results) and bool(materialized.get("ok", True)),
        "range": [start, end],
        "sources": results,
        "materialized": materialized,
        "contract": "compact_index_anchors_v1",
    }


STATES = ("S0", "S1", "S2", "S3")
FEATURES = (
    "000300_return_20d", "000300_return_60d", "000300_realized_vol_20d",
    "000905_return_20d", "000905_return_60d", "000905_realized_vol_20d",
    "000852_return_20d", "000852_return_60d", "000852_realized_vol_20d",
    "market_breadth", "market_average_return", "market_return_dispersion",
    "market_limit_up_ratio", "market_limit_down_ratio", "market_limit_balance",
    "market_amount_ratio_20d",
)


def _load_weekly_features() -> tuple[list[str], np.ndarray]:
    with data_conn() as c:
        rows = c.execute(
            """SELECT feature_date,features_json FROM model_training_history
               WHERE feature_set='market_regime_full_market_v2' ORDER BY feature_date"""
        ).fetchall()
    weekly: dict[tuple[int, int], tuple[str, dict[str, Any]]] = {}
    for row in rows:
        item = dict(row)
        try:
            dt = datetime.strptime(str(item["feature_date"]), "%Y%m%d")
            payload = json.loads(item.get("features_json") or "{}")
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        if all(payload.get(feature) is not None for feature in FEATURES):
            iso = dt.isocalendar()
            weekly[(iso.year, iso.week)] = (str(item["feature_date"]), payload)
    values = list(weekly.values())
    dates = [item[0] for item in values]
    matrix = np.asarray([[float(item[1][feature]) for feature in FEATURES] for item in values], dtype=float)
    return dates, matrix


def _weekly_benchmark_returns(dates: list[str]) -> np.ndarray:
    if not dates:
        return np.asarray([], dtype=float)
    with data_conn() as c:
        rows = c.execute(
            """SELECT trade_date,payload_json FROM dc_tushare_raw_records
               WHERE api_name='index_daily' AND ts_code='000300.SH'
                 AND trade_date>=? AND trade_date<=? ORDER BY trade_date""",
            (dates[0], dates[-1]),
        ).fetchall()
    closes: dict[str, float] = {}
    for row in rows:
        item = dict(row)
        try:
            closes[str(item["trade_date"])] = float(json.loads(item.get("payload_json") or "{}")["close"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    selected = [closes.get(date) for date in dates]
    returns = [0.0]
    for previous, current in zip(selected[:-1], selected[1:]):
        returns.append(float(current / previous - 1.0) if previous and current else 0.0)
    return np.asarray(returns, dtype=float)


def _robust_scale(train: np.ndarray, values: np.ndarray) -> tuple[np.ndarray, dict[str, list[float]]]:
    center = np.median(train, axis=0)
    mad = np.median(np.abs(train - center), axis=0)
    scale = np.where(mad > 1e-9, 1.4826 * mad, np.std(train, axis=0) + 1e-6)
    return np.clip((values - center) / scale, -8, 8), {"center": center.tolist(), "scale": scale.tolist()}


def _semantic_seed(values: np.ndarray) -> np.ndarray:
    returns20 = np.mean(values[:, [0, 3, 6]], axis=1)
    returns60 = np.mean(values[:, [1, 4, 7]], axis=1)
    volatility = np.mean(values[:, [2, 5, 8]], axis=1)
    has_market_cross_section = values.shape[1] >= len(FEATURES)
    breadth = values[:, FEATURES.index("market_breadth")] if has_market_cross_section else np.zeros(len(values))
    market_return = values[:, FEATURES.index("market_average_return")] if has_market_cross_section else np.zeros(len(values))
    dispersion = values[:, FEATURES.index("market_return_dispersion")] if has_market_cross_section else volatility
    limit_balance = values[:, FEATURES.index("market_limit_balance")] if has_market_cross_section else np.zeros(len(values))
    labels = np.full(len(values), 1, dtype=int)
    labels[(returns60 > .25) & (volatility < .25) & (breadth > -.20)] = 0
    labels[(returns60 < -.25) | ((returns20 < -.20) & (volatility > .25)) | ((market_return < -.35) & (breadth < -.35))] = 2
    labels[(returns20 > .10) & ((volatility > .40) | (dispersion > .40)) & (returns60 <= .25) & (limit_balance > -.35)] = 3
    return labels


def _duration_parameters(labels: np.ndarray) -> list[dict[str, float]]:
    runs: dict[int, list[int]] = {index: [] for index in range(4)}
    start = 0
    for index in range(1, len(labels) + 1):
        if index == len(labels) or labels[index] != labels[start]:
            runs[int(labels[start])].append(max(2, min(52, index - start)))
            start = index
    output = []
    for state in range(4):
        durations = runs[state] or [4]
        mean = float(np.mean(durations))
        variance = max(mean + 1e-3, float(np.var(durations, ddof=1)) if len(durations) > 1 else mean * 1.5)
        p = max(.02, min(.98, mean / variance))
        r = max(.1, mean * p / max(1e-6, 1 - p))
        output.append({"r": r, "p": p, "mean": mean})
    return output


def _duration_logpmf(duration: int, params: dict[str, float]) -> float:
    k = max(0, duration - 2)
    r, p = params["r"], params["p"]
    raw = gammaln(k + r) - gammaln(r) - gammaln(k + 1) + r * math.log(p) + k * math.log(1 - p)
    normalizer = sum(math.exp(gammaln(d - 2 + r) - gammaln(r) - gammaln(d - 1) + r * math.log(p) + (d - 2) * math.log(1 - p)) for d in range(2, 53))
    return float(raw - math.log(max(normalizer, 1e-300)))


def _fit(values: np.ndarray) -> dict[str, Any]:
    labels = _semantic_seed(values)
    emissions = []
    for state in range(4):
        subset = values[labels == state]
        if len(subset) < 4:
            subset = values
        mean = np.median(subset, axis=0)
        centered = subset - mean
        covariance = np.cov(centered, rowvar=False) if len(subset) > 1 else np.eye(values.shape[1])
        covariance = np.asarray(covariance) + np.eye(values.shape[1]) * .15
        emissions.append({"mean": mean.tolist(), "covariance": covariance.tolist(), "df": 6.0})
    counts = np.ones((4, 4), dtype=float) * .25
    np.fill_diagonal(counts, 0.0)
    for left, right in zip(labels[:-1], labels[1:]):
        if left != right:
            counts[int(left), int(right)] += 1
    transitions = counts / np.maximum(counts.sum(axis=1, keepdims=True), 1e-12)
    start = np.bincount(labels[: min(52, len(labels))], minlength=4).astype(float) + 1
    start /= start.sum()
    return {"emissions": emissions, "durations": _duration_parameters(labels), "transitions": transitions.tolist(), "start": start.tolist()}


def _decode(values: np.ndarray, model: dict[str, Any]) -> tuple[list[int], float]:
    n, state_count = len(values), 4
    emission = np.zeros((n, state_count))
    for state, params in enumerate(model["emissions"]):
        emission[:, state] = multivariate_t.logpdf(values, loc=np.asarray(params["mean"]), shape=np.asarray(params["covariance"]), df=params["df"])
    cumulative = np.vstack([np.zeros(state_count), np.cumsum(emission, axis=0)])
    score = np.full((n + 1, state_count), -np.inf)
    back: list[list[tuple[int, int] | None]] = [[None] * state_count for _ in range(n + 1)]
    transitions = np.log(np.maximum(np.asarray(model["transitions"]), 1e-300))
    starts = np.log(np.maximum(np.asarray(model["start"]), 1e-300))
    duration_logs = [[-np.inf, -np.inf] + [_duration_logpmf(duration, model["durations"][state]) for duration in range(2, 53)] for state in range(state_count)]
    for end in range(2, n + 1):
        for state in range(state_count):
            for duration in range(2, min(52, end) + 1):
                begin = end - duration
                segment = cumulative[end, state] - cumulative[begin, state] + duration_logs[state][duration]
                if begin == 0:
                    candidate, previous = starts[state] + segment, -1
                else:
                    candidates = score[begin] + transitions[:, state]
                    candidates[state] = -np.inf
                    previous = int(np.argmax(candidates))
                    candidate = float(candidates[previous] + segment)
                if candidate > score[end, state]:
                    score[end, state], back[end][state] = candidate, (begin, previous)
    state = int(np.argmax(score[n]))
    end = n
    decoded = [1] * n
    while end > 0 and back[end][state] is not None:
        begin, previous = back[end][state]  # type: ignore[misc]
        decoded[begin:end] = [state] * (end - begin)
        end, state = begin, previous
        if state < 0:
            break
    return decoded, float(np.max(score[n]))


def _max_drawdown(returns: np.ndarray) -> float:
    curve = np.cumprod(1 + returns)
    peak = np.maximum.accumulate(curve)
    return float(np.min(curve / peak - 1)) if len(curve) else 0.0


def train_hsmm_shadow(*, refresh_api: bool = True) -> dict[str, Any]:
    from app.services.market_context import MARKET_PROFILE_LABELS

    api_refresh = refresh_market_regime_training_history() if refresh_api else {"status": "skipped"}
    dates, raw = _load_weekly_features()
    if len(raw) < 52 * 6:
        return {"ok": False, "status": "insufficient_history", "observations": len(raw), "minimum": 52 * 6, "api_refresh": api_refresh}
    split = int(len(raw) * .70)
    train_scaled, scaler = _robust_scale(raw[:split], raw[:split])
    test_scaled, _ = _robust_scale(raw[:split], raw[split:])
    model = _fit(train_scaled)
    decoded, log_likelihood = _decode(test_scaled, model)
    states = np.asarray(decoded)
    weekly_returns = _weekly_benchmark_returns(dates)[split:]
    multipliers = np.asarray([1.0, .85, .55, .70])[states]
    baseline_dd = _max_drawdown(weekly_returns)
    gated_dd = _max_drawdown(weekly_returns * multipliers)
    worst = weekly_returns <= np.quantile(weekly_returns, .10)
    stress_recall = float(np.mean(np.isin(states[worst], [2, 3]))) if np.any(worst) else 0.0
    return_ranges = [float(np.mean(test_scaled[states == state][:, [0, 1, 3, 4, 6, 7]])) if np.any(states == state) else 0 for state in range(4)]
    vol_ranges = [float(np.mean(test_scaled[states == state][:, [2, 5, 8]])) if np.any(states == state) else 0 for state in range(4)]
    breadth_index = FEATURES.index("market_breadth")
    stress_indices = [FEATURES.index("market_return_dispersion"), FEATURES.index("market_limit_balance")]
    breadth_ranges = [float(np.mean(test_scaled[states == state][:, breadth_index])) if np.any(states == state) else 0 for state in range(4)]
    stress_ranges = [float(np.mean(test_scaled[states == state][:, stress_indices])) if np.any(states == state) else 0 for state in range(4)]
    differentiation = (
        int((max(return_ranges) - min(return_ranges) > .35))
        + int((max(vol_ranges) - min(vol_ranges) > .35))
        + int((max(breadth_ranges) - min(breadth_ranges) > .35))
        + int((max(stress_ranges) - min(stress_ranges) > .35))
    )
    validation = {
        "status": "passed" if differentiation >= 2 and gated_dd >= baseline_dd and stress_recall >= .40 else "shadow_failed",
        "oos_start": dates[split], "oos_end": dates[-1], "oos_observations": len(states),
        "baseline_max_drawdown": round(baseline_dd, 6), "gated_max_drawdown": round(gated_dd, 6),
        "drawdown_improvement": round(gated_dd - baseline_dd, 6), "stress_recall": round(stress_recall, 4),
        "stable_distinguishing_dimensions": differentiation,
    }
    version = f"hsmm-shadow-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    artifact_dir = Path(__file__).resolve().parents[2] / ".cache" / "models" / "market_regime"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{version}.json"
    profile_layer = {
        "method": "hierarchical_market_profile_v1",
        "count": len(MARKET_PROFILE_LABELS),
        "labels": MARKET_PROFILE_LABELS,
        "parent_states": list(STATES),
        "risk_ceiling_policy": "cannot_exceed_parent_state",
        "risk_multiplier_range": [.85, 1.0],
    }
    artifact = {"version": version, "states": list(STATES), "market_profile_layer": profile_layer, "features": list(FEATURES), "feature_set": "market_regime_full_market_v2", "data_contract": "full_market_daily_aggregates_plus_broad_index_anchors_v2", "scaler": scaler, "model": model, "validation": validation, "latest_shadow_state": STATES[int(states[-1])], "dates": {"train_start": dates[0], "train_end": dates[split - 1], "oos_end": dates[-1]}}
    artifact_path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    model_version_id = create_model_version(model_key="market_regime", version=version, status="shadow_validated" if validation["status"] == "passed" else "shadow_failed", artifact_path=str(artifact_path), config={"family": "hierarchical_four_state_hsmm_with_12_profiles", "feature_set": "market_regime_full_market_v2", "data_contract": "full_market_daily_aggregates_plus_broad_index_anchors_v2", "observation": "multivariate_student_t", "duration": "truncated_negative_binomial", "min_weeks": 2, "max_weeks": 52, "profile_layer": profile_layer}, metrics={"log_likelihood": log_likelihood, **validation}, validation=validation, is_active=False)
    return {"ok": True, "model_version_id": model_version_id, "version": version, "latest_shadow_state": artifact["latest_shadow_state"], "validation": validation, "artifact_path": str(artifact_path), "api_refresh": api_refresh}
