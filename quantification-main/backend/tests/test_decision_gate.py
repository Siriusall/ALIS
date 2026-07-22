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


def test_bootstrap_production_model_unblocks_selection_chain():
    pause = decision.evaluate_model_readiness_gate({"active_version": {
        "id": 5, "is_active": True,
        "validation": {
            "status": "passed", "production_eligible": False,
            "bootstrap_production_eligible": True,
            "promotion_policy": "bootstrap_offline_gates",
            "production_eligibility": {"eligible": False, "history_sessions": 487},
        },
    }})

    assert pause.should_pause is False


def test_decision_gate_blocks_new_exposure_when_equity_exceeds_effective_budget():
    ledger = {"cash_balance": 30_000, "position_market_value": 70_000, "total_equity": 100_000}
    market = {
        "ok": True,
        "position_gate": {"allow_new_exposure": True, "combined_risk_budget_multiplier": 0.525},
        "market_state": {"state": "S3", "risk_budget_multiplier": 0.7},
        "portfolio_risk": {"breaches": [], "deleveraging_actions": []},
    }

    gate = decision._build_gate_decision(
        data_health={"issues": []},
        latest_data_contract={"ok": True, "latest_trade_date": "20260721", "missing_apis": [], "snapshot_ready": True},
        ledger=ledger,
        model_status={"active_version": {"is_active": True}},
        market_overview=market,
    )
    explanation = decision._risk_gate_explanation(
        gate_decision=gate,
        latest_data_contract={"ok": True},
        ledger=ledger,
        model_status={"active_version": {"is_active": True}},
        market_overview=market,
        recent_execution=None,
    )

    assert gate["reason_code"] == "equity_budget_exceeded"
    assert explanation["allow_new_exposure"] is False
    assert explanation["requires_rebalance"] is True
