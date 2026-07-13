from app.services.operations_overview import _deleveraging_actions, _stress_tests


def test_stress_tests_expose_concentration_loss_and_reduction_action():
    exposures = [
        {"symbol": "000001", "industry": "银行", "market_value": 80_000, "weight": .8, "single_name_limit": .15},
        {"symbol": "000002", "industry": "地产", "market_value": 20_000, "weight": .2, "single_name_limit": .15},
    ]
    tests = _stress_tests(exposures, 100_000)
    actions = _deleveraging_actions(exposures, [], tests)

    assert any(item["code"] == "largest_position_down_20pct" and item["severity"] == "error" for item in tests)
    assert {item["symbol"] for item in actions if item["action"] == "reduce"} == {"000001", "000002"}
    assert any(item["action"] == "reduce_risk_budget" for item in actions)
