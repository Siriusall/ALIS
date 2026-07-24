import app.services.quant_run_engine as engine
from app.integrations.broker_adapter import OrderRequest
from app.services.rebalance_allocator import allocate_buy_orders
from app.services.quant_risk import enforce_marginal_cvar_limit


def test_monthly_double_sided_turnover_budget_is_thirty_percent(monkeypatch):
    monkeypatch.setattr(engine, "list_operations", lambda execution_id=None, limit=200000: [
        {"at": "2026-07-02T09:30:00", "price": 10, "quantity": 1000},
        {"at": "2026-07-08T09:30:00", "price": 20, "quantity": 500},
        {"at": "2026-06-30T09:30:00", "price": 99, "quantity": 1000},
    ])
    budget = engine._monthly_turnover_budget("2026-07-15", 200_000)
    assert budget == {"limit": 60_000.0, "used_before": 20_000.0, "remaining": 40_000.0}


def test_high_risk_names_are_capped_at_one_percent_each_and_five_percent_total():
    orders = [OrderRequest(symbol=f"00000{i}", side="BUY", price=10, quantity=0, volume=1_000_000, close=10) for i in range(1, 7)]
    planned, _, _ = allocate_buy_orders(
        orders, budget=100_000, total_equity=200_000, max_single_position_pct=.25,
        max_industry_position_pct=1, max_correlated_industry_pct=1,
        high_risk_symbols={order.symbol for order in orders}, initial_entry_fraction=1,
    )
    assert all(item.amount <= 2_000 for item in planned)
    assert sum(item.amount for item in planned) <= 10_000


def test_order_value_is_capped_at_one_percent_of_twenty_day_average_amount():
    order = OrderRequest(symbol="000001", side="BUY", price=10, quantity=0, volume=1_000_000, close=10)
    planned, _, _ = allocate_buy_orders(
        [order], budget=100_000, total_equity=200_000, max_single_position_pct=.25,
        max_order_value_by_symbol={"000001": 12_000}, initial_entry_fraction=1,
    )
    assert planned[0].amount <= 12_000


def test_marginal_cvar_limit_fails_closed_below_nine_names_and_passes_risk_parity_at_nine():
    returns = {f"S{i}": [-.01, .005, -.02, .01] for i in range(9)}
    assert enforce_marginal_cvar_limit({key: .125 for key in list(returns)[:8]}, {key: returns[key] for key in list(returns)[:8]})["status"] == "infeasible"
    result = enforce_marginal_cvar_limit({key: 1 / 9 for key in returns}, returns)
    assert result["status"] == "ready"
    assert max(result["contributions"].values()) <= .12
