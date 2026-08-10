from app.services import strategy_controller


def _policy_context() -> dict:
    return {
        "strategy_policy": strategy_controller.DEFAULT_STRATEGY_POLICY,
        "strategy_policy_version": "test-v1",
        "strategy_policy_version_id": 1,
    }


def test_champion_quarantine_freezes_equity_buy_add_but_keeps_defensive_permission(monkeypatch):
    monkeypatch.setattr(strategy_controller, "active_strategy_policy_context", _policy_context)
    result = strategy_controller.build_pretrade_strategy_controller(
        candidate_symbols=["000001"],
        holding_symbols=[],
        selections={"000001": {"name": "平安银行", "score": 90, "model_signals": {}}},
        research_evidence={"000001": {"eligible_for_core": True}},
        expected_returns={"000001": 0.05, "511880": 0.01},
        current_weights={},
        defensive_weights={"511880": 0.30},
        execution_guard={"status": "quarantined", "mode": "research_only_sell_reduce"},
    )

    equity = result["weight_bounds"]["000001"]
    defensive = result["weight_bounds"]["511880"]
    assert equity["action"] == "buy_candidate"
    assert equity["permission_allowed"] is False
    assert equity["max_weight"] == 0
    assert defensive["action"] == "defensive_switch"
    assert defensive["permission_allowed"] is True
    assert defensive["max_weight"] == 0.15


def test_formal_oos_warning_does_not_block_qualified_equity_buy(monkeypatch):
    monkeypatch.setattr(strategy_controller, "active_strategy_policy_context", _policy_context)
    result = strategy_controller.build_pretrade_strategy_controller(
        candidate_symbols=["000001"],
        holding_symbols=[],
        selections={"000001": {"name": "平安银行", "score": 90, "model_signals": {}}},
        research_evidence={"000001": {"eligible_for_core": True}},
        expected_returns={"000001": 0.05},
        current_weights={},
        execution_guard={
            "status": "allowed",
            "mode": "normal",
            "reason": "active_champion_failed_formal_oos_warning_only",
            "formal_oos_warning_active": True,
            "execution_restricted_by_formal_oos": False,
        },
    )

    equity = result["weight_bounds"]["000001"]
    assert equity["action"] == "buy_candidate"
    assert equity["permission_allowed"] is True
    assert equity["max_weight"] > 0


def test_controller_freezes_exit_before_optimizer(monkeypatch):
    monkeypatch.setattr(strategy_controller, "active_strategy_policy_context", _policy_context)
    result = strategy_controller.build_pretrade_strategy_controller(
        candidate_symbols=["000001"],
        holding_symbols=["000001"],
        selections={"000001": {"score": 70, "model_signals": {}}},
        research_evidence={"000001": {"eligible_for_core": True}},
        expected_returns={"000001": -0.06},
        current_weights={"000001": 0.12},
        position_pnl_by_symbol={"000001": -0.07},
        execution_guard={"status": "allowed", "mode": "normal"},
    )

    bound = result["weight_bounds"]["000001"]
    assert bound["min_weight"] == 0.0
    assert bound["max_weight"] == 0.0
    assert bound["action"] == "sell_exit"
    assert bound["permission_allowed"] is True
    assert bound["intent"] == "sell_exit"
    assert bound["obligation"]["type"] == "mandatory_exit"


def test_controller_uses_frozen_data_cutoff_as_generation_time(monkeypatch):
    monkeypatch.setattr(strategy_controller, "active_strategy_policy_context", _policy_context)

    result = strategy_controller.build_pretrade_strategy_controller(
        candidate_symbols=["000001"],
        holding_symbols=[],
        expected_returns={"000001": 0.03},
        current_weights={},
        execution_guard={"status": "allowed", "mode": "normal"},
        context={"data_as_of_time": "2026-07-27T15:00:00+08:00"},
    )

    assert result["generated_at"] == "2026-07-27T15:00:00+08:00"
    assert result["strategy_policy_version"] == "test-v1"
    assert result["strategy_policy_version_id"] == 1


def test_holding_research_error_freezes_hold_instead_of_forcing_exit(monkeypatch):
    monkeypatch.setattr(strategy_controller, "active_strategy_policy_context", _policy_context)

    result = strategy_controller.build_pretrade_strategy_controller(
        candidate_symbols=[],
        holding_symbols=["000001"],
        research_evidence={
            "000001": {
                "evaluation_status": "error",
                "eligible_for_core": False,
                "error": {"code": "research_admission_error"},
            },
        },
        expected_returns={"000001": -0.08},
        current_weights={"000001": 0.12},
        execution_guard={"status": "allowed", "mode": "normal"},
    )

    bound = result["weight_bounds"]["000001"]
    assert bound["min_weight"] == 0.12
    assert bound["max_weight"] == 0.12
    assert bound["action"] == "hold"
    assert bound["permission_allowed"] is False
    assert bound["intent"] == "hold"
    assert bound["obligation"]["required"] is False
