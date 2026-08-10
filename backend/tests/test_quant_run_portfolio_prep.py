from dataclasses import dataclass

from app.services import quant_run_portfolio_prep as prep


def test_compute_buy_budget_clamps_target_cash_and_ratio():
    assert prep.compute_buy_budget(
        total_equity=200_000,
        position_market_value=80_000,
        cash_balance=90_000,
        target_position_pct=0.75,
        budget_ratio=0.5,
    ) == 35_000
    assert prep.compute_buy_budget(
        total_equity=200_000,
        position_market_value=160_000,
        cash_balance=90_000,
        target_position_pct=0.75,
        budget_ratio=1.0,
    ) == 0
    assert prep.compute_buy_budget(
        total_equity=200_000,
        position_market_value=0,
        cash_balance=50_000,
        target_position_pct=2.0,
        budget_ratio=2.0,
    ) == 50_000


def test_monthly_turnover_budget_only_counts_execution_month_and_caps_remaining():
    operations = [
        {"at": "2026-07-01T09:31:00", "price": 10, "quantity": 1_000},
        {"at": "2026-07-31T14:55:00", "price": 20, "quantity": 500},
        {"at": "2026-06-30T14:55:00", "price": 99, "quantity": 999},
    ]
    assert prep.monthly_turnover_budget("2026-07-15", 200_000, operations) == {
        "limit": 60_000.0,
        "used_before": 20_000.0,
        "remaining": 40_000.0,
    }
    assert prep.monthly_turnover_budget(
        "2026-07-15",
        50_000,
        operations,
    )["remaining"] == 0


def test_pretrade_symbol_limits_distinguish_etf_core_and_research_candidate():
    theses = [
        {
            "ts_code": "600000.SH",
            "derived_confidence": 80,
            "derived_status": "confirmed",
        },
        {
            "ts_code": "000001.SZ",
            "derived_confidence": 74.9,
            "derived_status": "active",
        },
    ]
    limits = prep.pretrade_symbol_limits(
        ["510300.SH", "600000.SH", "000001.SZ", "300001.SZ"],
        max_single=0.30,
        theses=theses,
        is_etf=lambda symbol: symbol.startswith("510"),
    )
    assert limits == {
        "510300.SH": 0.15,
        "600000.SH": 0.15,
        "000001.SZ": 0.15,
        "300001.SZ": 0.15,
    }


def test_selection_score_map_and_strategy_method_are_defensive():
    assert prep.selection_score_map({
        "selections": [
            {"symbol": "600000.SH", "score": 1.25},
            {"symbol": "", "score": 99},
            None,
        ]
    }) == {"600000.SH": 1.25}
    assert prep.strategy_portfolio_method({"content": {"portfolio_method": "HRP"}}) == "hrp"
    assert prep.strategy_portfolio_method({"content": {"portfolio_method": "unknown"}}) == "cvar"
    assert prep.strategy_portfolio_method(None) == "cvar"


def test_base_portfolio_weights_preserves_method_and_fallback_contracts():
    calls = []

    def cvar(values, *, max_single):
        calls.append(("cvar", values, max_single))
        return {"600000.SH": 0.2}

    def hrp(values, *, max_single):
        calls.append(("hrp", values, max_single))
        return {"000001.SZ": 0.2}

    returns = {
        "600000.SH": [0.01, -0.01],
        "000001.SZ": [0.02, 0.01, -0.03],
        "invalid": [0.01],
    }
    equal, equal_name = prep.base_portfolio_weights(
        returns,
        max_single=0.40,
        method="equal_weight",
        constrained_cvar=cvar,
        hrp_fallback=hrp,
    )
    assert equal == {"000001.SZ": 0.4, "600000.SH": 0.4}
    assert equal_name == "equal_weight_baseline_v1"
    assert calls == []

    weights, optimizer = prep.base_portfolio_weights(
        returns,
        max_single=0.20,
        method="cvar",
        constrained_cvar=cvar,
        hrp_fallback=hrp,
    )
    assert weights == {"600000.SH": 0.2}
    assert optimizer == "ledoit_wolf_shrinkage_plus_constrained_cvar_v1"
    assert "invalid" not in calls[-1][1]

    fallback_weights, fallback_name = prep.base_portfolio_weights(
        returns,
        max_single=0.20,
        method="cvar",
        constrained_cvar=lambda *_args, **_kwargs: {},
        hrp_fallback=hrp,
    )
    assert fallback_weights == {"000001.SZ": 0.2}
    assert fallback_name == "hrp_inverse_volatility_fallback_v1"


@dataclass
class _Order:
    quantity: int
    price: float
    commission: float = 0
    transfer_fee: float = 0


def test_order_helpers_only_keep_fills_and_include_all_buy_costs():
    orders = [
        _Order(quantity=100, price=10, commission=5, transfer_fee=1),
        _Order(quantity=0, price=99, commission=0, transfer_fee=0),
    ]
    assert prep.filled_orders(orders) == [orders[0]]
    assert prep.calc_total_buy_cost(prep.filled_orders(orders)) == 1_006


@dataclass
class _RankedRow:
    symbol: str
    name: str


def test_selection_explanation_is_ranked_bounded_and_contribution_sorted():
    ranked = [
        (_RankedRow("600000.SH", "浦发银行"), 1.23456789),
        (_RankedRow("000001.SZ", "平安银行"), 0.5),
    ]
    result = prep.selection_explanation(
        ranked,
        weights={"small": 0.1, "large": -0.4},
        factor_values={"600000.SH": {"small": 1, "large": 2}},
        top_n=1,
    )
    assert result == [{
        "symbol": "600000.SH",
        "name": "浦发银行",
        "score": 1.234568,
        "factor_contributions": [
            {"factor": "large", "weight": -0.4, "raw_value": 2.0, "contribution": -0.8},
            {"factor": "small", "weight": 0.1, "raw_value": 1.0, "contribution": 0.1},
        ],
    }]
