from app.services.tiered_offline_replay import run_tiered_offline_replay_comparison


def _portfolio(signal_date):
    symbol = "000001" if signal_date.endswith("14") else "000002"
    return {
        "status": "ready",
        "selection_source": "unified_full_market_selection",
        "selection_snapshot": {
            "schema_version": "alis_unified_stock_selection_v2",
            "ranked_symbols": [symbol],
            "core_symbols": [],
            "restricted_symbols": [symbol],
            "hard_blocked_symbols": [],
            "tradable_symbols": [symbol],
            "data_as_of_time": f"{signal_date[:4]}-{signal_date[4:6]}-{signal_date[6:]}T15:00:00+08:00",
        },
        "research_tier_contract": {
            "schema_version": "alis_equity_research_tier_v1",
            "contract_hash": f"hash-{signal_date}",
        },
        "tiered_equity_budget": {
            "schema_version": "alis_tiered_equity_budget_v1",
            "contract_hash": f"budget-{signal_date}",
            "opportunity_status": "restricted_only",
            "base_equity_budget": .6,
            "effective_equity_budget": .2,
            "restricted_pool_budget": .2,
            "restricted_symbol_cap": .03,
        },
        "pretrade_controller": {
            "schema_version": "alis_strategy_controller_pretrade_v3",
            "actions": [{"action": "buy_candidate", "research_tier": "restricted"}],
        },
        "optimizer": {"schema_version": "optimizer_v1", "status": "completed"},
        "industry_allocation": {"schema_version": "industry_v1"},
        "expected_return_contract": {"schema_version": "expected_v1"},
        "portfolio_method_competition": {"schema_version": "alis_portfolio_method_competition_v1"},
        "risk_limit_contract": {"schema_version": "risk_v1"},
        "decision_version_manifest": {"schema_version": "alis_decision_version_manifest_v3"},
        "rebalance_proposal": [{"symbol": symbol, "research_tier": "restricted"}],
        "execution_orders": [],
        "final_target_weights": {},
        "decision_data_gate": {"decision_ready": True},
        "execution_checks": [{"key": "risk", "status": "pass"}],
    }


def test_offline_comparison_runs_full_contract_path_without_persistence(monkeypatch):
    monkeypatch.setattr(
        "app.services.tiered_offline_replay.resolve_execute_date",
        lambda signal_date: (
            "20260915" if signal_date == "20260914" else "20260917",
            True,
        ),
    )
    monkeypatch.setattr(
        "app.services.decision_version_manifest.build_decision_version_manifest",
        lambda **_kwargs: {"schema_version": "alis_decision_version_manifest_v3"},
    )
    monkeypatch.setattr(
        "app.services.system_release_center.release_status",
        lambda: {"ok": True, "active_release": {"id": 1}, "drift_components": []},
    )
    monkeypatch.setattr("app.services.tiered_offline_replay.validate_unified_decision_contract", lambda _contract: None)

    result = run_tiered_offline_replay_comparison(
        ("20260914", "20260916"), portfolio_builder=_portfolio,
    )

    assert result["schema_version"] == "alis_tiered_offline_replay_v1"
    assert result["persisted"] is False
    assert result["ledger_touched"] is False
    assert result["comparison"]["same_decision_schema"] is True
    assert result["comparison"]["all_t1_frozen"] is True
