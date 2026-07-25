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


def _evaluate(
    *,
    method: str,
    weights: dict[str, float],
    returns: dict[str, list[float]],
    expected_returns: dict[str, float],
    current_weights: dict[str, float],
    trading_costs: dict[str, float],
    equity_budget: float,
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
    objective = expected_20d - 1.25 * cvar_20d - cost - 0.10 * volatility_daily * math.sqrt(20.0)
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
) -> dict[str, Any]:
    """Choose among CVaR, equal weight, HRP and holding current positions."""
    symbols = sorted(symbol for symbol, values in returns_by_symbol.items() if values)
    budget = max(0.0, min(1.0, _safe(equity_budget)))
    equal = {symbol: 1.0 / len(symbols) for symbol in symbols} if symbols else {}
    hrp = hrp_fallback_weights({symbol: returns_by_symbol[symbol] for symbol in symbols}, max_single=0.25)
    candidates = {
        "empirical_cvar": _normalise_to_budget(cvar_weights, budget),
        "equal_weight": _normalise_to_budget(equal, budget),
        "hrp": _normalise_to_budget(hrp, budget),
        "hold_current": {
            str(symbol): max(0.0, _safe(value))
            for symbol, value in current_weights.items()
            if _safe(value) > 0
        },
    }
    evaluations = [
        _evaluate(
            method=method,
            weights=weights,
            returns=returns_by_symbol,
            expected_returns=expected_returns,
            current_weights=current_weights,
            trading_costs=trading_costs,
            equity_budget=budget,
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
        "schema_version": "portfolio_method_competition_v1",
        "winner": winner,
        "selected_method": str((winner or {}).get("method") or ""),
        "weights": dict((winner or {}).get("weights") or {}),
        "evaluations": evaluations,
        "policy": {
            "methods": ["empirical_cvar", "equal_weight", "hrp", "hold_current"],
            "risk_penalty": "1.25 * CVaR20",
            "cost_gate": "winner objective edge must exceed max(0.10%, estimated cost)",
            "simulation_only": True,
        },
    }
