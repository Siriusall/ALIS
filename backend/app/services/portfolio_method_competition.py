from __future__ import annotations

import math
from statistics import mean
from typing import Any

from app.services.quant_risk import hrp_fallback_weights


def _safe(value: Any) -> float:
    try:
        number = float(value or 0.0)
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _normalise_to_budget(weights: dict[str, float], budget: float) -> dict[str, float]:
    positive = {str(key): max(0.0, _safe(value)) for key, value in weights.items() if _safe(value) > 0}
    total = sum(positive.values())
    if total <= 0 or budget <= 0:
        return {}
    return {key: value / total * budget for key, value in positive.items()}


def _apply_weight_bounds(
    weights: dict[str, float],
    *,
    symbols: list[str],
    bounds: dict[str, dict[str, Any]],
    budget: float,
) -> dict[str, float]:
    result = {
        symbol: max(
            _safe((bounds.get(symbol) or {}).get("min_weight")),
            min(
                _safe((bounds.get(symbol) or {}).get("max_weight")),
                max(0.0, _safe(weights.get(symbol))),
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
                symbol: max(0.0, _safe((bounds.get(symbol) or {}).get("max_weight")) - result[symbol])
                for symbol in symbols
            }
        else:
            room = {
                symbol: max(0.0, result[symbol] - _safe((bounds.get(symbol) or {}).get("min_weight")))
                for symbol in symbols
            }
        room_total = sum(room.values())
        if room_total <= 1e-12:
            break
        amount = min(abs(gap), room_total)
        direction = 1.0 if gap > 0 else -1.0
        for symbol in symbols:
            result[symbol] += direction * amount * room[symbol] / room_total
    return {symbol: value for symbol, value in result.items() if value > 1e-12}


def _apply_hold_bounds(
    current_weights: dict[str, float],
    *,
    symbols: list[str],
    bounds: dict[str, dict[str, Any]],
) -> dict[str, float]:
    """Apply mandatory Controller reductions without creating a rebalance."""
    result: dict[str, float] = {}
    for symbol in symbols:
        current = max(0.0, _safe(current_weights.get(symbol)))
        lower = max(0.0, _safe((bounds.get(symbol) or {}).get("min_weight")))
        upper = max(lower, _safe((bounds.get(symbol) or {}).get("max_weight")))
        value = max(lower, min(upper, current))
        if value > 1e-12:
            result[symbol] = value
    return result


def _portfolio_returns(weights: dict[str, float], returns: dict[str, list[float]]) -> list[float]:
    available = [symbol for symbol in weights if returns.get(symbol)]
    common = min((len(returns[symbol]) for symbol in available), default=0)
    if common < 2:
        return []
    return [
        sum(_safe(weights.get(symbol)) * _safe(returns[symbol][-common + index]) for symbol in available)
        for index in range(common)
    ]


def _max_drawdown(values: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    drawdown = 0.0
    for value in values:
        equity *= 1.0 + value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1.0)
    return drawdown


def _industry_budget_penalty(
    weights: dict[str, float],
    *,
    symbol_industries: dict[str, str],
    industry_targets: dict[str, float],
    penalty_rate: float,
) -> tuple[float, dict[str, float]]:
    actual: dict[str, float] = {}
    for symbol, weight in weights.items():
        industry = str(symbol_industries.get(symbol) or "未分类")
        actual[industry] = actual.get(industry, 0.0) + max(0.0, _safe(weight))
    deviation = sum(
        abs(actual.get(industry, 0.0) - max(0.0, _safe(industry_targets.get(industry))))
        for industry in set(actual) | set(industry_targets)
    )
    return max(0.0, float(penalty_rate)) * deviation, {
        key: round(value, 8) for key, value in actual.items()
    }


def _evaluate(
    *,
    method: str,
    weights: dict[str, float],
    returns: dict[str, list[float]],
    expected_returns: dict[str, float],
    current_weights: dict[str, float],
    trading_costs: dict[str, float],
    equity_budget: float,
    weight_bounds: dict[str, dict[str, Any]],
    symbol_industries: dict[str, str],
    industry_targets: dict[str, float],
    industry_penalty_rate: float,
) -> dict[str, Any]:
    series = _portfolio_returns(weights, returns)
    ordered = sorted(series)
    tail_n = max(1, math.ceil(len(ordered) * 0.05)) if ordered else 1
    cvar_daily = max(0.0, -mean(ordered[:tail_n])) if ordered else 1.0
    expected_20d = sum(_safe(weights.get(symbol)) * _safe(expected_returns.get(symbol)) for symbol in weights)
    turnover = sum(
        abs(_safe(weights.get(symbol)) - _safe(current_weights.get(symbol)))
        for symbol in set(weights) | set(current_weights)
    )
    cost = sum(
        abs(_safe(weights.get(symbol)) - _safe(current_weights.get(symbol)))
        * (_safe(trading_costs.get(symbol)) or 0.0015)
        for symbol in set(weights) | set(current_weights)
    )
    volatility_daily = (
        math.sqrt(sum((value - mean(series)) ** 2 for value in series) / len(series))
        if series else 1.0
    )
    cvar_20d = cvar_daily * math.sqrt(20.0)
    industry_penalty, industry_weights = _industry_budget_penalty(
        weights,
        symbol_industries=symbol_industries,
        industry_targets=industry_targets,
        penalty_rate=industry_penalty_rate,
    )
    objective = (
        expected_20d
        - 1.25 * cvar_20d
        - cost
        - 0.10 * volatility_daily * math.sqrt(20.0)
        - industry_penalty
    )
    breaches: list[str] = []
    if any(value < -1e-12 for value in weights.values()):
        breaches.append("negative_weight")
    # Holding an existing position is a no-trade safety baseline.  A newly
    # reduced risk budget may make the existing book larger than the desired
    # budget, but it must not silently turn "hold current" into liquidation.
    if method != "hold_current" and sum(weights.values()) > equity_budget + 1e-6:
        breaches.append("equity_budget")
    if method != "hold_current" and max(weights.values(), default=0.0) > min(0.25, equity_budget) + 1e-6:
        breaches.append("single_position")
    for symbol in weight_bounds:
        value = _safe(weights.get(symbol))
        lower = _safe((weight_bounds.get(symbol) or {}).get("min_weight"))
        upper = _safe((weight_bounds.get(symbol) or {}).get("max_weight"))
        if value < lower - 1e-8 or value > upper + 1e-8:
            breaches.append(f"controller_bound:{symbol}")
    if len(series) < 20:
        breaches.append("return_history_lt_20")
    return {
        "method": method,
        "eligible": not breaches,
        "breaches": breaches,
        "weights": {key: round(value, 8) for key, value in weights.items()},
        "expected_20d_return": round(expected_20d, 8),
        "cvar_20d": round(cvar_20d, 8),
        "volatility_20d": round(volatility_daily * math.sqrt(20.0), 8),
        "max_drawdown_history": round(_max_drawdown(series), 8),
        "turnover": round(turnover, 8),
        "estimated_one_way_cost": round(cost, 8),
        "industry_weights": industry_weights,
        "industry_budget_penalty": round(industry_penalty, 8),
        "objective": round(objective, 8),
        "sample_count": len(series),
    }


def compete_portfolio_methods(
    *,
    returns_by_symbol: dict[str, list[float]],
    expected_returns: dict[str, float],
    current_weights: dict[str, float],
    trading_costs: dict[str, float],
    equity_budget: float,
    cvar_weights: dict[str, float],
    weight_bounds: dict[str, dict[str, Any]] | None = None,
    symbol_industries: dict[str, str] | None = None,
    industry_targets: dict[str, float] | None = None,
    industry_penalty_rate: float = 0.01,
) -> dict[str, Any]:
    """Choose among CVaR, equal weight, HRP and holding current positions."""
    symbols = sorted(symbol for symbol, values in returns_by_symbol.items() if values)
    bounds_enabled = weight_bounds is not None
    raw_bounds = dict(weight_bounds or {})
    frozen_symbol_industries = {
        str(symbol): str(industry or "未分类")
        for symbol, industry in (symbol_industries or {}).items()
    }
    frozen_industry_targets = {
        str(industry): max(0.0, _safe(weight))
        for industry, weight in (industry_targets or {}).items()
    }
    bounds = {
        symbol: {
            "min_weight": max(0.0, _safe((raw_bounds.get(symbol) or {}).get("min_weight"))),
            "max_weight": max(0.0, _safe((raw_bounds.get(symbol) or {}).get("max_weight", 1.0))),
            "action": str((raw_bounds.get(symbol) or {}).get("action") or ""),
        }
        for symbol in symbols
    }
    minimum_budget = sum(item["min_weight"] for item in bounds.values())
    maximum_budget = sum(item["max_weight"] for item in bounds.values())
    requested_budget = max(0.0, min(1.0, _safe(equity_budget)))
    budget = max(minimum_budget, min(maximum_budget, requested_budget)) if bounds_enabled else requested_budget
    equal = {symbol: 1.0 / len(symbols) for symbol in symbols} if symbols else {}
    hrp = hrp_fallback_weights({symbol: returns_by_symbol[symbol] for symbol in symbols}, max_single=0.25)
    raw_candidates = {
        "empirical_cvar": _normalise_to_budget(cvar_weights, budget),
        "equal_weight": _normalise_to_budget(equal, budget),
        "hrp": _normalise_to_budget(hrp, budget),
        "hold_current": {
            str(symbol): max(0.0, _safe(value))
            for symbol, value in current_weights.items()
            if _safe(value) > 0
        },
    }
    if bounds_enabled:
        candidates = {
            method: (
                _apply_hold_bounds(weights, symbols=symbols, bounds=bounds)
                if method == "hold_current"
                else _apply_weight_bounds(
                    weights,
                    symbols=symbols,
                    bounds=bounds,
                    budget=budget,
                )
            )
            for method, weights in raw_candidates.items()
        }
    else:
        candidates = raw_candidates
    evaluations = [
        _evaluate(
            method=method,
            weights=weights,
            returns=returns_by_symbol,
            expected_returns=expected_returns,
            current_weights=current_weights,
            trading_costs=trading_costs,
            equity_budget=budget,
            weight_bounds=bounds if bounds_enabled else {},
            symbol_industries=frozen_symbol_industries,
            industry_targets=frozen_industry_targets,
            industry_penalty_rate=industry_penalty_rate,
        )
        for method, weights in candidates.items()
    ]
    eligible = [item for item in evaluations if item["eligible"]]
    hold = next((item for item in evaluations if item["method"] == "hold_current"), None)
    winner = max(eligible, key=lambda item: (item["objective"], -item["turnover"], item["method"])) if eligible else hold
    if winner and hold and hold["eligible"] and winner["method"] != "hold_current":
        required_edge = max(0.001, float(winner["estimated_one_way_cost"]))
        if float(winner["objective"]) - float(hold["objective"]) <= required_edge:
            winner = hold
    return {
        "ok": bool(winner),
        "schema_version": "portfolio_method_competition_v2",
        "winner": winner,
        "selected_method": str((winner or {}).get("method") or ""),
        "weights": dict((winner or {}).get("weights") or {}),
        "evaluations": evaluations,
        "weight_bounds": bounds,
        "industry_budget": {
            "schema_version": "alis_portfolio_industry_budget_v1",
            "symbol_industries": frozen_symbol_industries,
            "industry_targets": {
                key: round(value, 8) for key, value in frozen_industry_targets.items()
            },
            "penalty_rate": round(max(0.0, float(industry_penalty_rate)), 8),
            "application": "soft_deviation_penalty_applied_to_every_competing_method",
        },
        "target_budget": round(budget, 8),
        "policy": {
            "methods": ["empirical_cvar", "equal_weight", "hrp", "hold_current"],
            "risk_penalty": "1.25 * CVaR20",
            "cost_gate": "winner objective edge must exceed max(0.10%, estimated cost)",
            "industry_budget": "every method pays the same frozen industry-budget deviation penalty",
            "simulation_only": True,
        },
    }
