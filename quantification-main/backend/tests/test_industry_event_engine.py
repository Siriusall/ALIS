from app.services.industry_event_engine import score_event, score_industry


def test_industry_requires_coverage_and_history() -> None:
    result = score_industry({"demand_orders_sales": 80, "product_price": 70}, history_points=12)
    assert result["status"] == "ready"


def test_industry_recovery_state_uses_direction() -> None:
    result = score_industry({key: 55 for key in ("demand_orders_sales", "product_price", "inventory_cycle", "capacity_utilization", "profit_diffusion")}, prior_score=45, history_points=12)
    assert result["status"] == "ready"
    assert result["lifecycle"] == "复苏"


def test_regulatory_event_decays_more_slowly() -> None:
    common = {"severity": "high", "impact_direction": "negative", "confidence": .9, "source_code": "cninfo", "event_date": "20260101"}
    regulatory = score_event({**common, "event_type": "regulatory_risk"})
    earnings = score_event({**common, "event_type": "earnings"})
    assert regulatory["half_life_days"] > earnings["half_life_days"]
    assert regulatory["decayed_score"] > earnings["decayed_score"]
