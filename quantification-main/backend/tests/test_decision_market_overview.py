from app.api.routes import decision


def test_market_overview_exposes_budget_and_does_not_invent_themes(monkeypatch):
    monkeypatch.setattr(decision, "list_themes", lambda **_kwargs: {"items": []})
    monkeypatch.setattr(
        decision,
        "live_market_context",
        lambda: {
            "ok": True,
            "status": "ready",
            "trade_date": "20260710",
            "market_state": {
                "state": "bear",
                "confidence": 0.72,
                "model": "transparent_state_machine_v1",
                "reason": "test",
            },
            "sentiment": {"score": 31.0, "state": "risk_off"},
            "limitations": [],
        },
    )

    result = decision._market_overview()

    assert result["market_state"]["label"] == "风险收缩"
    assert result["market_state"]["risk_budget_multiplier"] == 0.55
    assert result["themes"]["status"] == "insufficient_data"
    assert result["themes"]["items"] == []


def test_gate_restricts_new_risk_in_contraction():
    result = decision._build_gate_decision(
        data_health={"issues": []},
        latest_data_contract={"ok": True, "latest_trade_date": "20260717"},
        ledger={"cash_balance": 200_000},
        model_status={"active_version": {"is_active": True, "validation": {"status": "passed", "production_eligible": True}}},
        market_overview={
            "ok": True,
            "market_state": {"state": "stress", "risk_budget_multiplier": 0.55},
        },
    )

    assert result["tone"] == "hold"
    assert result["reason_code"] == "market_risk_contraction"


def test_gate_restricts_new_risk_when_portfolio_risk_is_blocked():
    result = decision._build_gate_decision(
        data_health={"issues": []},
        latest_data_contract={"ok": True, "latest_trade_date": "20260717"},
        ledger={"cash_balance": 200_000},
        model_status={"active_version": {"is_active": True}},
        market_overview={
            "ok": True,
            "market_state": {"state": "bull", "risk_budget_multiplier": 1.0},
            "position_gate": {"allow_new_exposure": False, "reason": "组合集中度触发硬约束"},
        },
    )

    assert result["tone"] == "hold"
    assert result["reason_code"] == "portfolio_risk_gate"


def test_gate_degrades_when_market_context_is_unavailable():
    result = decision._build_gate_decision(
        data_health={"issues": []},
        latest_data_contract={"ok": True, "latest_trade_date": "20260717"},
        ledger={"cash_balance": 200_000},
        model_status={"active_version": {"is_active": True}},
        market_overview={"ok": False},
    )

    assert result["tone"] == "hold"
    assert result["reason_code"] == "market_context_unavailable"
