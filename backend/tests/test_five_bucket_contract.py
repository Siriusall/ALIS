from app.services.five_bucket_contract import build_target_weight_contract, summarize_bucket_contracts
from app.integrations.broker_adapter import OrderRequest
from app.services.rebalance_allocator import allocate_buy_orders


def test_target_weight_contract_contains_plan_required_fields():
    contract = build_target_weight_contract(
        symbol="000001",
        target_weight=.08,
        current_weight=.03,
        score=82,
        evidence={"narrative_lifecycle": "扩散", "capital_persistence": .62},
    )

    assert contract["schema_version"] == "five_bucket_target_weight_contract_v1"
    assert contract["function_bucket"] == "leader"
    assert contract["return_source"]
    assert contract["holding_period"]
    assert contract["funding_confirmation"] == "已确认"
    assert contract["exit_conditions"]
    assert contract["data_status"] == "ready"
    assert contract["evidence_quality"] in {"proxy_backed", "hybrid", "model_backed"}
    assert contract["evidence_source_status"]["funding_confirmation"] == "capital_proxy"
    assert contract["model_evidence"]["capital_model"] == "capital_persistence_proxy_v1"


def test_bucket_summary_groups_stable_and_offensive_budget():
    contracts = [
        build_target_weight_contract(symbol="511880.SH", target_weight=.2, evidence={"defensive_asset": True}),
        build_target_weight_contract(symbol="000001", target_weight=.1, score=75, evidence={"narrative_lifecycle": "启动"}),
    ]
    summary = summarize_bucket_contracts(contracts)

    assert summary["risk_groups"]["stable"] == .2
    assert summary["risk_groups"]["offensive"] == .1


def test_allocator_attaches_contract_to_planned_buy_order():
    planned, blocked, _ = allocate_buy_orders(
        [OrderRequest(symbol="000001", side="BUY", price=10, quantity=0, name="平安银行")],
        budget=20000,
        total_equity=200000,
        symbol_scores={"000001": 85},
    )

    assert not blocked
    assert planned[0].target_weight_contract["schema_version"] == "five_bucket_target_weight_contract_v1"
    assert planned[0].target_weight_contract["target_weight"] > 0
