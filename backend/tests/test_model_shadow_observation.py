from app.services import model_shadow_observation as shadow


def _version(model_shadow=None):
    return {
        "id": 42,
        "model_key": "medium_horizon_lambdarank_v1",
        "version": "medium-candidate-v1",
        "status": "candidate",
        "metrics": {
            "strategy_impact_ab": {
                "status": "passed",
                "frozen_contract": {"contract_hash": "ab-contract"},
            },
        },
        "validation": {
            "production_eligibility": {
                "gates": {
                    "data_ready": True,
                    "controlled_strategy_ab": True,
                    "human_review": False,
                    "live_observation_20_sessions": False,
                },
            },
            **({"model_shadow": model_shadow} if model_shadow else {}),
        },
    }


def _champion():
    return {
        "champion_id": 50,
        "strategy_id": 1,
        "strategy": {
            "name": "Champion",
            "content": {"portfolio_method": "hrp"},
        },
        "strategy_controller_version_id": 31,
        "factor_bindings": {"momentum_20": {"version": "v1"}},
        "market_state_binding": {},
        "portfolio_optimizer": {"implementation": "hrp"},
        "execution_contract": {"signal": "T_close"},
    }


def test_ab_passed_candidate_enrolls_after_current_ready_date(monkeypatch):
    persisted = {}
    version = _version()
    monkeypatch.setattr(shadow, "get_model_version", lambda _version_id: version)
    monkeypatch.setattr(
        shadow,
        "active_production_plan_model_bindings",
        lambda: {key: None for key in shadow.PLAN_MODELS},
    )
    monkeypatch.setattr(shadow, "resolve_production_strategy_champion", _champion)
    monkeypatch.setattr(
        shadow,
        "_latest_ready_trade_date",
        lambda: ("20260729", {"status": "ready", "experiment_ready": True}),
    )
    monkeypatch.setattr(shadow, "_next_trade_date", lambda _date: "20260730")
    monkeypatch.setattr(
        shadow,
        "update_model_version_validation",
        lambda version_id, **values: persisted.update(
            {"version_id": version_id, **values}
        ),
    )

    result = shadow.enroll_model_shadow_candidate(42)

    enrolled = result["shadow"]
    assert enrolled["status"] == "awaiting_first_future_signal"
    assert enrolled["enrolled_after_trade_date"] == "20260729"
    assert enrolled["signal_start_trade_date"] == "20260730"
    assert enrolled["observed_return_sessions"] == 0
    assert enrolled["production_change_forbidden"] is True
    assert persisted["validation"]["production_eligible"] is False
    assert (
        persisted["validation"]["production_eligibility"]["gates"][
            "live_observation_20_sessions"
        ]
        is False
    )


def test_twenty_real_future_returns_can_pass_shadow_gate(monkeypatch):
    reference_bindings = {key: None for key in shadow.PLAN_MODELS}
    contract = {
        **_champion(),
        "schema_version": shadow.SHADOW_SCHEMA_VERSION,
        "strategy_name": "Champion",
        "strategy_content": {"portfolio_method": "hrp"},
        "reference_model_bindings": reference_bindings,
        "candidate_model_binding": {
            "id": 42,
            "model_key": "medium_horizon_lambdarank_v1",
        },
        "contract_hash": "shadow-contract",
    }
    model_shadow = {
        "schema_version": shadow.SHADOW_SCHEMA_VERSION,
        "status": "awaiting_first_future_signal",
        "model_version_id": 42,
        "model_key": "medium_horizon_lambdarank_v1",
        "signal_start_trade_date": "20260730",
        "reference_binding_ids": {key: None for key in shadow.PLAN_MODELS},
        "contract": contract,
        "contract_hash": "shadow-contract",
        "observed_return_sessions": 0,
        "observations": [],
    }
    version = _version(model_shadow)
    persisted = {}
    monkeypatch.setattr(shadow, "get_model_version", lambda _version_id: version)
    monkeypatch.setattr(
        shadow,
        "_reference_bindings",
        lambda _model_key: reference_bindings,
    )
    monkeypatch.setattr(
        shadow,
        "_latest_ready_trade_date",
        lambda: ("20260827", {"status": "ready", "experiment_ready": True}),
    )
    monkeypatch.setattr(
        shadow,
        "update_model_version_validation",
        lambda version_id, **values: persisted.update(
            {"version_id": version_id, **values}
        ),
    )

    dates = [f"2026-08-{index:02d}" for index in range(1, 22)]

    class DataSource:
        def get_available_trade_dates(self, _start, _end):
            return dates

    monkeypatch.setattr(shadow, "BacktestDataSource", DataSource)

    def replay(rate, total_return):
        return {
            "status": "completed",
            "metrics": {
                "total_return": total_return,
                "max_drawdown": -0.05,
                "turnover_rate": 0.20,
            },
            "equity_series": [
                {
                    "date": date,
                    "total_value": 200_000 * ((1.0 + rate) ** index),
                }
                for index, date in enumerate(dates)
            ],
            "model_fusion_history": [],
        }

    reference = replay(0.001, 0.0202)
    candidate = replay(0.002, 0.0408)
    candidate["model_fusion_history"] = [
        {
            "production_models": [{
                "model_key": "medium_horizon_lambdarank_v1",
                "consumed": True,
                "effective": True,
            }],
        }
        for _ in range(20)
    ]
    monkeypatch.setattr(
        shadow,
        "_run_pair",
        lambda *_args, **_kwargs: (reference, candidate),
    )

    result = shadow.advance_model_shadow_candidate(
        42,
        through_trade_date="20260827",
    )

    assert result["status"] == "passed"
    assert result["observed_return_sessions"] == 20
    assert all(result["gates"].values())
    validation = persisted["validation"]
    assert validation["status"] == "shadow_passed"
    assert validation["production_eligible"] is False
    assert (
        validation["production_eligibility"]["gates"][
            "live_observation_20_sessions"
        ]
        is True
    )
    assert result["production_change_forbidden"] is True
