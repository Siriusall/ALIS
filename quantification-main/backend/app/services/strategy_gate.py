from __future__ import annotations

from typing import Any


def evaluate_strategy_gate(
    *, research_score: float, confidence: float, data_quality: float, risk_eligible: bool,
    market_state: str, sentiment_score: float, expectation_score: float, valuation_score: float,
    theme_score: float = 0.0, theme_verified: bool = False,
    valuation_upside: float | None = None, required_base_margin: float = .10,
    change_alarm_probability: float = 0.0,
) -> dict[str, Any]:
    blockers: list[str] = []
    if not risk_eligible: blockers.append("硬风险门控未通过")
    if data_quality < 80: blockers.append("数据质量低于80")
    if confidence < .60: blockers.append("研究置信度低于0.60")
    if research_score < 65: blockers.append("研究准入分低于65")
    admitted = not blockers
    state = str(market_state or "unknown")
    market_multiplier = {"S0": 1.0, "S1": .85, "S2": .55, "S3": .70,
                         "bull": 1.0, "sideways": .85, "bear": .55, "stress": .55, "recovery": .70}.get(state, .55)
    sentiment_multiplier = .70 + max(0.0, min(100.0, sentiment_score)) / 100.0 * .30
    expectation_multiplier = .75 + max(0.0, min(100.0, expectation_score)) / 400.0
    valuation_multiplier = .75 + max(0.0, min(100.0, valuation_score)) / 400.0
    theme_bonus = min(.10, max(0.0, theme_score - 60) / 400.0) if theme_verified else 0.0
    risk_budget_multiplier = 0.0 if not admitted else min(1.0, market_multiplier * sentiment_multiplier * expectation_multiplier * valuation_multiplier + theme_bonus)
    safety_margin_multiplier = 1.35 if state in {"S2", "stress", "bear"} else 1.25 if state in {"S3", "recovery"} else 1.10 if state in {"S1", "sideways"} else 1.0
    required_safety_margin = max(0.0, float(required_base_margin)) * safety_margin_multiplier
    if valuation_upside is not None and float(valuation_upside) < required_safety_margin:
        blockers.append(
            f"基准估值安全边际{float(valuation_upside):.1%}低于动态要求{required_safety_margin:.1%}"
        )
        admitted = False
        risk_budget_multiplier = 0.0
    change_alarm_multiplier = .70 if float(change_alarm_probability or 0) >= .80 else 1.0
    risk_budget_multiplier *= change_alarm_multiplier
    cash_target = round(max(0.05, 1.0 - risk_budget_multiplier), 4)
    return {
        "admitted": admitted, "blockers": blockers, "risk_budget_multiplier": round(risk_budget_multiplier, 4),
        "cash_target": cash_target, "safety_margin_multiplier": safety_margin_multiplier,
        "required_safety_margin": round(required_safety_margin, 6),
        "valuation_upside": None if valuation_upside is None else round(float(valuation_upside), 6),
        "change_alarm_probability": round(float(change_alarm_probability or 0), 6),
        "change_alarm_multiplier": change_alarm_multiplier,
        "theme_used": bool(theme_verified),
        "explanation": "硬门控决定能否进入候选；市场、情绪、预期和估值只调整风险预算，不覆盖企业事实。",
    }
