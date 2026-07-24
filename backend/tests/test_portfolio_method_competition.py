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
