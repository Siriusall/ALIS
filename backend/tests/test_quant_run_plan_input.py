import pytest

from app.services.quant_run_contracts import ReplayRiskGateError
from app.services.quant_run_plan_input import build_authoritative_selection_detail, normalize_authoritative_plan


def _plan() -> dict:
    return {
        "plan_id": 9, "plan_version": "20260724-v1", "input_hash": "hash",
        "execution_claim_token": "task-1", "target_symbols": ["000001", "600000"],
        "target_weights": {"000001": .4, "600000": .5},
        "proposal": [{"symbol": "000001", "name": "平安银行", "expected_20d_return": .03}],
        "risk_budget": {"target_position_pct": .9}, "decision_version_manifest": {"hash": "v1"},
    }


def test_authoritative_plan_normalizes_and_freezes_provenance():
    plan = normalize_authoritative_plan(_plan())
    detail = build_authoritative_selection_detail(plan, signal_date="2026-07-24", exchange_group="HS")
    assert plan["target_weights"] == {"000001": .4, "600000": .5}
    assert detail["plan_provenance"]["plan_id"] == 9
    assert detail["selections"][0]["score"] == 3.0


@pytest.mark.parametrize("mutation", [
    {"execution_claim_token": ""},
    {"target_symbols": ["000001", "000001"]},
    {"target_weights": {"000001": -.1}},
    {"target_weights": {"000001": .6, "600000": .5}},
    {"target_weights": {"000001": .4, "300001": .1}},
])
def test_authoritative_plan_rejects_unsafe_contracts(mutation):
    plan = {**_plan(), **mutation}
    with pytest.raises(ReplayRiskGateError) as exc:
        normalize_authoritative_plan(plan)
    assert exc.value.code == "invalid_authoritative_plan"
