from __future__ import annotations

from typing import Any


def evaluate_strategy_gate(
    *, research_score: float, confidence: float, data_quality: float, risk_eligible: bool,
    market_state: str, sentiment_score: float, expectation_score: float, valuation_score: float,
    theme_score: float = 0.0, theme_verified: bool = False,
) -> dict[str, Any]:
    blockers: list[str] = []
    if not risk_eligible: blockers.append("硬风险门控未通过")
    if data_quality < 80: blockers.append("数据质量低于80")
    if confidence < .60: blockers.append("研究置信度低于0.60")
    if research_score < 65: blockers.append("研究准入分低于65")
    admitted = not blockers
    state = str(market_state or "unknown")
    market_multiplier = {"bull": 1.0, "recovery": .85, "sideways": .70, "stress": .40, "bear": .35}.get(state, .50)
    sentiment_multiplier = .70 + max(0.0, min(100.0, sentiment_score)) / 100.0 * .30
    expectation_multiplier = .75 + max(0.0, min(100.0, expectation_score)) / 400.0
    valuation_multiplier = .75 + max(0.0, min(100.0, valuation_score)) / 400.0
    theme_bonus = min(.10, max(0.0, theme_score - 60) / 400.0) if theme_verified else 0.0
    risk_budget_multiplier = 0.0 if not admitted else min(1.0, market_multiplier * sentiment_multiplier * expectation_multiplier * valuation_multiplier + theme_bonus)
    cash_target = round(max(0.05, 1.0 - risk_budget_multiplier), 4)
    safety_margin_multiplier = 1.35 if state in {"stress", "bear"} else 1.15 if sentiment_score < 35 else 1.0
    return {
        "admitted": admitted, "blockers": blockers, "risk_budget_multiplier": round(risk_budget_multiplier, 4),
        "cash_target": cash_target, "safety_margin_multiplier": safety_margin_multiplier,
        "theme_used": bool(theme_verified),
        "explanation": "硬门控决定能否进入候选；市场、情绪、预期和估值只调整风险预算，不覆盖企业事实。",
    }
