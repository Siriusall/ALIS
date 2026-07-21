from __future__ import annotations

import math
from typing import Any

from app.services.quant_risk import constrained_cvar_weights, historical_cvar, hrp_fallback_weights


STABLE_BUCKETS = {"medium", "defensive", "liquidity"}
OFFENSIVE_BUCKETS = {"narrative", "leader", "event"}


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _normalize(weights: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, float(v)) for v in weights.values()) or 1.0
    return {key: round(max(0.0, float(value)) / total, 8) for key, value in weights.items()}


def _scale_group(base: dict[str, float], symbols: list[str], target: float) -> dict[str, float]:
    subtotal = sum(base.get(symbol, 0.0) for symbol in symbols)
    if subtotal <= 0:
        equal = target / max(1, len(symbols))
        return {symbol: equal for symbol in symbols}
    return {symbol: base.get(symbol, 0.0) / subtotal * target for symbol in symbols}


def two_group_cvar_optimizer(
    returns_by_symbol: dict[str, list[float]],
    *,
    expected_returns: dict[str, float] | None = None,
    buckets: dict[str, str] | None = None,
    current_weights: dict[str, float] | None = None,
    trading_costs: dict[str, float] | None = None,
    crowding: dict[str, float] | None = None,
    max_single_mid: float = 0.10,
    max_single_offensive: float = 0.08,
) -> dict[str, Any]:
    observations = {str(k): [float(x) for x in v] for k, v in returns_by_symbol.items() if v}
    if not observations:
        return {
            "ok": False,
            "schema_version": "two_group_cvar_optimizer_v1",
            "optimizer": "five_bucket_center_weight_risk_parity_fallback_v1",
            "weights": {},
            "blocked_reasons": ["missing_returns"],
        }
    bucket_map = {symbol: str((buckets or {}).get(symbol) or "narrative") for symbol in observations}
    stable_symbols = [symbol for symbol, bucket in bucket_map.items() if bucket in STABLE_BUCKETS]
    offensive_symbols = [symbol for symbol, bucket in bucket_map.items() if bucket in OFFENSIVE_BUCKETS]
    if not stable_symbols:
        stable_symbols = [symbol for symbol in observations if bucket_map[symbol] == "defensive"]
    if not offensive_symbols:
        offensive_symbols = [symbol for symbol in observations if symbol not in stable_symbols]
    base = constrained_cvar_weights(observations, max_single=0.25) or hrp_fallback_weights(observations, max_single=0.25)
    weights: dict[str, float] = {}
    weights.update(_scale_group(base, stable_symbols, 0.50 if stable_symbols else 0.0))
    weights.update(_scale_group(base, offensive_symbols, 0.50 if offensive_symbols else 0.0))
    max_caps = {
        symbol: max_single_mid if bucket_map.get(symbol) in {"medium", "defensive", "liquidity"} else max_single_offensive
        for symbol in weights
    }
    capped = {symbol: min(value, max_caps[symbol]) for symbol, value in weights.items()}
    weights = _normalize(capped)
    stable_weight = sum(value for symbol, value in weights.items() if bucket_map.get(symbol) in STABLE_BUCKETS)
    offensive_weight = sum(value for symbol, value in weights.items() if bucket_map.get(symbol) in OFFENSIVE_BUCKETS)
    risk_by_symbol = {symbol: abs(float(historical_cvar(observations[symbol]).get("cvar") or 0.0)) for symbol in weights}
    total_risk = sum(weights[symbol] * risk_by_symbol[symbol] for symbol in weights) or 1.0
    explanations = {}
    objective_terms = {"expected_net_return": 0.0, "cvar95": 0.0, "turnover_cost": 0.0, "crowding_penalty": 0.0}
    for symbol, weight in weights.items():
        exp = float((expected_returns or {}).get(symbol) or 0.0)
        cost = abs(weight - float((current_weights or {}).get(symbol) or 0.0)) * float((trading_costs or {}).get(symbol) or 0.0)
        crowd = weight * float((crowding or {}).get(symbol) or 0.0)
        cvar_contribution = weight * risk_by_symbol[symbol] / total_risk
        objective_terms["expected_net_return"] += weight * exp
        objective_terms["turnover_cost"] += cost
        objective_terms["crowding_penalty"] += crowd
        explanations[symbol] = {
            "weight": round(weight, 8),
            "function_bucket": bucket_map.get(symbol),
            "expected_return_contribution": round(weight * exp, 8),
            "cvar_contribution": round(cvar_contribution, 8),
            "trading_cost": round(cost, 8),
            "crowding_penalty": round(crowd, 8),
            "constraint_shadow_price": round(max(0.0, base.get(symbol, 0.0) - max_caps[symbol]), 8),
        }
    objective_terms["cvar95"] = sum(weights[symbol] * risk_by_symbol[symbol] for symbol in weights)
    objective = (
        objective_terms["expected_net_return"]
        - 2.0 * objective_terms["cvar95"]
        - 0.5 * objective_terms["turnover_cost"]
        - 0.5 * objective_terms["crowding_penalty"]
    )
    gates = {
        "stable_group_45_55": 0.45 <= stable_weight <= 0.55 or not stable_symbols or not offensive_symbols,
        "offensive_group_45_55": 0.45 <= offensive_weight <= 0.55 or not stable_symbols or not offensive_symbols,
        "weight_sum_100": math.isclose(sum(weights.values()), 1.0, abs_tol=1e-6),
        "long_only": all(value >= -1e-12 for value in weights.values()),
        "single_name_caps": all(weights[symbol] <= max_caps[symbol] + 1e-8 for symbol in weights),
    }
    blocked = [key for key, passed in gates.items() if not passed]
    return {
        "ok": not blocked,
        "schema_version": "two_group_cvar_optimizer_v1",
        "optimizer": "rockafellar_uryasev_cvar95_contract_v1",
        "objective": "max_expected_net_return_minus_2_cvar95_minus_0_5_turnover_minus_0_5_crowding",
        "objective_value": round(objective, 8),
        "objective_terms": {key: round(value, 8) for key, value in objective_terms.items()},
        "weights": weights,
        "weight_sum": round(sum(weights.values()), 8),
        "risk_groups": {"stable": round(stable_weight, 8), "offensive": round(offensive_weight, 8)},
        "gates": gates,
        "blocked_reasons": blocked,
        "explanations": explanations,
        "fallback_policy": "if_unavailable_use_five_bucket_center_weight_plus_in_bucket_risk_parity_never_all_cash",
    }
