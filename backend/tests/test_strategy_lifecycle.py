from app.services import production_strategy
from app.services import strategy_lifecycle as lifecycle


def _formal_reference(*, cycle_task_id: int = 1) -> dict:
    return {
        "schema_version": "alis_formal_oos_reference_v1",
        "cycle_task_id": cycle_task_id,
        "hard_data_window": {"start": "20200101", "end": "20260731"},
        "validation_schema_version": "alis_purged_walk_forward_oos_v1",
        "protocol": {"minimum_windows": 5, "cost_policy": "fees_and_slippage"},
        "production_strategy_version_id": 50,
        "production_strategy_version": "champion-v50",
        "strategy_controller_version": "controller-v1",
        "test_windows": [{"index": index, "start": str(index), "end": str(index), "sessions": 126} for index in range(1, 6)],
    }


def _formal_statistics(*, passed: bool = True) -> dict:
    return {"multiple_testing": {
        "schema_version": "alis_deflated_sharpe_ratio_v1",
        "probability_sharpe_exceeds_selection_bias": .99 if passed else .60,
        "confidence_threshold": .95,
        "passed": passed,
    }}


def _passed_holdout() -> dict:
    return {
        "schema_version": "alis_sealed_strategy_holdout_v1",
        "status": "passed",
        "passed": True,
        "contract_hash": "sealed-contract",
    }


def _shadow_rows(count: int, *, delta: float = 0.002) -> list[dict]:
    return [
        {
            "trade_date": f"2026-08-{index + 1:02d}",
            "candidate_return": 0.01,
            "champion_return": 0.01 - delta,
            "excess_delta": delta,
            "candidate_drawdown": -0.05,
            "champion_drawdown": -0.06,
            "metrics": {
                "return_contract": lifecycle.SHADOW_RETURN_CONTRACT,
                "candidate_daily_trade_count": 1 if index % 5 == 0 else 0,
            },
            "point_in_time_passed": True,
            "behavior_passed": True,
        }
        for index in range(count)
    ]


def test_formal_gate_requires_protocol_and_non_worse_champion():
    gate = lifecycle.formal_candidate_gate(
        {
            "status": "passed",
            "reference_contract": _formal_reference(),
            "statistics": _formal_statistics(),
            "summary": {
                "aggregate_cost_after_return": 0.12,
                "worst_window_drawdown": -0.11,
                "total_fees": 800,
            },
        },
        {
            "reference_contract": _formal_reference(),
            "summary": {
                "aggregate_cost_after_return": 0.08,
                "worst_window_drawdown": -0.10,
                "total_fees": 700,
            },
        },
        sealed_holdout=_passed_holdout(),
    )
    assert gate["passed"] is True
    failed = lifecycle.formal_candidate_gate(
        {
            "status": "failed", "reference_contract": _formal_reference(),
            "summary": {"aggregate_cost_after_return": 0.20, "worst_window_drawdown": -0.1, "total_fees": 800},
        },
        {
            "reference_contract": _formal_reference(),
            "summary": {"aggregate_cost_after_return": 0.08, "worst_window_drawdown": -0.1, "total_fees": 700},
        },
        sealed_holdout=_passed_holdout(),
    )
    assert failed["passed"] is False
    assert "formal_oos_protocol_passed" in failed["blocking_reasons"]


def test_formal_gate_blocks_missing_or_mismatched_champion_reference():
    candidate = {
        "status": "passed",
        "reference_contract": _formal_reference(),
        "statistics": _formal_statistics(),
        "summary": {"aggregate_cost_after_return": 0.12, "worst_window_drawdown": -0.1, "total_fees": 800},
    }
    missing = lifecycle.formal_candidate_gate(candidate, None, sealed_holdout=_passed_holdout())
    assert missing["decision"] == "blocked_by_reference"
    assert missing["comparison"]["champion_return"] is None
    assert "same_run_window_cost_reference" in missing["blocking_reasons"]

    mismatched = lifecycle.formal_candidate_gate(candidate, {
        "reference_contract": _formal_reference(cycle_task_id=2),
        "summary": {"aggregate_cost_after_return": 0.08, "worst_window_drawdown": -0.1, "total_fees": 700},
    }, sealed_holdout=_passed_holdout())
    assert mismatched["decision"] == "blocked_by_reference"


def test_formal_gate_preserves_candidate_when_multiple_testing_support_is_weak():
    candidate = {
        "status": "passed", "reference_contract": _formal_reference(),
        "statistics": _formal_statistics(passed=False),
        "summary": {"aggregate_cost_after_return": .12, "worst_window_drawdown": -.1, "total_fees": 800},
    }
    champion = {
        "reference_contract": _formal_reference(),
        "summary": {"aggregate_cost_after_return": .08, "worst_window_drawdown": -.1, "total_fees": 700},
    }
    gate = lifecycle.formal_candidate_gate(candidate, champion, sealed_holdout=_passed_holdout())
    assert gate["decision"] == "blocked_by_statistics"
    assert gate["passed"] is False
    assert "multiple_testing_adjusted_support" in gate["blocking_reasons"]


def test_shadow_gate_cannot_promote_before_twenty_future_sessions():
    early = lifecycle.shadow_promotion_gate(_shadow_rows(19))
    assert early["passed"] is False
    assert "minimum_future_sessions" in early["blocking_reasons"]
    ready = lifecycle.shadow_promotion_gate(_shadow_rows(20))
    assert ready["passed"] is True
    assert ready["session_count"] == 20


def test_shadow_gate_blocks_point_in_time_or_behavior_failure():
    rows = _shadow_rows(20)
    rows[3]["point_in_time_passed"] = False
    rows[5]["behavior_passed"] = False
    gate = lifecycle.shadow_promotion_gate(rows)
    assert gate["passed"] is False
    assert "point_in_time_all_passed" in gate["blocking_reasons"]
    assert "behavior_all_passed" in gate["blocking_reasons"]


def test_shadow_gate_rejects_legacy_overlapping_window_returns():
    rows = _shadow_rows(20)
    for row in rows:
        row["metrics"] = {}
    gate = lifecycle.shadow_promotion_gate(rows)
    assert gate["passed"] is False
    assert "non_overlapping_daily_return_contract" in gate["blocking_reasons"]


def test_shadow_gate_extends_after_twenty_days_when_rebalances_are_insufficient():
    rows = _shadow_rows(20)
    for row in rows:
        row["metrics"]["candidate_daily_trade_count"] = 0
    gate = lifecycle.shadow_promotion_gate(rows)
    assert gate["passed"] is False
    assert gate["effective_rebalance_count"] == 0
    assert gate["observation_decision"] == "extend_for_effective_rebalances"


def test_shadow_gate_extends_when_confidence_interval_still_crosses_zero():
    rows = _shadow_rows(20)
    returns = [.02, -.02] * 10
    for row, value in zip(rows, returns):
        row["candidate_return"] = value
        row["champion_return"] = 0.0
        row["excess_delta"] = value
    gate = lifecycle.shadow_promotion_gate(rows)
    assert gate["passed"] is False
    assert gate["uncertainty"]["compounded_excess_ci_95"][0] <= 0
    assert gate["observation_decision"] == "extend_for_uncertainty"


def test_replay_daily_return_uses_only_last_adjacent_equity_change():
    daily_return, evidence = lifecycle._replay_daily_return(
        {
            "metrics": {"total_return": 0.21},
            "equity_series": [
                {"date": "2026-08-01", "total_value": 100.0},
                {"date": "2026-08-02", "total_value": 110.0},
                {"date": "2026-08-03", "total_value": 121.0},
            ],
        },
        trade_date="2026-08-03",
    )
    assert round(daily_return, 8) == 0.1
    assert evidence["previous_equity"] == 110.0
    assert evidence["current_equity"] == 121.0


def test_shadow_gate_rebuilds_forward_return_and_drawdown_from_daily_returns():
    rows = _shadow_rows(20, delta=0.0)
    candidate_returns = [0.1, -0.1] + [0.0] * 18
    champion_returns = [0.0] * 20
    for row, candidate_return, champion_return in zip(rows, candidate_returns, champion_returns):
        row["candidate_return"] = candidate_return
        row["champion_return"] = champion_return
        row["excess_delta"] = candidate_return - champion_return
    gate = lifecycle.shadow_promotion_gate(rows)
    assert gate["candidate_compounded_return"] == -0.01
    assert gate["champion_compounded_return"] == 0.0
    assert gate["candidate_forward_drawdown"] == -0.1
    assert gate["champion_forward_drawdown"] == 0.0


def test_shadow_cycle_records_non_overlapping_daily_returns_from_replay(monkeypatch):
    candidate = {
        "id": 11,
        "validation": {"status": "shadow_observation", "shadow_started_after": "2026-08-01"},
        "config": {"strategy_id": 11},
    }
    recorded: list[dict] = []
    monkeypatch.setattr(lifecycle, "audit_oos_hard_data", lambda **_kwargs: {
        "status": "ready", "window_start": "20260801", "window_end": "20260802",
    })

    class FakeSource:
        def get_available_trade_dates(self, *_args):
            return ["2026-08-01", "2026-08-02"]

    monkeypatch.setattr(lifecycle, "BacktestDataSource", FakeSource)
    monkeypatch.setattr(lifecycle, "resolve_production_strategy_champion", lambda: {
        "champion_id": 50, "strategy_id": 1,
    })
    monkeypatch.setattr(lifecycle, "list_model_versions", lambda *_args, **_kwargs: [candidate])
    monkeypatch.setattr(lifecycle, "get_model_version", lambda *_args: {"config": {"strategy_id": 1}})
    monkeypatch.setattr(lifecycle, "list_shadow_observations", lambda *_args, **_kwargs: list(recorded))
    monkeypatch.setattr(lifecycle, "record_shadow_observation", lambda **kwargs: recorded.append({
        **kwargs,
        "excess_delta": kwargs["candidate_return"] - kwargs["champion_return"],
    }))
    monkeypatch.setattr(lifecycle, "_merge_validation", lambda *_args, **kwargs: kwargs)

    def replay_runner(*, model_version_id, **_kwargs):
        previous_equity = 110.0 if model_version_id == 11 else 105.0
        ending_equity = 121.0 if model_version_id == 11 else 110.25
        return {
            "metrics": {"total_return": ending_equity / 100.0 - 1.0, "max_drawdown": 0.0},
            "summary": {},
            "equity_series": [
                {"date": "2026-07-31", "total_value": 100.0},
                {"date": "2026-08-01", "total_value": previous_equity},
                {"date": "2026-08-02", "total_value": ending_equity},
            ],
            "data_quality": {"point_in_time": {"passed": True}},
            "blocked_events": [],
            "trades": [{"date": "2026-08-02"}] if model_version_id == 11 else [],
        }

    result = lifecycle.run_strategy_shadow_cycle(replay_runner=replay_runner)

    assert result["schema_version"] == "alis_strategy_shadow_cycle_v2"
    assert len(recorded) == 1
    assert round(recorded[0]["candidate_return"], 8) == 0.1
    assert round(recorded[0]["champion_return"], 8) == 0.05
    assert recorded[0]["metrics"]["return_contract"] == lifecycle.SHADOW_RETURN_CONTRACT
    assert recorded[0]["metrics"]["candidate_daily_trade_count"] == 1


def test_cooling_gate_requests_rollback_on_material_underperformance():
    rows = _shadow_rows(5, delta=-0.011)
    gate = lifecycle.cooling_rollback_gate(rows, promoted_after="2026-07-31")
    assert gate["rollback"] is True
    assert "cooling_excess_below_minus_5pct" in gate["reasons"]


def test_formal_lifecycle_opens_shadow_only_for_eligible_candidate(monkeypatch):
    validations: list[tuple[int, dict]] = []
    statuses: list[tuple[int, str]] = []
    monkeypatch.setattr(lifecycle, "_merge_validation", lambda model_id, **updates: validations.append((model_id, updates)) or updates)
    monkeypatch.setattr(
        lifecycle,
        "update_model_version_status",
        lambda model_id, status, is_active: statuses.append((model_id, status)),
    )
    outcomes = lifecycle.advance_formal_candidate_lifecycle(
        [
            {
                "role": "champion",
                "strategy_id": 1,
                "reference_contract": _formal_reference(),
                "summary": {
                    "aggregate_cost_after_return": 0.05,
                    "worst_window_drawdown": -0.12,
                    "total_fees": 1000,
                },
            },
            {
                "role": "challenger",
                "strategy_id": 2,
                "model_version_id": 9,
                "status": "passed",
                "reference_contract": _formal_reference(),
                "statistics": _formal_statistics(),
                "summary": {
                    "aggregate_cost_after_return": 0.09,
                    "worst_window_drawdown": -0.10,
                    "total_fees": 900,
                },
            },
        ],
        shadow_start_date="20260722",
        holdout_runner=lambda *_args, **_kwargs: _passed_holdout(),
    )
    assert outcomes[0]["status"] == "shadow_observation"
    assert validations[0][1]["production_eligible"] is False
    assert validations[0][1]["shadow_started_after"] == "20260722"
    assert statuses == [(9, "shadow_observation")]


def test_formal_lifecycle_blocks_for_sealed_holdout_data_without_rejecting_candidate(monkeypatch):
    validations: list[dict] = []
    monkeypatch.setattr(lifecycle, "_merge_validation", lambda _id, **updates: validations.append(updates) or updates)
    monkeypatch.setattr(lifecycle, "update_model_version_status", lambda *_args, **_kwargs: None)
    items = [
        {
            "role": "champion", "reference_contract": _formal_reference(),
            "summary": {"aggregate_cost_after_return": .08, "worst_window_drawdown": -.1, "total_fees": 700},
        },
        {
            "role": "challenger", "model_version_id": 9, "status": "passed",
            "reference_contract": _formal_reference(), "statistics": _formal_statistics(),
            "summary": {"aggregate_cost_after_return": .12, "worst_window_drawdown": -.1, "total_fees": 800},
        },
    ]
    outcome = lifecycle.advance_formal_candidate_lifecycle(
        items,
        shadow_start_date="20260731",
        holdout_runner=lambda *_args, **_kwargs: {"status": "blocked_by_data", "passed": False},
    )[0]
    assert outcome["status"] == "blocked_by_holdout_data"
    assert validations[0]["status"] == "formal_oos_blocked_by_holdout_data"
    assert validations[0]["next_gate"] == "sealed_holdout_data_repair"


def test_formal_lifecycle_does_not_expose_holdout_details_to_failure_memory(monkeypatch):
    validations: list[dict] = []
    monkeypatch.setattr(lifecycle, "_merge_validation", lambda _id, **updates: validations.append(updates) or updates)
    monkeypatch.setattr(lifecycle, "update_model_version_status", lambda *_args, **_kwargs: None)
    items = [
        {
            "role": "champion", "reference_contract": _formal_reference(),
            "summary": {"aggregate_cost_after_return": .08, "worst_window_drawdown": -.1, "total_fees": 700},
        },
        {
            "role": "challenger", "model_version_id": 9, "status": "passed",
            "reference_contract": _formal_reference(), "statistics": _formal_statistics(),
            "summary": {"aggregate_cost_after_return": .12, "worst_window_drawdown": -.1, "total_fees": 800},
        },
    ]
    outcome = lifecycle.advance_formal_candidate_lifecycle(
        items,
        shadow_start_date="20260731",
        holdout_runner=lambda *_args, **_kwargs: {
            "schema_version": "alis_sealed_strategy_holdout_v1",
            "status": "failed", "passed": False, "contract_hash": "sealed-contract",
        },
    )[0]
    assert outcome["status"] == "blocked_by_holdout"
    assert outcome["sealed_holdout"] == {
        "schema_version": "alis_sealed_strategy_holdout_v1",
        "status": "failed", "passed": False, "contract_hash": "sealed-contract",
    }
    assert validations[0]["next_gate"] == "future_independent_holdout_cohort"


def test_formal_lifecycle_preserves_candidate_when_reference_is_missing(monkeypatch):
    validations: list[tuple[int, dict]] = []
    statuses: list[tuple[int, dict]] = []
    monkeypatch.setattr(lifecycle, "_merge_validation", lambda model_id, **updates: validations.append((model_id, updates)) or updates)
    monkeypatch.setattr(lifecycle, "update_model_version_status", lambda model_id, **updates: statuses.append((model_id, updates)))

    outcomes = lifecycle.advance_formal_candidate_lifecycle(
        [{
            "role": "challenger", "model_version_id": 9, "status": "passed",
            "reference_contract": _formal_reference(),
            "summary": {"aggregate_cost_after_return": .09, "worst_window_drawdown": -.1, "total_fees": 900},
        }],
        shadow_start_date="20260731",
    )

    assert outcomes[0]["status"] == "blocked_by_reference"
    assert validations[0][1]["status"] == "formal_oos_blocked_by_reference"
    assert validations[0][1]["next_gate"] == "formal_oos_reference_rerun"
    assert statuses == [(9, {"status": "candidate", "is_active": False})]


def test_formal_lifecycle_waits_for_future_window_when_dsr_is_weak(monkeypatch):
    validations: list[dict] = []
    monkeypatch.setattr(lifecycle, "_merge_validation", lambda _id, **updates: validations.append(updates) or updates)
    monkeypatch.setattr(lifecycle, "update_model_version_status", lambda *_args, **_kwargs: None)
    items = [
        {
            "role": "champion", "reference_contract": _formal_reference(),
            "summary": {"aggregate_cost_after_return": .08, "worst_window_drawdown": -.1, "total_fees": 700},
        },
        {
            "role": "challenger", "model_version_id": 9, "status": "passed",
            "reference_contract": _formal_reference(), "statistics": _formal_statistics(passed=False),
            "summary": {"aggregate_cost_after_return": .12, "worst_window_drawdown": -.1, "total_fees": 800},
        },
    ]
    outcome = lifecycle.advance_formal_candidate_lifecycle(items, shadow_start_date="20260731")[0]
    assert outcome["status"] == "blocked_by_statistics"
    assert validations[0]["status"] == "formal_oos_blocked_by_statistics"
    assert validations[0]["formal_statistics_blocked_after"] == "20260731"
    assert validations[0]["next_gate"] == "future_formal_oos_refresh"


def test_shadow_competition_promotes_exactly_one_candidate(monkeypatch):
    rows_by_id = {
        10: _shadow_rows(20, delta=.001),
        11: _shadow_rows(20, delta=.003),
    }
    candidates = [
        {
            "id": model_id,
            "metrics": {"formal_oos": {"aggregate_excess_return": .01 + model_id / 10000}},
            "validation": {"status": "shadow_observation", "shadow_started_after": "20260731"},
            "config": {"strategy_id": model_id},
        }
        for model_id in (10, 11)
    ]
    validations = []
    statuses = []
    promotions = []
    monkeypatch.setattr(lifecycle, "audit_oos_hard_data", lambda **_kwargs: {
        "status": "ready", "window_start": "20260701", "window_end": "20260820",
    })

    class FakeSource:
        def get_available_trade_dates(self, *_args):
            return [f"2026-08-{index + 1:02d}" for index in range(20)]

    monkeypatch.setattr(lifecycle, "BacktestDataSource", FakeSource)
    monkeypatch.setattr(lifecycle, "resolve_production_strategy_champion", lambda: {
        "champion_id": 50, "strategy_id": 1,
    })
    monkeypatch.setattr(lifecycle, "list_model_versions", lambda *_args, **_kwargs: candidates)
    monkeypatch.setattr(lifecycle, "list_shadow_observations", lambda model_id, **_kwargs: rows_by_id[model_id])
    monkeypatch.setattr(lifecycle, "get_model_version", lambda model_id: {"id": model_id, "config": {"strategy_id": 1}})
    monkeypatch.setattr(lifecycle, "_merge_validation", lambda model_id, **updates: validations.append((model_id, updates)) or updates)
    monkeypatch.setattr(lifecycle, "update_model_version_status", lambda model_id, **kwargs: statuses.append((model_id, kwargs)))
    monkeypatch.setattr(lifecycle, "promote_strategy_candidate", lambda model_id, **_kwargs: promotions.append(model_id) or {
        "from_champion_version_id": 50, "to_champion_version_id": 99,
    })
    monkeypatch.setattr(lifecycle, "_record_promotion_event", lambda *_args, **_kwargs: 1)

    result = lifecycle.run_strategy_shadow_cycle()

    assert promotions == [11]
    by_id = {item["model_version_id"]: item for item in result["results"]}
    assert by_id[11]["status"] == "promoted_cooling"
    assert by_id[10]["status"] == "shadow_reserve"
    assert any(model_id == 10 and update["status"] == "shadow_reserve" for model_id, update in validations)


def test_shadow_winner_crosses_real_production_promotion_contract(monkeypatch):
    candidate = {
        "id": 11,
        "model_key": "alis_strategy_challenger",
        "version": "challenger-v11",
        "metrics": {"formal_oos": {"aggregate_excess_return": .02}},
        "validation": {"status": "shadow_observation", "shadow_started_after": "20260731"},
        "config": {"strategy_id": 11},
    }
    champion = {
        "id": 50,
        "version": "champion-v50",
        "config": {"strategy_id": 1, "role": "champion"},
    }
    versions = {11: candidate, 50: champion}
    status_updates: list[tuple[int, str, bool]] = []
    created: dict = {}

    def get_version(model_id: int):
        return versions.get(model_id)

    def update_validation(model_id: int, *, validation: dict):
        versions[model_id]["validation"] = validation

    def update_status(model_id: int, *, status: str, is_active: bool):
        versions[model_id]["status"] = status
        versions[model_id]["is_active"] = is_active
        status_updates.append((model_id, status, is_active))

    monkeypatch.setattr(lifecycle, "audit_oos_hard_data", lambda **_kwargs: {
        "status": "ready", "window_start": "20260701", "window_end": "20260820",
    })

    class FakeSource:
        def get_available_trade_dates(self, *_args):
            return [f"2026-08-{index + 1:02d}" for index in range(20)]

    monkeypatch.setattr(lifecycle, "BacktestDataSource", FakeSource)
    monkeypatch.setattr(lifecycle, "resolve_production_strategy_champion", lambda: {
        "champion_id": 50, "strategy_id": 1,
    })
    monkeypatch.setattr(lifecycle, "list_model_versions", lambda *_args, **_kwargs: [candidate])
    monkeypatch.setattr(lifecycle, "list_shadow_observations", lambda *_args, **_kwargs: _shadow_rows(20, delta=.003))
    monkeypatch.setattr(lifecycle, "get_model_version", get_version)
    monkeypatch.setattr(lifecycle, "update_model_version_validation", update_validation)
    monkeypatch.setattr(lifecycle, "update_model_version_status", update_status)
    monkeypatch.setattr(lifecycle, "_record_promotion_event", lambda *_args, **_kwargs: 1)

    monkeypatch.setattr(production_strategy, "get_model_version", get_version)
    monkeypatch.setattr(production_strategy, "get_strategy", lambda strategy_id: {
        "id": strategy_id, "name": "Candidate 11", "type": "formula", "content": {},
    })
    monkeypatch.setattr(production_strategy, "ensure_production_strategy_champion", lambda: champion)
    monkeypatch.setattr(production_strategy, "update_model_version_status", update_status)
    monkeypatch.setattr(
        production_strategy,
        "create_model_version",
        lambda **kwargs: created.update(kwargs) or 99,
    )
    monkeypatch.setattr("app.services.factor_platform.active_factor_bindings", lambda _names: {})
    monkeypatch.setattr("app.services.strategy_config.get_strategy_factor_names", lambda *_args: set())

    result = lifecycle.run_strategy_shadow_cycle()

    assert result["results"] == [{
        "model_version_id": 11,
        "status": "promoted_cooling",
        "gate": lifecycle.shadow_promotion_gate(_shadow_rows(20, delta=.003)),
        "competition_winner": True,
    }]
    assert created["model_key"] == production_strategy.CHAMPION_MODEL_KEY
    assert created["config"]["promotion"]["candidate_model_version_id"] == 11
    assert versions[11]["validation"]["status"] == "promoted_cooling"
    assert (50, "superseded", False) in status_updates


def test_shadow_passed_candidate_resumes_promotion_after_restart(monkeypatch):
    gate = lifecycle.shadow_promotion_gate(_shadow_rows(20, delta=.003))
    candidate = {
        "id": 11,
        "validation": {
            "status": "shadow_passed",
            "production_eligible": True,
            "shadow_gate": gate,
        },
        "config": {"strategy_id": 11},
    }
    validations = []
    promotions = []
    monkeypatch.setattr(lifecycle, "audit_oos_hard_data", lambda **_kwargs: {
        "status": "ready", "window_start": "20260701", "window_end": "20260820",
    })

    class FakeSource:
        def get_available_trade_dates(self, *_args):
            return ["2026-08-20"]

    monkeypatch.setattr(lifecycle, "BacktestDataSource", FakeSource)
    monkeypatch.setattr(lifecycle, "resolve_production_strategy_champion", lambda: {
        "champion_id": 50, "strategy_id": 1,
    })
    monkeypatch.setattr(lifecycle, "list_model_versions", lambda *_args, **_kwargs: [candidate])
    monkeypatch.setattr(
        lifecycle,
        "_merge_validation",
        lambda model_id, **updates: validations.append((model_id, updates)) or updates,
    )
    monkeypatch.setattr(lifecycle, "update_model_version_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(lifecycle, "_record_promotion_event", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(
        lifecycle,
        "promote_strategy_candidate",
        lambda model_id, **_kwargs: promotions.append(model_id) or {
            "from_champion_version_id": 50,
            "to_champion_version_id": 99,
        },
    )

    result = lifecycle.run_strategy_shadow_cycle(
        replay_runner=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not replay")),
    )

    assert promotions == [11]
    assert result["results"][0]["status"] == "promoted_cooling"
    assert result["results"][0]["recovered"] is True
    assert any(update.get("status") == "promoted_cooling" for _, update in validations)


def test_cooling_rollback_reactivates_best_reserve_with_fresh_shadow_floor(monkeypatch):
    validations = []
    statuses = []
    versions = [
        {
            "id": 10,
            "metrics": {"formal_oos": {"aggregate_excess_return": .02}},
            "validation": {
                "status": "shadow_reserve",
                "shadow_gate": {"compounded_delta": .03, "outperform_ratio": .7},
                "shadow_competition": {"winner_model_version_id": 11},
            },
        },
        {
            "id": 12,
            "metrics": {"formal_oos": {"aggregate_excess_return": .01}},
            "validation": {
                "status": "shadow_reserve",
                "shadow_gate": {"compounded_delta": .01, "outperform_ratio": .6},
                "shadow_competition": {"winner_model_version_id": 11},
            },
        },
        {
            "id": 13,
            "validation": {
                "status": "shadow_reserve",
                "shadow_competition": {"winner_model_version_id": 99},
            },
        },
    ]
    monkeypatch.setattr(
        lifecycle,
        "_merge_validation",
        lambda model_id, **updates: validations.append((model_id, updates)) or updates,
    )
    monkeypatch.setattr(
        lifecycle,
        "update_model_version_status",
        lambda model_id, **kwargs: statuses.append((model_id, kwargs)),
    )

    result = lifecycle.reactivate_best_shadow_reserve(
        failed_leader_id=11,
        shadow_started_after="20260901",
        versions=versions,
    )

    assert result["model_version_id"] == 10
    assert validations == [(10, {
        "status": "shadow_observation",
        "production_eligible": False,
        "shadow_started_after": "20260901",
        "shadow_observation_sessions": 0,
        "next_gate": "future_shadow_observation",
        "blocking_reasons": [],
        "reserve_reactivated_after_leader_id": 11,
    })]
    assert statuses == [(10, {"status": "shadow_observation", "is_active": False})]
