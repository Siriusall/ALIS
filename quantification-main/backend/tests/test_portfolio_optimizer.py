from app.services.two_group_optimizer import two_group_cvar_optimizer


def _returns(symbols: list[str]) -> dict[str, list[float]]:
    base = [0.01, -0.012, 0.006, -0.003, 0.008, -0.004, 0.005, -0.002]
    return {symbol: [value * (1 + index * 0.05) for value in base] for index, symbol in enumerate(symbols)}


def test_single_offensive_name_does_not_make_portfolio_infeasible() -> None:
    symbols = ["DEF1", "DEF2", "DEF3", "OFF1"]
    result = two_group_cvar_optimizer(
        _returns(symbols),
        buckets={"DEF1": "defensive", "DEF2": "medium", "DEF3": "liquidity", "OFF1": "narrative"},
    )

    assert result["ok"] is True
    assert result["blocked_reasons"] == []
    assert abs(sum(result["weights"].values()) - 1.0) < 1e-6
    assert all(weight >= 0 for weight in result["weights"].values())


def test_bucket_labels_do_not_change_the_feasible_set() -> None:
    observations = _returns(["A", "B", "C"])
    all_defensive = two_group_cvar_optimizer(observations, buckets={key: "defensive" for key in observations})
    all_offensive = two_group_cvar_optimizer(observations, buckets={key: "narrative" for key in observations})

    assert all_defensive["ok"] is True
    assert all_offensive["ok"] is True
    assert all_defensive["weights"] == all_offensive["weights"]


def test_missing_returns_has_explicit_non_solver_status() -> None:
    result = two_group_cvar_optimizer({})

    assert result["ok"] is False
    assert result["status"] == "insufficient_data"
    assert result["blocked_reasons"] == ["missing_returns"]


def test_transaction_cost_discourages_unnecessary_turnover() -> None:
    observations = _returns(["A", "B"])
    current = {"A": 0.8, "B": 0.2}
    cheap = two_group_cvar_optimizer(observations, current_weights=current, trading_costs={"A": 0.0, "B": 0.0})
    expensive = two_group_cvar_optimizer(observations, current_weights=current, trading_costs={"A": 1.0, "B": 1.0})
    cheap_turnover = sum(abs(cheap["weights"][symbol] - current[symbol]) for symbol in current)
    expensive_turnover = sum(abs(expensive["weights"][symbol] - current[symbol]) for symbol in current)

    assert expensive_turnover <= cheap_turnover + 1e-8
