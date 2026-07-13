import pytest

from app.services.market_context import score_market_sentiment, score_theme_narratives
from app.services.strategy_gate import evaluate_strategy_gate


def test_sentiment_uses_plan_weights_and_crowding_penalty() -> None:
    result = score_market_sentiment(breadth=.7, average_return=.02, volume_ratio=1.2, limit_up_ratio=.03, limit_down_ratio=.01, leader_persistence=.8, crowding=.5)
    assert result["weights"]["leader_persistence"] == .15
    assert result["crowding_penalty"] == 10
    assert result["score"] == pytest.approx(result["base_score"] - 10)


def test_theme_requires_evidence_gate_even_when_score_is_high() -> None:
    result = score_theme_narratives([{"theme":"测试","strength":1,"price_confirmation":1,"breadth":1,"persistence":1,"fundamental_verifiability":1,"catalyst_duration":1,"source_diversity":1,"source_types":1,"related_instruments":20,"sessions":20}])[0]
    assert result["promotable"] is False


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
