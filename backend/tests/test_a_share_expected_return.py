from app.services.a_share_expected_return import build_a_share_expected_return_contract


def _selection(score: float) -> dict:
    return {
        "score": score,
        "factor_contributions": [{"factor": "momentum_20", "contribution": score / 100.0}],
        "model_signals": {"models": {}},
    }


def test_expected_return_contract_charges_cost_only_as_execution_explanation():
    inputs = {
        "symbols": ["000001", "600000"],
        "selections": {"000001": _selection(80), "600000": _selection(20)},
        "research_evidence": {
            "000001": {"research_dimensions": {}, "raw_metrics": {}},
            "600000": {"research_dimensions": {}, "raw_metrics": {}},
        },
        "returns_by_symbol": {"000001": [0.001] * 60, "600000": [0.001] * 60},
        "market_state": "S1",
    }
    cheap = build_a_share_expected_return_contract(
        **inputs,
        trading_costs={"000001": 0.001, "600000": 0.001},
    )
    expensive = build_a_share_expected_return_contract(
        **inputs,
        trading_costs={"000001": 0.01, "600000": 0.01},
    )

    assert cheap["optimizer_expected_returns"] == expensive["optimizer_expected_returns"]
    cheap_item = next(item for item in cheap["items"] if item["symbol"] == "000001")
    expensive_item = next(item for item in expensive["items"] if item["symbol"] == "000001")
    assert cheap_item["estimated_entry_net_20d_return"] > expensive_item["estimated_entry_net_20d_return"]
    assert cheap["cost_policy"]["double_cost_subtraction_forbidden"] is True


def test_expected_return_uses_cross_sectional_rank_instead_of_raw_microcap_return_extrapolation():
    result = build_a_share_expected_return_contract(
        symbols=["000001", "600000"],
        selections={"000001": _selection(90), "600000": _selection(10)},
        research_evidence={},
        returns_by_symbol={"000001": [0.20] * 60, "600000": [-0.20] * 60},
        market_state="S1",
        trading_costs={},
    )

    assert result["optimizer_expected_returns"]["000001"] <= 0.12
    assert result["optimizer_expected_returns"]["600000"] >= -0.10
    assert result["optimizer_expected_returns"]["000001"] > result["optimizer_expected_returns"]["600000"]
