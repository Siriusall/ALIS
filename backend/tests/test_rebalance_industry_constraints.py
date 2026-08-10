from app.integrations.broker_adapter import OrderRequest
from app.services.rebalance_allocator import allocate_buy_orders


def _order(symbol: str, price: float = 10.0) -> OrderRequest:
    return OrderRequest(
        symbol=symbol, side="BUY", price=price, quantity=0, name=symbol,
        close=price, change_pct=0, volume=1_000_000,
    )


def test_allocator_enforces_pre_trade_industry_cap() -> None:
    candidates = [_order("000001"), _order("000002"), _order("000003"), _order("600001")]
    industries = {"000001": "银行", "000002": "银行", "000003": "银行", "600001": "医药"}

    planned, blocked, _ = allocate_buy_orders(
        candidates, budget=100_000, total_equity=100_000,
        max_single_position_pct=.40, max_industry_position_pct=.40,
        industry_by_symbol=industries,
    )

    bank_value = sum(item.amount for item in planned if industries[item.symbol] == "银行")
    assert bank_value <= 40_000 + 1e-6
    assert sum(item.amount for item in planned) <= 80_000 + 1e-6
    assert not [item for item in blocked if item.block_code == "industry_position_limit"]


def test_allocator_blocks_new_lot_when_existing_industry_is_at_cap() -> None:
    planned, blocked, _ = allocate_buy_orders(
        [_order("000001")], budget=10_000, total_equity=100_000,
        max_single_position_pct=.40, max_industry_position_pct=.40,
        industry_by_symbol={"000001": "银行"}, existing_industry_value={"银行": 39_500},
    )

    assert planned == []
    assert blocked[0].block_code == "industry_position_limit"


def test_allocator_uses_optimizer_target_weights_instead_of_equal_weighting() -> None:
    planned, _, _ = allocate_buy_orders(
        [_order("000001"), _order("600001")], budget=100_000, total_equity=100_000,
        max_single_position_pct=.80, max_industry_position_pct=1.0,
        industry_by_symbol={"000001": "银行", "600001": "医药"},
        target_weights={"000001": .70, "600001": .30},
    )
    values = {item.symbol: item.amount for item in planned}
    assert values["000001"] > values["600001"] * 2


def test_allocator_enforces_standard_and_core_limits_before_order_creation() -> None:
    planned, _, _ = allocate_buy_orders(
        [_order("000001"), _order("600001")], budget=100_000, total_equity=200_000,
        max_single_position_pct=.25, max_industry_position_pct=1.0,
        industry_by_symbol={"000001": "银行", "600001": "医药"},
        target_weights={"000001": .5, "600001": .5},
        symbol_position_limits={"000001": .15, "600001": .25},
        initial_entry_fraction=1.0,
    )
    values = {item.symbol: item.amount for item in planned}
    assert values["000001"] <= 30_000
    assert values["600001"] <= 50_000
    assert values["600001"] > values["000001"]


def test_first_entry_is_at_most_half_of_target_allocation() -> None:
    planned, _, _ = allocate_buy_orders(
        [_order("000001")], budget=100_000, total_equity=200_000,
        max_single_position_pct=.25, target_weights={"000001": 1.0},
    )
    assert len(planned) == 1
    assert planned[0].amount <= 50_000


def test_correlated_industries_share_a_thirty_five_percent_cap() -> None:
    planned, _, _ = allocate_buy_orders(
        [_order("000001"), _order("600001")], budget=100_000, total_equity=100_000,
        max_single_position_pct=.50, max_industry_position_pct=.50,
        max_correlated_industry_pct=.35, initial_entry_fraction=1.0,
        industry_by_symbol={"000001": "银行", "600001": "证券"},
    )
    assert sum(item.amount for item in planned) <= 35_000


def test_existing_target_is_topped_up_to_optimizer_weight() -> None:
    planned, _, _ = allocate_buy_orders(
        [_order("511880", 1.0), _order("518880", 1.0)],
        budget=120_000, total_equity=200_000,
        max_single_position_pct=.15, max_industry_position_pct=1.0,
        existing_position_value={"511880": 20_000, "518880": 20_000},
        target_weights={"511880": .15, "518880": .15},
        symbol_position_limits={"511880": .15, "518880": .15},
        max_correlated_industry_pct=1.0,
        initial_entry_fraction=1.0,
    )
    values = {item.symbol: item.amount for item in planned}
    assert 9_000 <= values["511880"] <= 10_000
    assert 9_000 <= values["518880"] <= 10_000


def test_asset_specific_etf_limit_cannot_exceed_general_stock_limit() -> None:
    planned, _, _ = allocate_buy_orders(
        [_order("511880", 1.0)], budget=100_000, total_equity=200_000,
        max_single_position_pct=.15, target_weights={"511880": 1.0},
        max_industry_position_pct=1.0,
        symbol_position_limits={"511880": .15}, initial_entry_fraction=1.0,
    )
    assert 29_000 <= planned[0].amount <= 30_000
