from __future__ import annotations

from app.services import quant_risk


def test_latest_market_day_gate_pauses_when_daily_bundle_is_incomplete(monkeypatch):
    monkeypatch.setattr(
        "app.data_platform.services.decision_data_contract.decision_data_contract",
        lambda: {
            "ok": False,
            "latest_trade_date": "20260717",
            "missing_apis": ["daily_basic", "stk_limit"],
            "snapshot_ready": False,
            "required_apis": ["daily", "daily_basic", "stk_limit"],
        },
    )

    decision = quant_risk.evaluate_latest_market_day_gate()

    assert decision.should_pause is True
    assert decision.code == "paused_latest_market_day_not_ready"
    assert decision.evidence["latest_trade_date"] == "20260717"
    assert decision.evidence["missing_apis"] == ["daily_basic", "stk_limit"]


def test_latest_market_day_gate_passes_when_daily_bundle_and_snapshot_exist(monkeypatch):
    monkeypatch.setattr(
        "app.data_platform.services.decision_data_contract.decision_data_contract",
        lambda: {"ok": True, "latest_trade_date": "20260717"},
    )

    decision = quant_risk.evaluate_latest_market_day_gate()

    assert decision.should_pause is False
    assert decision.code == "ok"
