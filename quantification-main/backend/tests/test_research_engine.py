from app.services.research_engine import classify_company, evaluate_risk, research_state, score_dimension


def test_hard_risk_cannot_be_averaged_away() -> None:
    result = evaluate_risk({"major_investigation": True, "high_pledge": True})
    assert result["eligible"] is False
    assert result["max_position"] == 0
    assert "重大立案调查" in result["hard_reasons"]


def test_missing_evidence_is_not_scored_as_complete() -> None:
    result = score_dimension("quality", {"profitability": 90, "capital_efficiency": 80})
    assert result["status"] == "insufficient_data"
    assert result["coverage"] == 0.4
    assert "cash_conversion" in result["missing"]


def test_weights_are_renormalized_only_with_disclosed_coverage() -> None:
    result = score_dimension("growth", {
        "revenue_growth": 80,
        "core_profit_growth": 60,
        "three_year_cagr": 70,
        "growth_acceleration": 50,
        "growth_quality": 90,
        "evidence_confidence": 80,
    })
    assert result["status"] == "ready"
    assert result["score"] == 71.0
    assert result["confidence"] == 0.8


def test_company_classification_uses_industry_and_growth() -> None:
    result = classify_company(industry="银行", metrics={"revenue_growth": 10, "core_profit_growth": 8})
    assert result["business_model"] == "金融杠杆型"
    assert result["lifecycle"] == "稳定成长"


def test_research_state_respects_risk_gate() -> None:
    dimensions = {name: {"score": 90, "confidence": .9} for name in ("quality", "growth", "cashflow", "governance", "industry", "expectation", "valuation")}
    result = research_state(dimensions, evaluate_risk({"fraud": True}))
    assert result["classification"] == "风险恶化"
    assert result["eligible"] is False


def test_missing_governance_evidence_does_not_mean_risk_deterioration() -> None:
    dimensions = {name: {"score": 0, "confidence": 0, "status": "insufficient_data"} for name in ("quality", "growth", "cashflow", "governance", "industry", "expectation", "valuation")}
    result = research_state(dimensions, evaluate_risk({}))
    assert result["classification"] == "研究观察"
    assert result["eligible"] is True
