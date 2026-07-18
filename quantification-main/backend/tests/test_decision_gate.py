from __future__ import annotations

import sys
import types

sys.modules.setdefault("tushare", types.SimpleNamespace(pro_api=lambda *args, **kwargs: None))

from app.api.routes import decision


def test_decision_gate_stops_when_latest_trade_day_is_not_fully_materialized():
    gate = decision._build_gate_decision(
        data_health={"issues": []},
        latest_data_contract={
            "ok": False,
            "latest_trade_date": "20260716",
            "missing_apis": ["daily_basic", "stk_limit"],
            "snapshot_ready": False,
        },
        ledger={"cash_balance": 100000},
        model_status={"active_version": {"is_active": True, "validation": {"status": "passed", "production_eligible": True}}},
        market_overview={"ok": True, "position_gate": {"allow_new_exposure": True}, "market_state": {"state": "S1", "risk_budget_multiplier": 0.85}},
    )

    assert gate["tone"] == "stop"
    assert gate["reason_code"] == "latest_market_day_not_materialized"
    assert "20260716" in gate["detail"]


def test_decision_gate_can_continue_after_latest_trade_day_contract_is_ready():
    gate = decision._build_gate_decision(
        data_health={"issues": []},
        latest_data_contract={
            "ok": True,
            "latest_trade_date": "20260716",
            "missing_apis": [],
            "snapshot_ready": True,
        },
        ledger={"cash_balance": 100000},
        model_status={"active_version": {"is_active": True, "validation": {"status": "passed", "production_eligible": True}}},
        market_overview={"ok": True, "position_gate": {"allow_new_exposure": True}, "market_state": {"state": "S1", "risk_budget_multiplier": 0.85}},
    )

    assert gate["reason_code"] == "ready"


def test_decision_gate_and_worker_contract_block_rule_fallback_without_active_model():
    gate = decision._build_gate_decision(
        data_health={"issues": []},
        latest_data_contract={"ok": True, "latest_trade_date": "20260717", "missing_apis": [], "snapshot_ready": True},
        ledger={"cash_balance": 100000},
        model_status={"active_version": None},
        market_overview={"ok": True, "position_gate": {"allow_new_exposure": True}, "market_state": {"state": "S1"}},
    )

    assert gate["tone"] == "hold"
    assert gate["reason_code"] == "paused_model_not_ready"
