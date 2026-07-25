from app.services import autonomous_healing as healing


def test_classify_step_health_distinguishes_degraded_and_observing() -> None:
    assert healing.classify_step_health({"ok": True, "result": {"status": "partial"}})["status"] == "degraded"
    assert healing.classify_step_health({"ok": True, "result": {"status": "escalated"}})["status"] == "degraded"
    assert healing.classify_step_health({"ok": True, "result": {"status": "attention"}})["status"] == "observing"
    assert healing.classify_step_health({"ok": True, "result": {"status": "insufficient_observations"}})["status"] == "observing"
    assert healing.classify_step_health({"ok": False, "error": "network"})["status"] == "failed"


def test_self_healing_repairs_only_explicit_hard_data_gaps(monkeypatch) -> None:
    audits = iter([
        {"status": "blocked", "dates": [{"trade_date": "20260701", "eligible": False, "missing_apis": ["daily"]}]},
        {"status": "ready"},
    ])
    monkeypatch.setattr("app.data_platform.services.oos_hard_data.audit_oos_hard_data", lambda **_: next(audits))
    monkeypatch.setattr("app.data_platform.services.oos_hard_data.repair_oos_hard_data_batch", lambda dates, **kwargs: {"ok": True, "dates": dates})

    result = healing.run_autonomous_self_healing([
        {"name": "hard_data_gate", "ok": True, "result": {"status": "blocked"}},
        {"name": "factor_daily_cycle", "ok": True, "result": {"status": "completed", "materialization": {"point_in_time": {"passed": True}}}},
    ])

    assert result["status"] == "healthy"
    assert result["components"]["hard_data_gate"]["status"] == "recovered"
    assert result["actions"][0]["dates"] == ["20260701"]


def test_self_healing_repairs_sealed_holdout_in_bounded_batch(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.sealed_strategy_holdout.repair_sealed_strategy_holdout",
        lambda **kwargs: {"status": "repaired_batch", "dates": ["20190910"] * kwargs["limit"]},
    )
    monkeypatch.setattr(
        "app.services.sealed_strategy_holdout.audit_sealed_strategy_holdout",
        lambda **_kwargs: {"status": "blocked", "eligible_sessions": 6, "required_sessions": 126},
    )

    result = healing.run_autonomous_self_healing([
        {"name": "sealed_holdout_data_gate", "ok": True, "result": {"status": "blocked"}},
    ])

    action = result["actions"][0]
    assert action["kind"] == "sealed_strategy_holdout"
    assert len(action["dates"]) == healing.MAX_OOS_REPAIR_DATES
    assert action["verification"]["eligible_sessions"] == 6


def test_sealed_adj_factor_bulk_task_dedupe_tracks_the_actual_gap(monkeypatch) -> None:
    created = []
    monkeypatch.setattr(
        "app.services.sealed_strategy_holdout.sealed_holdout_adj_factor_only_gap",
        lambda: {"homogeneous": True, "repair_dates": ["20190910", "20190911"], "eligible_sessions": 124, "required_sessions": 126},
    )
    monkeypatch.setattr(
        "app.services.sealed_strategy_holdout.audit_sealed_strategy_holdout",
        lambda **_kwargs: {"status": "blocked", "eligible_sessions": 124, "required_sessions": 126},
    )
    monkeypatch.setattr(
        "app.services.evolution_orchestrator.create_evolution_task",
        lambda **kwargs: created.append(kwargs) or {"id": 88, "status": "queued", "progress": 0},
    )

    healing.run_autonomous_self_healing([
        {"name": "sealed_holdout_data_gate", "ok": True, "result": {"status": "blocked"}},
    ])

    assert created[0]["dedupe_key"].startswith("sealed_holdout_adj_factor_backfill:v2:")
    assert created[0]["dedupe_key"] != "sealed_holdout_adj_factor_backfill:v1"


def test_self_healing_quarantines_ledger_drift_without_mutating_it() -> None:
    result = healing.run_autonomous_self_healing([
        {"name": "simulation_ledger_audit", "ok": True, "result": {"status": "failed"}},
    ])

    assert result["status"] == "blocked"
    assert result["actions"][0]["status"] == "quarantined"


def test_self_healing_schedules_existing_daily_update_for_decision_data_drift(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.data_platform.services.daily_update.daily_update_status",
        lambda: {"latest_decision_ready_trade_date": "20260705"},
    )
    monkeypatch.setattr(
        "app.data_platform.services.daily_update.start_daily_update",
        lambda: {"ok": True, "running": True},
    )

    result = healing.run_autonomous_self_healing([
        {"name": "decision_data_gate", "ok": False, "result": {"status": "blocked"}},
    ])

    assert result["status"] == "blocked"
    assert result["actions"] == [{
        "kind": "latest_decision_data",
        "status": "refresh_scheduled",
        "before": {"latest_decision_ready_trade_date": "20260705"},
        "start": {"ok": True, "running": True},
        "after": {"latest_decision_ready_trade_date": "20260705"},
        "verification": {"refresh_running": False, "latest_decision_ready_trade_date": "20260705"},
    }]


def test_self_healing_does_not_duplicate_a_factor_step_still_running_after_timeout(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.factor_platform.run_factor_daily_cycle",
        lambda: (_ for _ in ()).throw(AssertionError("timed-out factor step must not be re-entered")),
    )

    result = healing.run_autonomous_self_healing([
        {"name": "hard_data_gate", "ok": True, "result": {"status": "ready"}},
        {
            "name": "factor_daily_cycle",
            "ok": False,
            "error": "step_timeout_after_120s",
            "result": {"status": "timed_out", "timeout_seconds": 120},
        },
    ])

    assert result["status"] == "blocked"
    assert not any(action.get("kind") == "factor_daily_cycle" for action in result["actions"])
