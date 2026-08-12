from app.services import controller_candidate_observation as observation


def test_controller_candidate_replay_is_isolated_and_keeps_eligibility_false(monkeypatch):
    version = {
        "id": 9,
        "model_key": "strategy_controller",
        "version": "controller-v9",
        "status": "candidate",
        "is_active": False,
        "created_at": "2026-08-01T16:00:00+08:00",
        "validation": {"production_eligible": True},
    }
    champion = {
        "champion_id": 3,
        "strategy_id": 4,
        "strategy": {"name": "Champion", "content": {}},
        "factor_bindings": {},
        "model_bindings": {},
        "market_state_binding": {},
        "execution_contract": {},
    }
    persisted = {}
    monkeypatch.setattr(observation, "get_model_version", lambda _id: version)
    monkeypatch.setattr(observation, "resolve_production_strategy_champion", lambda: champion)
    monkeypatch.setattr(
        observation.BacktestDataSource,
        "get_available_trade_dates",
        lambda self, start, end: ["20260803", "20260804"],
    )
    replay = {"status": "completed", "equity_series": []}
    import app.services.strategy_replay as strategy_replay
    monkeypatch.setattr(strategy_replay, "run_strategy_replay", lambda **kwargs: replay)
    monkeypatch.setattr(
        observation,
        "persist_candidate_replay",
        lambda version_id, value: {"ok": True, "account_id": 12},
    )
    monkeypatch.setattr(
        observation,
        "update_model_version_validation",
        lambda version_id, **kwargs: persisted.update(kwargs),
    )

    result = observation.run_controller_candidate_observation(
        9, through_trade_date="20260804"
    )

    assert result["ledger_audit"]["account_id"] == 12
    assert result["production_change_forbidden"] is True
    assert persisted["validation"]["production_eligible"] is False
