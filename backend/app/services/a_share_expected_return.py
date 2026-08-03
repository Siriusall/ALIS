from __future__ import annotations

import math
from typing import Any


EXPECTED_RETURN_SCHEMA_VERSION = "alis_a_share_expected_return_20d_v1"
MODEL_COMPONENT_SCALES = {
    "medium_horizon_lambdarank_v1": 0.040,
    "capital_persistence_multitask_v1": 0.020,
    "leader_lambdarank_v1": 0.015,
    "event_impact_assessor_v2": 0.012,
}
ENVIRONMENT_PRIORS = {
    "S0": 0.015,
    "S1": 0.0025,
    "S2": -0.015,
    "S3": -0.0075,
}


def _safe(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _percentiles(values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    if not ordered:
        return {}
    denominator = max(1, len(ordered) - 1)
    result: dict[str, float] = {}
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end][1] == ordered[start][1]:
            end += 1
        percentile = ((start + end - 1) / 2.0) / denominator
        for index in range(start, end):
            result[ordered[index][0]] = percentile
        start = end
    return result


def _factor_signal(selection: dict[str, Any]) -> float:
    return sum(
        _safe(item.get("contribution"))
        for item in selection.get("factor_contributions") or []
        if isinstance(item, dict)
    )


def _research_quality(evidence: dict[str, Any]) -> tuple[float, int]:
    scores = [
        _safe(item.get("score"), float("nan"))
        for item in (evidence.get("research_dimensions") or {}).values()
        if isinstance(item, dict) and str(item.get("status") or "") == "ready"
    ]
    scores = [value for value in scores if math.isfinite(value)]
    return ((sum(scores) / len(scores) / 100.0) if scores else 0.5), len(scores)


def _valuation_component(evidence: dict[str, Any]) -> tuple[float, float | None]:
    metrics = evidence.get("raw_metrics") if isinstance(evidence.get("raw_metrics"), dict) else {}
    raw_upside = metrics.get("valuation_base_upside")
    if raw_upside is None:
        return 0.0, None
    upside = _safe(raw_upside)
    # Research stores this metric as a fraction. Positive safety margin helps,
    # while a negative base-case margin becomes an explicit overextension cost.
    return _clip(upside, -0.50, 0.50) * 0.020, upside


def _model_components(selection: dict[str, Any]) -> tuple[dict[str, float], float, int]:
    signals = selection.get("model_signals") if isinstance(selection.get("model_signals"), dict) else {}
    models = signals.get("models") if isinstance(signals.get("models"), dict) else {}
    components: dict[str, float] = {}
    degradation_penalty = 0.0
    available = 0
    for key, scale in MODEL_COMPONENT_SCALES.items():
        model = models.get(key) if isinstance(models.get(key), dict) else {}
        percentile = model.get("percentile")
        if percentile is None or not bool(model.get("point_in_time_available", True)):
            components[key] = 0.0
            continue
        available += 1
        components[key] = (_clip(_safe(percentile), 0.0, 1.0) - 0.5) * scale
        multiplier = _clip(_safe(model.get("weight_multiplier"), 1.0), 0.0, 1.0)
        if str(model.get("status") or "") == "degraded" or multiplier < 1.0:
            degradation_penalty += (1.0 - multiplier) * scale * 0.50
    return components, degradation_penalty, available


def _theme_crowding_penalty(selection: dict[str, Any]) -> tuple[float, str]:
    signals = selection.get("model_signals") if isinstance(selection.get("model_signals"), dict) else {}
    narrative = signals.get("narrative") if isinstance(signals.get("narrative"), dict) else {}
    lifecycle = str(narrative.get("lifecycle") or "未识别")
    penalty = {
        "成熟/拥挤": 0.010,
        "亢奋": 0.010,
        "分歧": 0.007,
    }.get(lifecycle, 0.0)
    return penalty, lifecycle


def build_a_share_expected_return_contract(
    *,
    symbols: list[str],
    selections: dict[str, dict[str, Any]],
    research_evidence: dict[str, dict[str, Any]],
    returns_by_symbol: dict[str, list[float]],
    market_state: str,
    trading_costs: dict[str, float],
) -> dict[str, Any]:
    """Build a conservative, auditable 20-session A-share return prior.

    This is a rank-to-return contract, not a promise. Coefficients remain
    conservative priors until point-in-time forward outcomes can calibrate them.
    Transaction cost is exposed separately because the optimizer charges it by
    actual weight change.
    """

    ordered_symbols = list(dict.fromkeys(str(symbol) for symbol in symbols if str(symbol)))
    factor_percentiles = _percentiles({
        symbol: _factor_signal(selections.get(symbol) or {})
        for symbol in ordered_symbols
        if symbol in selections
    })
    fused_percentiles = _percentiles({
        symbol: _safe((selections.get(symbol) or {}).get("score"))
        for symbol in ordered_symbols
        if symbol in selections
    })
    environment_prior = ENVIRONMENT_PRIORS.get(str(market_state or "S1"), ENVIRONMENT_PRIORS["S1"])
    items: list[dict[str, Any]] = []
    optimizer_expected: dict[str, float] = {}
    for symbol in ordered_symbols:
        selection = selections.get(symbol) or {}
        research = research_evidence.get(symbol) or {}
        model_components, degradation_penalty, model_count = _model_components(selection)
        factor_pct = factor_percentiles.get(symbol, 0.5)
        fused_pct = fused_percentiles.get(symbol, 0.5)
        factor_component = (factor_pct - 0.5) * 0.040
        residual_rank_component = (fused_pct - 0.5) * 0.015
        research_quality, ready_dimensions = _research_quality(research)
        research_component = (research_quality - 0.5) * 0.015
        valuation_component, valuation_upside = _valuation_component(research)
        theme_penalty, theme_lifecycle = _theme_crowding_penalty(selection)
        history_count = len(returns_by_symbol.get(symbol) or [])
        reliability = _clip(
            0.30
            + min(0.28, model_count * 0.07)
            + min(0.15, ready_dimensions * 0.03)
            + (0.12 if history_count >= 40 else 0.06 if history_count >= 20 else 0.0),
            0.30,
            0.85,
        )
        alpha_components = {
            "factor_combination": factor_component,
            "residual_fused_rank": residual_rank_component,
            "research_quality": research_component,
            "valuation_safety_margin": valuation_component,
            **model_components,
        }
        raw_alpha = sum(alpha_components.values())
        gross_before_penalties = environment_prior + reliability * raw_alpha
        expected_before_trade_cost = _clip(
            gross_before_penalties - degradation_penalty - theme_penalty,
            -0.10,
            0.12,
        )
        one_way_cost = max(0.0, _safe(trading_costs.get(symbol)))
        entry_net = expected_before_trade_cost - one_way_cost
        optimizer_expected[symbol] = round(expected_before_trade_cost, 8)
        items.append({
            "symbol": symbol,
            "expected_20d_return_before_trade_cost": round(expected_before_trade_cost, 8),
            "estimated_entry_net_20d_return": round(entry_net, 8),
            "environment_prior": round(environment_prior, 8),
            "alpha_before_reliability": round(raw_alpha, 8),
            "reliability": round(reliability, 8),
            "components": {key: round(value, 8) for key, value in alpha_components.items()},
            "penalties": {
                "model_degradation": round(degradation_penalty, 8),
                "theme_crowding": round(theme_penalty, 8),
                "one_way_trade_cost_if_entered": round(one_way_cost, 8),
            },
            "evidence": {
                "factor_percentile": round(factor_pct, 8),
                "fused_rank_percentile": round(fused_pct, 8),
                "available_model_count": model_count,
                "ready_research_dimensions": ready_dimensions,
                "valuation_base_upside": valuation_upside,
                "theme_lifecycle": theme_lifecycle,
                "return_history_count": history_count,
            },
        })
    return {
        "schema_version": EXPECTED_RETURN_SCHEMA_VERSION,
        "horizon_sessions": 20,
        "market_state": str(market_state or "S1"),
        "optimizer_expected_returns": optimizer_expected,
        "items": items,
        "calibration": {
            "status": "conservative_prior_pending_point_in_time_forward_calibration",
            "coefficient_policy": "bounded_cross_sectional_percentiles_with_reliability_shrinkage",
            "minimum_future_outcomes_before_recalibration": 20,
            "direct_production_refit_forbidden": True,
        },
        "cost_policy": {
            "optimizer_input_is_before_trade_cost": True,
            "optimizer_charges_cost_by_absolute_weight_change": True,
            "estimated_entry_net_is_explanation_only": True,
            "double_cost_subtraction_forbidden": True,
        },
        "a_share_evidence_basis": [
            {
                "title": "Size and value in China",
                "doi": "10.1016/j.jfineco.2019.03.008",
                "implication": "exclude shell-like microcaps and prefer earnings-price evidence over mechanical book-to-market replication",
            },
            {
                "title": "Replicating and Digesting Anomalies in the Chinese A-Share Market",
                "doi": "10.1287/mnsc.2023.4904",
                "implication": "avoid equal-weight microcap-driven anomalies and require capacity-aware point-in-time validation",
            },
        ],
    }
