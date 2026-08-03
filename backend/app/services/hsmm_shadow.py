from __future__ import annotations

import json
import math
import threading
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from scipy.special import gammaln, logsumexp
from scipy.stats import multivariate_t

from app.data_platform.db import _conn as data_conn
from app.db.store import create_model_version, list_model_versions
from app.services.model_artifacts import resolve_model_artifact_path
from app.core.runtime_paths import backend_cache_root


_HSMM_TRAINING_LOCK = threading.Lock()
HSMM_EVALUATION_CONTRACT = "alis_hsmm_task_native_evaluation_v3"
HSMM_TRAINING_ALGORITHM = "explicit_duration_forward_backward_em_v3"
HSMM_MIN_DURATION_WEEKS = 1
HSMM_MAX_DURATION_WEEKS = 52


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
MODEL_FEATURES = (
    "broad_market_return_20d",
    "broad_market_return_60d",
    "broad_market_realized_volatility_20d",
    "market_breadth",
    "market_average_return",
    "market_return_dispersion",
    "market_limit_balance",
    "market_amount_ratio_20d",
)


def _market_regime_axes(values: np.ndarray) -> np.ndarray:
    """Collapse correlated index copies into stable, interpretable axes."""

    matrix = np.asarray(values, dtype=float)
    if matrix.ndim != 2 or matrix.shape[1] != len(FEATURES):
        raise ValueError("market regime raw feature contract mismatch")
    return np.column_stack([
        np.mean(matrix[:, [0, 3, 6]], axis=1),
        np.mean(matrix[:, [1, 4, 7]], axis=1),
        np.mean(matrix[:, [2, 5, 8]], axis=1),
        matrix[:, FEATURES.index("market_breadth")],
        matrix[:, FEATURES.index("market_average_return")],
        matrix[:, FEATURES.index("market_return_dispersion")],
        matrix[:, FEATURES.index("market_limit_balance")],
        matrix[:, FEATURES.index("market_amount_ratio_20d")],
    ])


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
    return dates, _market_regime_axes(matrix)


def _robust_scale(train: np.ndarray, values: np.ndarray) -> tuple[np.ndarray, dict[str, list[float]]]:
    center = np.median(train, axis=0)
    mad = np.median(np.abs(train - center), axis=0)
    scale = np.where(mad > 1e-9, 1.4826 * mad, np.std(train, axis=0) + 1e-6)
    return np.clip((values - center) / scale, -8, 8), {"center": center.tolist(), "scale": scale.tolist()}


def _semantic_seed(values: np.ndarray) -> np.ndarray:
    if values.shape[1] == len(MODEL_FEATURES):
        returns20 = values[:, 0]
        returns60 = values[:, 1]
        volatility = values[:, 2]
        breadth = values[:, 3]
        market_return = values[:, 4]
        dispersion = values[:, 5]
        limit_balance = values[:, 6]
    else:
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
            runs[int(labels[start])].append(
                max(HSMM_MIN_DURATION_WEEKS, min(HSMM_MAX_DURATION_WEEKS, index - start))
            )
            start = index
    output = []
    for state in range(4):
        durations = runs[state] or [4]
        shifted = np.asarray(durations, dtype=float) - HSMM_MIN_DURATION_WEEKS
        mean_shifted = max(1e-3, float(np.mean(shifted)))
        variance_shifted = (
            float(np.var(shifted, ddof=1))
            if len(shifted) > 1
            else mean_shifted * 1.5
        )
        if variance_shifted <= mean_shifted + 1e-6:
            r = 200.0
        else:
            r = mean_shifted ** 2 / (variance_shifted - mean_shifted)
        r = max(.1, min(500.0, r))
        p = max(.02, min(.98, r / (r + mean_shifted)))
        output.append({
            "r": r,
            "p": p,
            "mean": mean_shifted + HSMM_MIN_DURATION_WEEKS,
        })
    return output


def _duration_logpmf(duration: int, params: dict[str, float]) -> float:
    k = max(0, duration - HSMM_MIN_DURATION_WEEKS)
    r, p = params["r"], params["p"]
    raw = gammaln(k + r) - gammaln(r) - gammaln(k + 1) + r * math.log(p) + k * math.log(1 - p)
    normalizer = sum(
        math.exp(
            gammaln(d - HSMM_MIN_DURATION_WEEKS + r)
            - gammaln(r)
            - gammaln(d - HSMM_MIN_DURATION_WEEKS + 1)
            + r * math.log(p)
            + (d - HSMM_MIN_DURATION_WEEKS) * math.log(1 - p)
        )
        for d in range(HSMM_MIN_DURATION_WEEKS, HSMM_MAX_DURATION_WEEKS + 1)
    )
    return float(raw - math.log(max(normalizer, 1e-300)))


def _duration_log_probabilities(model: dict[str, Any]) -> np.ndarray:
    result = np.full((len(model["durations"]), 53), -np.inf, dtype=float)
    for state, params in enumerate(model["durations"]):
        result[state, HSMM_MIN_DURATION_WEEKS:HSMM_MAX_DURATION_WEEKS + 1] = [
            _duration_logpmf(duration, params)
            for duration in range(HSMM_MIN_DURATION_WEEKS, HSMM_MAX_DURATION_WEEKS + 1)
        ]
    return result


def _initial_model_from_labels(
    values: np.ndarray,
    labels: np.ndarray,
) -> dict[str, Any]:
    emissions = []
    global_variance = np.maximum(np.var(values, axis=0), .05)
    for state in range(4):
        subset = values[labels == state]
        if len(subset) < 4:
            subset = values
        mean = np.median(subset, axis=0)
        variance = np.var(subset - mean, axis=0)
        variance = np.maximum(.85 * variance + .15 * global_variance, .05)
        covariance = np.diag(variance)
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


def _hsmm_expected_statistics(
    values: np.ndarray,
    model: dict[str, Any],
) -> dict[str, Any]:
    """Exact explicit-duration forward/backward E-step."""

    emission = _emission_log_probabilities(values, model)
    observation_count, state_count = emission.shape
    cumulative = np.vstack([
        np.zeros(state_count, dtype=float),
        np.cumsum(emission, axis=0),
    ])
    forward, likelihood = _hsmm_forward_scores(values, model)
    transitions = np.log(
        np.maximum(np.asarray(model["transitions"], dtype=float), 1e-300)
    )
    starts = np.log(np.maximum(np.asarray(model["start"], dtype=float), 1e-300))
    duration_logs = _duration_log_probabilities(model)
    backward = np.full(
        (observation_count + 1, state_count),
        -np.inf,
        dtype=float,
    )
    backward[observation_count] = 0.0
    for begin in range(observation_count - HSMM_MIN_DURATION_WEEKS, -1, -1):
        maximum_duration = min(HSMM_MAX_DURATION_WEEKS, observation_count - begin)
        for previous_state in range(state_count):
            candidates: list[float] = []
            for state in range(state_count):
                if state == previous_state:
                    continue
                for duration in range(HSMM_MIN_DURATION_WEEKS, maximum_duration + 1):
                    end = begin + duration
                    segment = (
                        cumulative[end, state]
                        - cumulative[begin, state]
                        + duration_logs[state, duration]
                    )
                    candidates.append(float(
                        transitions[previous_state, state]
                        + segment
                        + backward[end, state]
                    ))
            if candidates:
                backward[begin, previous_state] = float(logsumexp(candidates))

    start_counts = np.zeros(state_count, dtype=float)
    transition_counts = np.zeros((state_count, state_count), dtype=float)
    duration_counts = np.zeros((state_count, 53), dtype=float)
    occupancy_difference = np.zeros(
        (state_count, observation_count + 1),
        dtype=float,
    )
    for end in range(HSMM_MIN_DURATION_WEEKS, observation_count + 1):
        for state in range(state_count):
            for duration in range(
                HSMM_MIN_DURATION_WEEKS,
                min(HSMM_MAX_DURATION_WEEKS, end) + 1,
            ):
                begin = end - duration
                segment = (
                    cumulative[end, state]
                    - cumulative[begin, state]
                    + duration_logs[state, duration]
                )
                if begin == 0:
                    log_probability = (
                        starts[state]
                        + segment
                        + backward[end, state]
                        - likelihood
                    )
                    segment_probability = (
                        math.exp(log_probability)
                        if log_probability > -745
                        else 0.0
                    )
                    start_counts[state] += segment_probability
                else:
                    previous_logs = (
                        forward[begin]
                        + transitions[:, state]
                        + segment
                        + backward[end, state]
                        - likelihood
                    )
                    previous_logs[state] = -np.inf
                    previous_probabilities = np.exp(
                        np.clip(previous_logs, -745, 0)
                    )
                    previous_probabilities[state] = 0.0
                    transition_counts[:, state] += previous_probabilities
                    segment_probability = float(previous_probabilities.sum())
                if segment_probability <= 0:
                    continue
                duration_counts[state, duration] += segment_probability
                occupancy_difference[state, begin] += segment_probability
                occupancy_difference[state, end] -= segment_probability
    occupancy = np.cumsum(occupancy_difference[:, :-1], axis=1).T
    occupancy /= np.maximum(occupancy.sum(axis=1, keepdims=True), 1e-12)
    return {
        "likelihood": float(likelihood),
        "start_counts": start_counts,
        "transition_counts": transition_counts,
        "duration_counts": duration_counts,
        "occupancy": occupancy,
    }


def _weighted_duration_parameters(
    duration_counts: np.ndarray,
) -> list[dict[str, float]]:
    output: list[dict[str, float]] = []
    durations = np.arange(
        HSMM_MIN_DURATION_WEEKS,
        HSMM_MAX_DURATION_WEEKS + 1,
        dtype=float,
    )
    shifted = durations - HSMM_MIN_DURATION_WEEKS
    for state_counts in duration_counts:
        weights = np.asarray(
            state_counts[
                HSMM_MIN_DURATION_WEEKS:HSMM_MAX_DURATION_WEEKS + 1
            ],
            dtype=float,
        ) + 1e-6
        weights /= weights.sum()
        mean_shifted = max(1e-3, float(np.sum(weights * shifted)))
        variance_shifted = float(
            np.sum(weights * (shifted - mean_shifted) ** 2)
        )
        if variance_shifted <= mean_shifted + 1e-6:
            r = 200.0
        else:
            r = mean_shifted ** 2 / (variance_shifted - mean_shifted)
        r = max(.1, min(500.0, r))
        p = max(.02, min(.98, r / (r + mean_shifted)))
        output.append({
            "r": r,
            "p": p,
            "mean": mean_shifted + HSMM_MIN_DURATION_WEEKS,
        })
    return output


def _hsmm_m_step(
    values: np.ndarray,
    model: dict[str, Any],
    statistics: dict[str, Any],
) -> dict[str, Any]:
    state_count = len(model["emissions"])
    occupancy = np.asarray(statistics["occupancy"], dtype=float)
    global_variance = np.maximum(np.var(values, axis=0), .05)
    emissions: list[dict[str, Any]] = []
    for state in range(state_count):
        gamma = np.maximum(occupancy[:, state], 1e-12)
        previous = model["emissions"][state]
        previous_mean = np.asarray(previous["mean"], dtype=float)
        previous_variance = np.maximum(
            np.diag(np.asarray(previous["covariance"], dtype=float)),
            .05,
        )
        mahalanobis = np.sum(
            ((values - previous_mean) ** 2) / previous_variance,
            axis=1,
        )
        latent_scale = (
            float(previous.get("df") or 6.0) + values.shape[1]
        ) / (float(previous.get("df") or 6.0) + mahalanobis)
        weights = gamma * latent_scale
        if float(weights.sum()) <= 1e-8:
            emissions.append(previous)
            continue
        mean = np.sum(values * weights[:, np.newaxis], axis=0) / weights.sum()
        variance = np.sum(
            ((values - mean) ** 2) * weights[:, np.newaxis],
            axis=0,
        ) / weights.sum()
        variance = np.maximum(
            .85 * variance + .15 * global_variance,
            .05,
        )
        emissions.append({
            "mean": mean.tolist(),
            "covariance": np.diag(variance).tolist(),
            "df": 6.0,
        })
    transition_counts = (
        np.asarray(statistics["transition_counts"], dtype=float) + .1
    )
    np.fill_diagonal(transition_counts, 0.0)
    transitions = transition_counts / np.maximum(
        transition_counts.sum(axis=1, keepdims=True),
        1e-12,
    )
    start = np.asarray(statistics["start_counts"], dtype=float) + .25
    start /= start.sum()
    return {
        "emissions": emissions,
        "durations": _weighted_duration_parameters(
            np.asarray(statistics["duration_counts"], dtype=float)
        ),
        "transitions": transitions.tolist(),
        "start": start.tolist(),
    }


def _fit(
    values: np.ndarray,
    *,
    n_init: int = 1,
    max_iterations: int = 15,
    tolerance: float = 1e-5,
) -> dict[str, Any]:
    """Maximum-likelihood HSMM fit with explicit-duration EM."""

    semantic = _semantic_seed(values)
    fitted: list[tuple[float, dict[str, Any]]] = []
    for initialization in range(max(1, int(n_init))):
        labels = semantic.copy()
        if initialization:
            rng = np.random.default_rng(20260719 + initialization)
            changed = rng.random(len(labels)) < min(.12, .04 * initialization)
            labels[changed] = rng.integers(0, 4, size=int(changed.sum()))
        model = _initial_model_from_labels(values, labels)
        likelihood = _hsmm_forward_log_likelihood(values, model)
        trace = [float(likelihood)]
        converged = False
        accepted_iterations = 0
        for _iteration in range(max(1, int(max_iterations))):
            statistics = _hsmm_expected_statistics(values, model)
            candidate = _hsmm_m_step(values, model, statistics)
            candidate_likelihood = _hsmm_forward_log_likelihood(
                values,
                candidate,
            )
            if (
                not np.isfinite(candidate_likelihood)
                or candidate_likelihood + 1e-7 < likelihood
            ):
                break
            improvement = candidate_likelihood - likelihood
            model = candidate
            likelihood = float(candidate_likelihood)
            trace.append(likelihood)
            accepted_iterations += 1
            if improvement <= tolerance * max(1.0, abs(likelihood)):
                converged = True
                break
        model["training"] = {
            "algorithm": HSMM_TRAINING_ALGORITHM,
            "initialization": initialization,
            "accepted_iterations": accepted_iterations,
            "converged": converged,
            "likelihood_trace": [round(value, 8) for value in trace],
            "diagonal_student_t_emissions": True,
        }
        fitted.append((float(likelihood), model))
    return max(fitted, key=lambda item: item[0])[1]


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
    duration_logs = _duration_log_probabilities(model)
    for end in range(HSMM_MIN_DURATION_WEEKS, n + 1):
        for state in range(state_count):
            for duration in range(
                HSMM_MIN_DURATION_WEEKS,
                min(HSMM_MAX_DURATION_WEEKS, end) + 1,
            ):
                begin = end - duration
                segment = cumulative[end, state] - cumulative[begin, state] + duration_logs[state, duration]
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


def _emission_log_probabilities(
    values: np.ndarray,
    model: dict[str, Any],
) -> np.ndarray:
    result = np.zeros((len(values), len(model["emissions"])), dtype=float)
    for state, params in enumerate(model["emissions"]):
        result[:, state] = multivariate_t.logpdf(
            values,
            loc=np.asarray(params["mean"]),
            shape=np.asarray(params["covariance"]),
            df=params["df"],
        )
    return result


def _hsmm_forward_scores(
    values: np.ndarray,
    model: dict[str, Any],
) -> tuple[np.ndarray, float]:
    """Return explicit-duration HSMM forward scores and marginal log likelihood."""
    emission = _emission_log_probabilities(values, model)
    observation_count, state_count = emission.shape
    cumulative = np.vstack([
        np.zeros(state_count, dtype=float),
        np.cumsum(emission, axis=0),
    ])
    scores = np.full((observation_count + 1, state_count), -np.inf)
    transitions = np.log(
        np.maximum(np.asarray(model["transitions"], dtype=float), 1e-300)
    )
    starts = np.log(np.maximum(np.asarray(model["start"], dtype=float), 1e-300))
    duration_logs = _duration_log_probabilities(model)
    for end in range(HSMM_MIN_DURATION_WEEKS, observation_count + 1):
        for state in range(state_count):
            candidates: list[float] = []
            for duration in range(
                HSMM_MIN_DURATION_WEEKS,
                min(HSMM_MAX_DURATION_WEEKS, end) + 1,
            ):
                begin = end - duration
                segment_score = (
                    cumulative[end, state]
                    - cumulative[begin, state]
                    + duration_logs[state, duration]
                )
                if begin == 0:
                    candidates.append(float(starts[state] + segment_score))
                    continue
                previous = scores[begin] + transitions[:, state]
                previous[state] = -np.inf
                candidates.append(float(logsumexp(previous) + segment_score))
            if candidates:
                scores[end, state] = float(logsumexp(candidates))
    likelihood = float(logsumexp(scores[observation_count]))
    return scores, likelihood


def _hsmm_forward_log_likelihood(
    values: np.ndarray,
    model: dict[str, Any],
) -> float:
    return _hsmm_forward_scores(values, model)[1]


def _fit_hmm_baseline(
    values: np.ndarray,
    *,
    emissions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fit the strongest fair HMM baseline with the HSMM emissions frozen."""
    labels = _semantic_seed(values)
    state_count = len(emissions)
    counts = np.ones((state_count, state_count), dtype=float)
    for left, right in zip(labels[:-1], labels[1:]):
        counts[int(left), int(right)] += 1.0
    transitions = counts / np.maximum(counts.sum(axis=1, keepdims=True), 1e-12)
    start = np.bincount(
        labels[: min(52, len(labels))],
        minlength=state_count,
    ).astype(float) + 1.0
    start /= start.sum()
    model = {
        "emissions": emissions,
        "transitions": transitions.tolist(),
        "start": start.tolist(),
    }
    emission = _emission_log_probabilities(values, model)
    likelihood_trace: list[float] = []
    for _iteration in range(100):
        log_transitions = np.log(
            np.maximum(np.asarray(model["transitions"], dtype=float), 1e-300)
        )
        log_start = np.log(
            np.maximum(np.asarray(model["start"], dtype=float), 1e-300)
        )
        alpha = np.full((len(values), state_count), -np.inf, dtype=float)
        alpha[0] = log_start + emission[0]
        for index in range(1, len(values)):
            alpha[index] = emission[index] + logsumexp(
                alpha[index - 1][:, np.newaxis] + log_transitions,
                axis=0,
            )
        likelihood = float(logsumexp(alpha[-1]))
        likelihood_trace.append(likelihood)
        beta = np.zeros((len(values), state_count), dtype=float)
        for index in range(len(values) - 2, -1, -1):
            beta[index] = logsumexp(
                log_transitions
                + emission[index + 1][np.newaxis, :]
                + beta[index + 1][np.newaxis, :],
                axis=1,
            )
        gamma = np.exp(np.clip(alpha + beta - likelihood, -745, 0))
        expected_transitions = np.ones((state_count, state_count), dtype=float) * .1
        for index in range(len(values) - 1):
            expected_transitions += np.exp(np.clip(
                alpha[index][:, np.newaxis]
                + log_transitions
                + emission[index + 1][np.newaxis, :]
                + beta[index + 1][np.newaxis, :]
                - likelihood,
                -745,
                0,
            ))
        next_transitions = expected_transitions / np.maximum(
            expected_transitions.sum(axis=1, keepdims=True),
            1e-12,
        )
        next_start = gamma[0] + .25
        next_start /= next_start.sum()
        change = max(
            float(np.max(np.abs(
                next_transitions
                - np.asarray(model["transitions"], dtype=float)
            ))),
            float(np.max(np.abs(
                next_start - np.asarray(model["start"], dtype=float)
            ))),
        )
        model["transitions"] = next_transitions.tolist()
        model["start"] = next_start.tolist()
        if change < 1e-6:
            break
    model["training"] = {
        "algorithm": "baum_welch_fixed_hsmm_emissions_v1",
        "iterations": len(likelihood_trace),
        "likelihood_trace": [
            round(value, 8) for value in likelihood_trace
        ],
    }
    return model


def _hmm_forward_log_likelihood(
    values: np.ndarray,
    model: dict[str, Any],
) -> float:
    emission = _emission_log_probabilities(values, model)
    if not len(emission):
        return float("-inf")
    transitions = np.log(
        np.maximum(np.asarray(model["transitions"], dtype=float), 1e-300)
    )
    score = (
        np.log(np.maximum(np.asarray(model["start"], dtype=float), 1e-300))
        + emission[0]
    )
    for index in range(1, len(emission)):
        score = emission[index] + logsumexp(
            score[:, np.newaxis] + transitions,
            axis=0,
        )
    return float(logsumexp(score))


def _decoded_run_durations(states: list[int]) -> list[tuple[int, int]]:
    if not states:
        return []
    result: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(states) + 1):
        if index == len(states) or states[index] != states[start]:
            result.append((int(states[start]), index - start))
            start = index
    return result


def _truncated_geometric_duration_logpmf(
    duration: int,
    self_transition_probability: float,
) -> float:
    probability = min(1 - 1e-9, max(1e-9, self_transition_probability))
    values = np.asarray([
        (probability ** (value - 1)) * (1.0 - probability)
        for value in range(HSMM_MIN_DURATION_WEEKS, HSMM_MAX_DURATION_WEEKS + 1)
    ])
    normalized = values / max(float(values.sum()), 1e-300)
    return float(np.log(max(float(
        normalized[duration - HSMM_MIN_DURATION_WEEKS]
    ), 1e-300)))


def _duration_log_scores(
    decoded: list[int],
    hsmm_model: dict[str, Any],
    hmm_model: dict[str, Any],
) -> dict[str, Any]:
    runs = [
        (state, duration)
        for state, duration in _decoded_run_durations(decoded)
        if HSMM_MIN_DURATION_WEEKS <= duration <= HSMM_MAX_DURATION_WEEKS
    ]
    if not runs:
        return {
            "run_count": 0,
            "hsmm_mean_log_score": None,
            "hmm_geometric_mean_log_score": None,
            "hsmm_minus_hmm": None,
        }
    transitions = np.asarray(hmm_model["transitions"], dtype=float)
    hsmm_scores = [
        _duration_logpmf(duration, hsmm_model["durations"][state])
        for state, duration in runs
    ]
    hmm_scores = [
        _truncated_geometric_duration_logpmf(
            duration,
            float(transitions[state, state]),
        )
        for state, duration in runs
    ]
    return {
        "run_count": len(runs),
        "hsmm_mean_log_score": round(float(np.mean(hsmm_scores)), 8),
        "hmm_geometric_mean_log_score": round(float(np.mean(hmm_scores)), 8),
        "hsmm_minus_hmm": round(
            float(np.mean(hsmm_scores) - np.mean(hmm_scores)),
            8,
        ),
    }


def _filtered_terminal_probabilities(
    values: np.ndarray,
    model: dict[str, Any],
) -> np.ndarray:
    scores, _likelihood = _hsmm_forward_scores(values, model)
    terminal = scores[len(values)]
    return np.exp(terminal - logsumexp(terminal))


@lru_cache(maxsize=2048)
def _infer_hsmm_state_cached(
    version_id: int,
    artifact_path: str,
    created_at: str,
    cutoff: str,
) -> tuple[tuple[str, Any], ...]:
    if created_at:
        try:
            created_dt = datetime.fromisoformat(created_at)
            cutoff_dt = datetime.fromisoformat(cutoff)
            if created_dt.tzinfo is not None and cutoff_dt.tzinfo is None:
                cutoff_dt = cutoff_dt.replace(tzinfo=created_dt.tzinfo)
            elif created_dt.tzinfo is None and cutoff_dt.tzinfo is not None:
                created_dt = created_dt.replace(tzinfo=cutoff_dt.tzinfo)
            if created_dt > cutoff_dt:
                return tuple(sorted({
                    "ok": False,
                    "status": "fallback",
                    "reason": "hsmm_version_created_after_cutoff",
                }.items()))
        except ValueError:
            if created_at > cutoff:
                return tuple(sorted({
                    "ok": False,
                    "status": "fallback",
                    "reason": "hsmm_version_created_after_cutoff",
                }.items()))
    path = resolve_model_artifact_path(artifact_path)
    if not path.is_file():
        return tuple(sorted({
            "ok": False,
            "status": "fallback",
            "reason": "hsmm_artifact_unavailable",
        }.items()))
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
        artifact_features = tuple(
            str(feature)
            for feature in (
                artifact.get("raw_features")
                or artifact.get("features")
                or FEATURES
            )
        )
        if not artifact_features:
            raise RuntimeError("hsmm_feature_contract_missing")
        with data_conn() as c:
            rows = c.execute(
                """SELECT feature_date,features_json FROM model_training_history
                   WHERE feature_set='market_regime_full_market_v2'
                     AND feature_date<=? AND source_as_of_time<=?
                   ORDER BY feature_date DESC LIMIT 520""",
                (cutoff[:10].replace("-", ""), cutoff),
            ).fetchall()
        weekly: dict[tuple[int, int], tuple[str, dict[str, Any]]] = {}
        for row in reversed(rows):
            item = dict(row)
            feature_date = str(item.get("feature_date") or "")
            try:
                dt = datetime.strptime(feature_date, "%Y%m%d")
                payload = json.loads(item.get("features_json") or "{}")
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
            if all(payload.get(feature) is not None for feature in artifact_features):
                iso = dt.isocalendar()
                weekly[(iso.year, iso.week)] = (feature_date, payload)
        values = list(weekly.values())[-104:]
        if len(values) < 4:
            raise RuntimeError("hsmm_point_in_time_features_insufficient")
        raw = np.asarray(
            [[float(item[1][feature]) for feature in artifact_features] for item in values],
            dtype=float,
        )
        model_values = (
            _market_regime_axes(raw)
            if artifact.get("feature_transform")
            == "broad_index_axis_aggregation_v1"
            else raw
        )
        scaler = artifact.get("scaler") if isinstance(artifact.get("scaler"), dict) else {}
        center = np.asarray(scaler.get("center") or [], dtype=float)
        scale = np.asarray(scaler.get("scale") or [], dtype=float)
        if (
            len(center) != model_values.shape[1]
            or len(scale) != model_values.shape[1]
        ):
            raise RuntimeError("hsmm_scaler_contract_invalid")
        scaled = np.clip(
            (model_values - center) / np.maximum(scale, 1e-9),
            -8,
            8,
        )
        model = artifact.get("model") if isinstance(artifact.get("model"), dict) else {}
        decoded, _score = _decode(scaled, model)
        state_index = int(decoded[-1])
        if all(key in model for key in ("durations", "transitions", "start")):
            probabilities = _filtered_terminal_probabilities(scaled, model)
            probability_method = "point_in_time_hsmm_filtered_terminal_v1"
        else:
            emission_logs = _emission_log_probabilities(scaled[-1:], model)[0]
            probabilities = np.exp(emission_logs - logsumexp(emission_logs))
            probability_method = "legacy_emission_posterior_compatibility_only"
        payload = {
            "ok": True,
            "status": "ready",
            "state": str((artifact.get("states") or STATES)[state_index]),
            "confidence": round(float(probabilities[state_index]), 6),
            "version_id": int(version_id),
            "feature_date": str(values[-1][0]),
            "feature_count": len(artifact_features),
            "model_axis_count": int(model_values.shape[1]),
            "method": probability_method,
            "state_path_method": "point_in_time_hsmm_viterbi_v1",
            "future_smoothed_probabilities_forbidden": True,
        }
    except Exception as exc:
        payload = {
            "ok": False,
            "status": "fallback",
            "reason": str(exc)[:300],
            "version_id": int(version_id),
        }
    return tuple(sorted(payload.items()))


def infer_hsmm_state_as_of(
    binding: dict[str, Any] | None,
    *,
    as_of_time: str,
) -> dict[str, Any]:
    if not isinstance(binding, dict) or not int(binding.get("id") or 0):
        return {"ok": False, "status": "fallback", "reason": "hsmm_binding_missing"}
    return dict(_infer_hsmm_state_cached(
        int(binding["id"]),
        str(binding.get("artifact_path") or ""),
        str(binding.get("created_at") or ""),
        str(as_of_time),
    ))


def _repeat_training_blocker(dates: list[str]) -> dict[str, Any] | None:
    latest = next(iter(list_model_versions("market_regime", limit=1)), None)
    if not latest or not dates:
        return None
    validation = latest.get("validation") if isinstance(
        latest.get("validation"), dict
    ) else {}
    observed_through = str(validation.get("oos_end") or "").replace("-", "")[:8]
    latest_available = str(dates[-1] or "").replace("-", "")[:8]
    if (
        str(validation.get("evaluation_contract") or "")
        == HSMM_EVALUATION_CONTRACT
        and observed_through
        and latest_available
        and observed_through >= latest_available
    ):
        return {
            "ok": True,
            "started": False,
            "status": "blocked_by_hsmm_governance",
            "reason": "current_contract_already_evaluated_without_new_data",
            "model_version_id": int(latest["id"]),
            "version": str(latest.get("version") or ""),
            "validation_status": str(validation.get("status") or ""),
            "observed_through": observed_through,
            "latest_available_date": latest_available,
            "production_change_forbidden": True,
        }
    return None


def train_hsmm_shadow(
    *,
    refresh_api: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    if not _HSMM_TRAINING_LOCK.acquire(blocking=False):
        return {
            "ok": True,
            "started": False,
            "status": "blocked_by_hsmm_governance",
            "reason": "training_in_progress",
            "production_change_forbidden": True,
        }
    try:
        return _train_hsmm_shadow_sync(
            refresh_api=refresh_api,
            force=force,
        )
    finally:
        _HSMM_TRAINING_LOCK.release()


def _enqueue_hsmm_research_trigger(
    *,
    model_version_id: int,
    version: str,
    validation: dict[str, Any],
) -> dict[str, Any]:
    if str(validation.get("status") or "") != "passed":
        return {
            "status": "not_enqueued",
            "reason": "hsmm_task_native_evaluation_failed",
        }
    try:
        from app.services.evolution_orchestrator import (
            enqueue_factor_strategy_research_event,
        )

        task = enqueue_factor_strategy_research_event(
            trigger="hsmm_shadow_training_completed",
            evidence_type="market_state_model_version",
            evidence_id=int(model_version_id),
            evidence={"model_key": "market_regime", "version": version},
            priority=75,
        )
        return {
            "status": str(task.get("status") or "queued"),
            "task_id": int(task.get("id") or 0),
        }
    except Exception as exc:  # Preserve the trained Shadow if the queue is temporarily unavailable.
        return {"status": "enqueue_failed", "error": str(exc)[:300]}


def _train_hsmm_shadow_sync(
    *,
    refresh_api: bool,
    force: bool,
) -> dict[str, Any]:
    from app.services.market_context import MARKET_PROFILE_LABELS

    dates, raw = _load_weekly_features()
    blocker = None if force else _repeat_training_blocker(dates)
    if blocker:
        return blocker
    api_refresh = refresh_market_regime_training_history() if refresh_api else {"status": "skipped"}
    dates, raw = _load_weekly_features()
    if len(raw) < 52 * 6:
        return {"ok": False, "status": "insufficient_history", "observations": len(raw), "minimum": 52 * 6, "api_refresh": api_refresh}

    initial_train_end = int(len(raw) * 0.70)
    remaining = len(raw) - initial_train_end
    fold_size = max(1, remaining // 3)
    fold_evidence: list[dict[str, Any]] = []
    for fold_index in range(3):
        train_end = initial_train_end + fold_index * fold_size
        test_end = (
            len(raw)
            if fold_index == 2
            else min(len(raw), train_end + fold_size)
        )
        train_values, _fold_scaler = _robust_scale(
            raw[:train_end],
            raw[:train_end],
        )
        test_values, _ = _robust_scale(raw[:train_end], raw[train_end:test_end])
        fold_model = _fit(
            train_values,
            n_init=1,
            max_iterations=12,
        )
        hmm_baseline = _fit_hmm_baseline(
            train_values,
            emissions=fold_model["emissions"],
        )
        decoded, viterbi_log_probability = _decode(test_values, fold_model)
        hsmm_likelihood = _hsmm_forward_log_likelihood(test_values, fold_model)
        hmm_likelihood = _hmm_forward_log_likelihood(
            test_values,
            hmm_baseline,
        )
        duration_scores = _duration_log_scores(
            _semantic_seed(test_values).astype(int).tolist(),
            fold_model,
            hmm_baseline,
        )
        states_fold = np.asarray(decoded, dtype=int)
        occupancy = {
            state: round(float(np.mean(states_fold == state)), 8)
            for state in range(4)
        }
        return_signal = np.mean(test_values[:, [0, 1, 4]], axis=1)
        volatility_signal = test_values[:, 2]
        state_return = {
            state: (
                float(np.mean(return_signal[states_fold == state]))
                if np.any(states_fold == state)
                else None
            )
            for state in range(4)
        }
        state_volatility = {
            state: (
                float(np.mean(volatility_signal[states_fold == state]))
                if np.any(states_fold == state)
                else None
            )
            for state in range(4)
        }
        semantic_alignment = bool(
            state_return[0] is not None
            and state_return[2] is not None
            and state_volatility[0] is not None
            and state_volatility[2] is not None
            and float(state_return[0]) > float(state_return[2])
            and float(state_volatility[2]) > float(state_volatility[0])
        )
        fold_evidence.append({
            "fold": fold_index + 1,
            "train_start": dates[0],
            "train_end": dates[train_end - 1],
            "test_start": dates[train_end],
            "test_end": dates[test_end - 1],
            "test_observations": len(test_values),
            "hsmm_marginal_log_likelihood": round(hsmm_likelihood, 8),
            "hmm_marginal_log_likelihood": round(hmm_likelihood, 8),
            "hsmm_log_likelihood_per_observation": round(
                hsmm_likelihood / len(test_values),
                8,
            ),
            "hmm_log_likelihood_per_observation": round(
                hmm_likelihood / len(test_values),
                8,
            ),
            "likelihood_gain_per_observation": round(
                (hsmm_likelihood - hmm_likelihood) / len(test_values),
                8,
            ),
            "viterbi_best_path_log_probability": round(
                viterbi_log_probability,
                8,
            ),
            "duration_diagnostic": {
                **duration_scores,
                "reference": "heldout_deterministic_semantic_proxy_runs",
                "promotion_gate": False,
            },
            "state_occupancy": occupancy,
            "occupied_state_count": sum(value > 0 for value in occupancy.values()),
            "largest_state_share": round(max(occupancy.values()), 8),
            "semantic_alignment": semantic_alignment,
            "state_return_signal": state_return,
            "state_volatility_signal": state_volatility,
            "hsmm_training": fold_model.get("training") or {},
            "hmm_training": hmm_baseline.get("training") or {},
        })

    likelihood_gain_passes = sum(
        float(item["likelihood_gain_per_observation"]) > 0
        for item in fold_evidence
    )
    occupancy_passes = sum(
        int(item["occupied_state_count"]) >= 3
        and float(item["largest_state_share"]) < 0.95
        for item in fold_evidence
    )
    semantic_alignment_passes = sum(
        bool(item["semantic_alignment"]) for item in fold_evidence
    )
    task_gates = {
        "three_expanding_oos_windows": len(fold_evidence) == 3,
        "finite_marginal_likelihoods": all(
            np.isfinite(float(item["hsmm_marginal_log_likelihood"]))
            and np.isfinite(float(item["hmm_marginal_log_likelihood"]))
            for item in fold_evidence
        ),
        "explicit_duration_em_fitted_all_windows": all(
            str((item.get("hsmm_training") or {}).get("algorithm") or "")
            == HSMM_TRAINING_ALGORITHM
            and int(
                (item.get("hsmm_training") or {}).get(
                    "accepted_iterations",
                )
                or 0
            ) > 0
            for item in fold_evidence
        ),
        "hsmm_likelihood_beats_same_emission_hmm_majority": (
            likelihood_gain_passes >= 2
        ),
        "noncollapsed_state_occupancy_majority": occupancy_passes >= 2,
        "semantic_state_alignment_majority": semantic_alignment_passes >= 2,
    }

    full_scaled, scaler = _robust_scale(raw, raw)
    model = _fit(full_scaled, n_init=2, max_iterations=20)
    decoded, viterbi_log_probability = _decode(full_scaled, model)
    states = np.asarray(decoded)
    marginal_log_likelihood = _hsmm_forward_log_likelihood(full_scaled, model)
    validation = {
        "schema_version": "alis_hsmm_task_native_evaluation_v3",
        "evaluation_contract": HSMM_EVALUATION_CONTRACT,
        "status": "passed" if all(task_gates.values()) else "shadow_failed",
        "task_native": {
            "task": "explicit_duration_latent_market_regime_model",
            "baseline": "same_emission_geometric_duration_hmm",
            "walk_forward_evidence": fold_evidence,
            "gates": task_gates,
            "passed": all(task_gates.values()),
            "policy_note": (
                "边际序列似然和显式时长基线来自HSMM标准；"
                "多数窗、占用率和语义对齐是ALIS生产治理门，不是通用论文阈值"
            ),
        },
        "full_fit_marginal_log_likelihood": round(marginal_log_likelihood, 8),
        "full_fit_viterbi_best_path_log_probability": round(
            viterbi_log_probability,
            8,
        ),
        "training": model.get("training") or {},
        "feature_transform": {
            "contract": "broad_index_axis_aggregation_v1",
            "raw_feature_count": len(FEATURES),
            "model_axis_count": len(MODEL_FEATURES),
            "correlated_index_copies_collapsed": True,
        },
        "oos_start": fold_evidence[0]["test_start"],
        "oos_end": fold_evidence[-1]["test_end"],
        "oos_observations": sum(
            int(item["test_observations"]) for item in fold_evidence
        ),
    }
    version = f"hsmm-shadow-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    artifact_dir = backend_cache_root() / "models" / "market_regime"
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
    artifact = {
        "version": version,
        "states": list(STATES),
        "market_profile_layer": profile_layer,
        "features": list(FEATURES),
        "raw_features": list(FEATURES),
        "model_features": list(MODEL_FEATURES),
        "feature_transform": "broad_index_axis_aggregation_v1",
        "feature_set": "market_regime_full_market_v2",
        "data_contract": "full_market_daily_aggregates_plus_broad_index_anchors_v2",
        "scaler": scaler,
        "model": model,
        "validation": validation,
        "latest_shadow_state": STATES[int(states[-1])],
        "dates": {
            "train_start": dates[0],
            "train_end": dates[-1],
            "oos_start": fold_evidence[0]["test_start"],
            "oos_end": dates[-1],
        },
    }
    artifact_path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    model_version_id = create_model_version(model_key="market_regime", version=version, status="shadow_validated" if validation["status"] == "passed" else "shadow_failed", artifact_path=str(artifact_path), config={"family": "hierarchical_four_state_hsmm_with_12_profiles", "feature_set": "market_regime_full_market_v2", "feature_transform": "broad_index_axis_aggregation_v1", "model_features": list(MODEL_FEATURES), "training_algorithm": HSMM_TRAINING_ALGORITHM, "data_contract": "full_market_daily_aggregates_plus_broad_index_anchors_v2", "observation": "multivariate_student_t_diagonal_covariance", "duration": "truncated_negative_binomial", "min_weeks": HSMM_MIN_DURATION_WEEKS, "max_weeks": HSMM_MAX_DURATION_WEEKS, "profile_layer": profile_layer}, metrics={"marginal_log_likelihood": marginal_log_likelihood, "viterbi_best_path_log_probability": viterbi_log_probability, **validation}, validation=validation, is_active=False)
    research_trigger = _enqueue_hsmm_research_trigger(
        model_version_id=int(model_version_id),
        version=version,
        validation=validation,
    )
    return {
        "ok": True,
        "model_version_id": model_version_id,
        "version": version,
        "latest_shadow_state": artifact["latest_shadow_state"],
        "validation": validation,
        "artifact_path": str(artifact_path),
        "api_refresh": api_refresh,
        "continuous_research_trigger": research_trigger,
    }
