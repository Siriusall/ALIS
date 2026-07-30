from app.services.portfolio_method_competition import compete_portfolio_methods


def test_method_competition_keeps_current_when_rebalance_edge_does_not_cover_cost():
    result = compete_portfolio_methods(
        returns_by_symbol={
            "000001": [0.001] * 60,
            "000002": [0.001] * 60,
            "000003": [0.001] * 60,
            "000004": [0.001] * 60,
        },
        expected_returns={key: 0.02 for key in ("000001", "000002", "000003", "000004")},
        current_weights={key: 0.25 for key in ("000001", "000002", "000003", "000004")},
        trading_costs={key: 0.0015 for key in ("000001", "000002", "000003", "000004")},
        equity_budget=1.0,
        cvar_weights={"000001": 0.26, "000002": 0.25, "000003": 0.25, "000004": 0.24},
    )
    assert result["selected_method"] == "hold_current"
    assert len(result["evaluations"]) == 4


def test_method_competition_rejects_overconcentrated_candidate():
    result = compete_portfolio_methods(
        returns_by_symbol={
            "000001": [0.01, -0.005] * 30,
            "000002": [0.002, -0.001] * 30,
            "000003": [0.003, -0.001] * 30,
            "000004": [0.004, -0.002] * 30,
        },
        expected_returns={key: 0.03 for key in ("000001", "000002", "000003", "000004")},
        current_weights={},
        trading_costs={key: 0.0015 for key in ("000001", "000002", "000003", "000004")},
        equity_budget=1.0,
        cvar_weights={"000001": 1.0},
    )
    cvar = next(item for item in result["evaluations"] if item["method"] == "empirical_cvar")
    assert cvar["eligible"] is False
    assert "single_position" in cvar["breaches"]
    assert result["selected_method"] in {"equal_weight", "hrp"}


def test_hold_current_preserves_existing_weights_when_new_equity_budget_is_zero():
    current = {"000001": 0.31, "000002": 0.22}
    result = compete_portfolio_methods(
        returns_by_symbol={"000001": [0.001] * 60, "000002": [0.001] * 60},
        expected_returns={"000001": 0.01, "000002": 0.01},
        current_weights=current,
        trading_costs={"000001": 0.0015, "000002": 0.0015},
        equity_budget=0.0,
        cvar_weights={},
    )

    assert result["selected_method"] == "hold_current"
    assert result["weights"] == current
    hold = next(item for item in result["evaluations"] if item["method"] == "hold_current")
    assert hold["turnover"] == 0
    assert hold["eligible"] is True


def test_every_competing_method_obeys_controller_action_bounds():
    bounds = {
        "000001": {"min_weight": 0.0, "max_weight": 0.0, "action": "sell_exit"},
        "000002": {"min_weight": 0.20, "max_weight": 0.20, "action": "hold"},
        "000003": {"min_weight": 0.0, "max_weight": 0.10, "action": "buy_candidate"},
    }
    result = compete_portfolio_methods(
        returns_by_symbol={
            "000001": [0.001, -0.001] * 30,
            "000002": [0.001, -0.001] * 30,
            "000003": [0.002, -0.001] * 30,
        },
        expected_returns={"000001": 0.10, "000002": 0.01, "000003": 0.03},
        current_weights={"000001": 0.30, "000002": 0.20},
        trading_costs={},
        equity_budget=0.30,
        cvar_weights={"000001": 0.8, "000002": 0.1, "000003": 0.1},
        weight_bounds=bounds,
    )

    for evaluation in result["evaluations"]:
        assert evaluation["weights"].get("000001", 0.0) == 0.0
        assert evaluation["weights"].get("000002", 0.0) == 0.20
        assert evaluation["weights"].get("000003", 0.0) <= 0.10
    hold = next(item for item in result["evaluations"] if item["method"] == "hold_current")
    assert hold["weights"] == {"000002": 0.20}


def test_every_competing_method_pays_the_same_frozen_industry_budget_formula():
    industries = {
        "000001": "银行",
        "000002": "银行",
        "000003": "医药",
        "000004": "医药",
    }
    targets = {"银行": 0.8, "医药": 0.2}
    penalty_rate = 0.02
    result = compete_portfolio_methods(
        returns_by_symbol={
            "000001": [0.002, -0.001] * 30,
            "000002": [0.001, -0.001] * 30,
            "000003": [0.003, -0.002] * 30,
            "000004": [0.002, -0.002] * 30,
        },
        expected_returns={symbol: 0.03 for symbol in industries},
        current_weights={symbol: 0.15 for symbol in industries},
        trading_costs={},
        equity_budget=1.0,
        cvar_weights={"000001": 0.7, "000002": 0.3},
        symbol_industries=industries,
        industry_targets=targets,
        industry_penalty_rate=penalty_rate,
    )

    assert result["industry_budget"]["application"] == (
        "soft_deviation_penalty_applied_to_every_competing_method"
    )
    assert {item["method"] for item in result["evaluations"]} == {
        "empirical_cvar", "equal_weight", "hrp", "hold_current",
    }
    for evaluation in result["evaluations"]:
        actual = evaluation["industry_weights"]
        deviation = sum(
            abs(actual.get(industry, 0.0) - targets.get(industry, 0.0))
            for industry in set(actual) | set(targets)
        )
        assert evaluation["industry_budget_penalty"] == round(
            penalty_rate * deviation, 8,
        )
        assert evaluation["industry_budget_penalty"] > 0
