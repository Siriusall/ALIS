import pytest

from app.services.market_context import _transparent_regime, bocpd_change_alarm, score_market_sentiment, score_theme_narratives
from app.services.strategy_gate import evaluate_strategy_gate


def test_sentiment_uses_plan_weights_and_crowding_penalty() -> None:
    result = score_market_sentiment(breadth=.7, average_return=.02, volume_ratio=1.2, limit_up_ratio=.03, limit_down_ratio=.01, leader_persistence=.8, crowding=.5)
    assert result["weights"]["leader_persistence"] == .15
    assert result["crowding_penalty"] == 10
    assert result["score"] == pytest.approx(result["base_score"] - 10)


def test_theme_requires_evidence_gate_even_when_score_is_high() -> None:
    result = score_theme_narratives([{"theme":"测试","strength":1,"price_confirmation":1,"breadth":1,"persistence":1,"fundamental_verifiability":1,"catalyst_duration":1,"source_diversity":1,"source_types":1,"related_instruments":20,"sessions":20}])[0]
    assert result["promotable"] is False


def test_theme_requires_score_threshold_after_evidence_gate() -> None:
    result = score_theme_narratives([{
        "theme": "低强度双来源", "strength": .2, "price_confirmation": .2,
        "breadth": .2, "persistence": .2, "fundamental_verifiability": .2,
        "catalyst_duration": .2, "source_diversity": .8, "source_types": 2,
        "related_instruments": 20, "sessions": 20,
    }])[0]
    assert result["evidence_qualified"] is True
    assert result["promotable"] is False
    assert result["state"] == "watch"


def test_strategy_gate_cannot_average_away_hard_risk() -> None:
    result = evaluate_strategy_gate(research_score=95,confidence=.95,data_quality=100,risk_eligible=False,market_state="bull",sentiment_score=90,expectation_score=90,valuation_score=90,theme_score=90,theme_verified=True)
    assert result["admitted"] is False
    assert result["risk_budget_multiplier"] == 0


def test_bear_market_changes_budget_not_research_fact() -> None:
    bull = evaluate_strategy_gate(research_score=80,confidence=.8,data_quality=90,risk_eligible=True,market_state="bull",sentiment_score=60,expectation_score=70,valuation_score=70)
    bear = evaluate_strategy_gate(research_score=80,confidence=.8,data_quality=90,risk_eligible=True,market_state="bear",sentiment_score=60,expectation_score=70,valuation_score=70)
    assert bull["admitted"] and bear["admitted"]
    assert bear["risk_budget_multiplier"] < bull["risk_budget_multiplier"]
    assert bear["safety_margin_multiplier"] > bull["safety_margin_multiplier"]


def test_dynamic_safety_margin_blocks_expensive_candidate() -> None:
    result = evaluate_strategy_gate(
        research_score=80, confidence=.8, data_quality=90, risk_eligible=True,
        market_state="bear", sentiment_score=40, expectation_score=70,
        valuation_score=70, valuation_upside=.12,
    )
    assert result["required_safety_margin"] == pytest.approx(.135)
    assert result["admitted"] is False
    assert result["risk_budget_multiplier"] == 0
    assert result["cash_target"] == 1


def test_fixed_four_market_states_and_constitution_multipliers() -> None:
    assert _transparent_regime(70, .01) == "S0"
    assert _transparent_regime(35, -.01) == "S2"
    assert _transparent_regime(55, .01, "S2") == "S3"
    assert _transparent_regime(50, 0) == "S1"
    gates = {
        state: evaluate_strategy_gate(
            research_score=80, confidence=.8, data_quality=90, risk_eligible=True,
            market_state=state, sentiment_score=100, expectation_score=100, valuation_score=100,
        )
        for state in ("S0", "S1", "S2", "S3")
    }
    assert gates["S0"]["risk_budget_multiplier"] == 1.0
    assert gates["S2"]["safety_margin_multiplier"] == 1.35
    assert gates["S3"]["safety_margin_multiplier"] == 1.25


def test_bocpd_extreme_change_triggers_temporary_risk_protection() -> None:
    stable = bocpd_change_alarm([0.0] * 40)
    changed = bocpd_change_alarm([0.0] * 40 + [5.0])
    assert stable["p_change_1d"] < .30
    assert changed["p_change_1d"] >= .80
    assert changed["risk_multiplier"] == .70
    gate = evaluate_strategy_gate(
        research_score=80, confidence=.8, data_quality=90, risk_eligible=True,
        market_state="S0", sentiment_score=100, expectation_score=100,
        valuation_score=100, change_alarm_probability=changed["p_change_1d"],
    )
    assert gate["risk_budget_multiplier"] == .70
    assert gate["change_alarm_multiplier"] == .70
