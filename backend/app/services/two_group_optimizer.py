from __future__ import annotations

from typing import Any

import numpy as np
from scipy.cluster.hierarchy import leaves_list, linkage
from scipy.spatial.distance import squareform
from scipy.optimize import minimize

from app.services.quant_risk import historical_cvar


STABLE_BUCKETS = {"medium", "defensive", "liquidity"}
OFFENSIVE_BUCKETS = {"narrative", "leader", "event"}


def _normalize(weights: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, float(value)) for value in weights.values())
    if total <= 0:
        return {}
    return {symbol: max(0.0, float(value)) / total for symbol, value in weights.items()}


def _bounded_fallback(
    weights: dict[str, float],
    symbols: list[str],
    bounds: dict[str, dict[str, float]],
    budget: float,
) -> dict[str, float]:
    result = {
        symbol: max(
            float((bounds.get(symbol) or {}).get("min_weight") or 0.0),
            min(
                float((bounds.get(symbol) or {}).get("max_weight") or 0.0),
                max(0.0, float(weights.get(symbol) or 0.0)),
            ),
        )
        for symbol in symbols
    }
    for _ in range(max(4, len(symbols) * 2)):
        gap = budget - sum(result.values())
        if abs(gap) <= 1e-10:
            break
        if gap > 0:
            room = {
                symbol: max(0.0, float((bounds.get(symbol) or {}).get("max_weight") or 0.0) - result[symbol])
                for symbol in symbols
            }
        else:
            room = {
                symbol: max(0.0, result[symbol] - float((bounds.get(symbol) or {}).get("min_weight") or 0.0))
                for symbol in symbols
            }
        total_room = sum(room.values())
        if total_room <= 1e-12:
            break
        magnitude = min(abs(gap), total_room)
        direction = 1.0 if gap > 0 else -1.0
        for symbol in symbols:
            result[symbol] += direction * magnitude * room[symbol] / total_room
    return result


def _aligned_return_matrix(observations: dict[str, list[float]], symbols: list[str]) -> np.ndarray:
    """Align on the most recent common window without inventing observations."""
    common = min((len(observations[symbol]) for symbol in symbols), default=0)
    if common <= 0:
        return np.empty((0, len(symbols)))
    return np.asarray([observations[symbol][-common:] for symbol in symbols], dtype=float).T


def _empirical_cvar_loss(matrix: np.ndarray, weights: np.ndarray, beta: float = 0.95) -> float:
    losses = -(matrix @ weights)
    if losses.size == 0:
        return 0.0
    var = float(np.quantile(losses, beta))
    tail = losses[losses >= var - 1e-12]
    return float(np.mean(tail)) if tail.size else var


def _inverse_volatility_fallback(matrix: np.ndarray, symbols: list[str]) -> dict[str, float]:
    if matrix.size == 0 or not symbols:
        return {}
    volatility = np.sqrt(np.mean(np.square(matrix), axis=0))
    inverse = 1.0 / np.maximum(volatility, 1e-6)
    inverse /= float(np.sum(inverse)) or 1.0
    return {symbol: float(inverse[index]) for index, symbol in enumerate(symbols)}


def _hrp_fallback(matrix: np.ndarray, symbols: list[str]) -> dict[str, float]:
    """Hierarchical risk parity with correlation clustering and recursive bisection."""
    if matrix.size == 0 or not symbols:
        return {}
    if len(symbols) < 2:
        return {symbols[0]: 1.0}
    covariance = np.atleast_2d(np.cov(matrix, rowvar=False))
    diagonal = np.maximum(np.diag(covariance), 1e-12)
    scale = np.sqrt(np.outer(diagonal, diagonal))
    correlation = np.clip(covariance / np.maximum(scale, 1e-12), -1.0, 1.0)
    np.fill_diagonal(correlation, 1.0)
    distance = np.sqrt(np.maximum(0.0, (1.0 - correlation) / 2.0))
    order = list(leaves_list(linkage(squareform(distance, checks=False), method="single")))
    weights = np.ones(len(symbols), dtype=float)

    def cluster_variance(indices: list[int]) -> float:
        sub_covariance = covariance[np.ix_(indices, indices)]
        inverse_variance = 1.0 / np.maximum(np.diag(sub_covariance), 1e-12)
        allocation = inverse_variance / (float(np.sum(inverse_variance)) or 1.0)
        return float(allocation @ sub_covariance @ allocation)

    clusters = [order]
    while clusters:
        next_clusters: list[list[int]] = []
        for cluster in clusters:
            if len(cluster) <= 1:
                continue
            midpoint = len(cluster) // 2
            left, right = cluster[:midpoint], cluster[midpoint:]
            left_variance, right_variance = cluster_variance(left), cluster_variance(right)
            left_fraction = right_variance / max(left_variance + right_variance, 1e-12)
            weights[left] *= left_fraction
            weights[right] *= 1.0 - left_fraction
            next_clusters.extend([left, right])
        clusters = next_clusters
    weights /= float(np.sum(weights)) or 1.0
    return {symbol: float(weights[index]) for index, symbol in enumerate(symbols)}


def two_group_cvar_optimizer(
    returns_by_symbol: dict[str, list[float]],
    *,
    expected_returns: dict[str, float] | None = None,
    buckets: dict[str, str] | None = None,
    current_weights: dict[str, float] | None = None,
    trading_costs: dict[str, float] | None = None,
    crowding: dict[str, float] | None = None,
    weight_bounds: dict[str, dict[str, float]] | None = None,
    target_budget: float = 1.0,
    risk_aversion: float = 2.0,
    concentration_penalty: float = 0.50,
) -> dict[str, Any]:
    """Optimize expected return against empirical CVaR and implementation costs.

    Bucket labels are deliberately descriptive only.  They no longer impose
    fixed stable/offensive allocations or bucket-specific single-name caps.
    Account and order safety constraints remain in the execution layer.
    """
    observations = {
        str(symbol): [float(value) for value in values]
        for symbol, values in returns_by_symbol.items()
        if values
    }
    if not observations:
        return {
            "ok": False,
            "status": "insufficient_data",
            "schema_version": "portfolio_cvar_optimizer_v2",
            "optimizer": "empirical_cvar_slsqp_v2",
            "weights": {},
            "blocked_reasons": ["missing_returns"],
            "fallback_used": False,
        }

    symbols = list(observations)
    matrix = _aligned_return_matrix(observations, symbols)
    if matrix.shape[0] < 2:
        return {
            "ok": False,
            "status": "insufficient_data",
            "schema_version": "portfolio_cvar_optimizer_v2",
            "optimizer": "empirical_cvar_slsqp_v2",
            "weights": {},
            "blocked_reasons": ["insufficient_common_observations"],
            "fallback_used": False,
        }

    expected_vector = np.asarray([float((expected_returns or {}).get(symbol) or 0.0) for symbol in symbols])
    current = {symbol: max(0.0, float((current_weights or {}).get(symbol) or 0.0)) for symbol in symbols}
    raw_bounds = dict(weight_bounds or {})
    bounds_map = {
        symbol: {
            "min_weight": max(0.0, float((raw_bounds.get(symbol) or {}).get("min_weight") or 0.0)),
            "max_weight": max(0.0, min(1.0, float((raw_bounds.get(symbol) or {}).get("max_weight") if (raw_bounds.get(symbol) or {}).get("max_weight") is not None else 1.0))),
        }
        for symbol in symbols
    }
    minimum_budget = sum(item["min_weight"] for item in bounds_map.values())
    maximum_budget = sum(item["max_weight"] for item in bounds_map.values())
    budget = max(minimum_budget, min(maximum_budget, max(0.0, min(1.0, float(target_budget or 0.0)))))
    current_vector = np.asarray([current.get(symbol, 0.0) for symbol in symbols])
    if float(np.sum(current_vector)) <= 0:
        current = _bounded_fallback(_inverse_volatility_fallback(matrix, symbols), symbols, bounds_map, budget)
    else:
        current = _bounded_fallback(current, symbols, bounds_map, budget)
    current_vector = np.asarray([current.get(symbol, 0.0) for symbol in symbols])
    cost_vector = np.asarray([max(0.0, float((trading_costs or {}).get(symbol) or 0.0)) for symbol in symbols])
    crowding_vector = np.asarray([max(0.0, float((crowding or {}).get(symbol) or 0.0)) for symbol in symbols])

    def objective(weight_vector: np.ndarray) -> float:
        expected = float(expected_vector @ weight_vector)
        cvar = _empirical_cvar_loss(matrix, weight_vector)
        turnover_cost = float(cost_vector @ np.abs(weight_vector - current_vector))
        crowding_cost = float(crowding_vector @ weight_vector)
        concentration = float(np.sum(np.square(weight_vector)))
        return -expected + float(risk_aversion) * cvar + turnover_cost + crowding_cost + float(concentration_penalty) * concentration

    result = minimize(
        objective,
        current_vector,
        method="SLSQP",
        bounds=[(bounds_map[symbol]["min_weight"], bounds_map[symbol]["max_weight"]) for symbol in symbols],
        constraints=[{"type": "eq", "fun": lambda weights: float(np.sum(weights) - budget)}],
        options={"maxiter": 500, "ftol": 1e-10},
    )
    fallback_used = not bool(result.success) or result.x is None or not np.all(np.isfinite(result.x))
    if fallback_used:
        fallback = _hrp_fallback(matrix, symbols) or _inverse_volatility_fallback(matrix, symbols)
        weight_map = _bounded_fallback(fallback, symbols, bounds_map, budget)
        status = "fallback"
        solver_message = str(result.message)
    else:
        solved = np.maximum(np.asarray(result.x, dtype=float), 0.0)
        weight_map = {symbol: float(solved[index]) for index, symbol in enumerate(symbols)}
        status = "optimal"
        solver_message = str(result.message)

    weights = {symbol: round(weight, 8) for symbol, weight in weight_map.items()}
    # Remove rounding drift while preserving the Controller-bounded budget.
    largest = max(weights, key=weights.get)
    weights[largest] = round(weights[largest] + (budget - sum(weights.values())), 8)
    bucket_map = {symbol: str((buckets or {}).get(symbol) or "unclassified") for symbol in symbols}
    stable_weight = sum(weights[symbol] for symbol in symbols if bucket_map[symbol] in STABLE_BUCKETS)
    offensive_weight = sum(weights[symbol] for symbol in symbols if bucket_map[symbol] in OFFENSIVE_BUCKETS)
    cvar95 = _empirical_cvar_loss(matrix, np.asarray([weights[symbol] for symbol in symbols]))
    expected_net_return = sum(weights[symbol] * expected_vector[index] for index, symbol in enumerate(symbols))
    turnover_cost = sum(abs(weights[symbol] - current_vector[index]) * cost_vector[index] for index, symbol in enumerate(symbols))
    crowding_penalty_value = sum(weights[symbol] * crowding_vector[index] for index, symbol in enumerate(symbols))
    concentration_value = sum(weight * weight for weight in weights.values())
    objective_terms = {
        "expected_net_return": float(expected_net_return),
        "cvar95": float(cvar95),
        "turnover_cost": float(turnover_cost),
        "crowding_penalty": float(crowding_penalty_value),
        "concentration_penalty": float(concentration_penalty) * concentration_value,
    }
    explanations: dict[str, dict[str, Any]] = {}
    total_standalone_risk = sum(
        weights[symbol] * abs(float(historical_cvar(observations[symbol]).get("cvar") or 0.0))
        for symbol in symbols
    ) or 1.0
    for index, symbol in enumerate(symbols):
        standalone_risk = abs(float(historical_cvar(observations[symbol]).get("cvar") or 0.0))
        explanations[symbol] = {
            "weight": weights[symbol],
            "function_bucket": bucket_map[symbol],
            "expected_return_contribution": round(weights[symbol] * expected_vector[index], 8),
            "cvar_contribution": round(weights[symbol] * standalone_risk / total_standalone_risk, 8),
            "trading_cost": round(abs(weights[symbol] - current_vector[index]) * cost_vector[index], 8),
            "crowding_penalty": round(weights[symbol] * crowding_vector[index], 8),
            "constraint_shadow_price": 0.0,
        }

    return {
        "ok": True,
        "status": status,
        "schema_version": "portfolio_cvar_optimizer_v2",
        "optimizer": "empirical_cvar_slsqp_v2" if not fallback_used else "hierarchical_risk_parity_fallback_v2",
        "objective": "max_expected_return_minus_cvar_cost_turnover_crowding_and_concentration",
        "objective_value": round(-objective(np.asarray([weights[symbol] for symbol in symbols])), 8),
        "objective_terms": {key: round(value, 8) for key, value in objective_terms.items()},
        "weights": weights,
        "weight_sum": round(sum(weights.values()), 8),
        "risk_groups": {"stable": round(stable_weight, 8), "offensive": round(offensive_weight, 8)},
        "gates": {
            "weight_sum_target": abs(sum(weights.values()) - budget) <= 1e-6,
            "long_only": all(value >= 0 for value in weights.values()),
            "controller_bounds": all(
                bounds_map[symbol]["min_weight"] - 1e-8 <= weights[symbol] <= bounds_map[symbol]["max_weight"] + 1e-8
                for symbol in symbols
            ),
        },
        "target_budget": round(budget, 8),
        "weight_bounds": bounds_map,
        "blocked_reasons": [],
        "soft_constraints": ["concentration", "turnover", "crowding", "bucket_exposure_descriptive_only"],
        "solver": {"success": bool(result.success), "message": solver_message, "iterations": int(getattr(result, "nit", 0) or 0)},
        "fallback_used": fallback_used,
        "explanations": explanations,
        "fallback_policy": "hierarchical_risk_parity_then_inverse_volatility_then_keep_current_positions_at_execution_gate",
    }
