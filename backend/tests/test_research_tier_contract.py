from app.services.research_tier_contract import (
    build_tiered_equity_budget,
    classify_core_eligibility,
)


def test_research_quality_failure_is_restricted_but_missing_data_is_hard_blocked():
    restricted = classify_core_eligibility({
        "gates": [
            {"key": "stock_master", "passed": True},
            {"key": "risk", "passed": True},
            {"key": "financial_fields", "passed": True},
            {"key": "research", "passed": False},
            {"key": "valuation", "passed": True},
            {"key": "thesis", "passed": False},
        ],
    })
    hard = classify_core_eligibility({
        "gates": [
            {"key": "stock_master", "passed": True},
            {"key": "risk", "passed": True},
            {"key": "financial_fields", "passed": False},
            {"key": "research", "passed": False},
            {"key": "valuation", "passed": True},
            {"key": "thesis", "passed": False},
        ],
    })

    assert restricted["tier"] == "restricted"
    assert restricted["trading_eligible"] is True
    assert restricted["core_research_eligible"] is False
    assert hard["tier"] == "hard_blocked"
    assert hard["trading_eligible"] is False


def test_valuation_shortfall_is_restricted_and_cannot_supply_core_evidence():
    result = classify_core_eligibility({
        "gates": [
            {"key": "stock_master", "passed": True},
            {"key": "risk", "passed": True},
            {"key": "financial_fields", "passed": True},
            {"key": "valuation", "passed": False},
        ],
    })

    assert result["tier"] == "restricted"
    assert result["trading_eligible"] is True
    assert result["core_research_eligible"] is False
    assert result["research_quality_failed_gates"] == ["valuation"]


def test_s1_restricted_only_budget_is_bounded_and_no_candidate_is_explicit():
    restricted = build_tiered_equity_budget(
        market_state="S1",
        base_equity_budget=.85,
        core_symbols=[],
        restricted_symbols=["000001", "000002"],
    )
    empty = build_tiered_equity_budget(
        market_state="S1",
        base_equity_budget=.85,
        core_symbols=[],
        restricted_symbols=[],
    )

    assert restricted["opportunity_status"] == "restricted_only"
    assert restricted["effective_equity_budget"] == .20
    assert restricted["restricted_symbol_cap"] == .03
    assert empty["opportunity_status"] == "no_qualified_equity_candidate"
    assert empty["effective_equity_budget"] == 0
