from app.services import autonomous_healing as healing


def test_classify_step_health_distinguishes_degraded_and_observing() -> None:
    assert healing.classify_step_health({"ok": True, "result": {"status": "partial"}})["status"] == "degraded"
    assert healing.classify_step_health({"ok": True, "result": {"status": "escalated"}})["status"] == "degraded"
    assert healing.classify_step_health({"ok": True, "result": {"status": "attention"}})["status"] == "observing"
    assert healing.classify_step_health({"ok": True, "result": {"status": "insufficient_observations"}})["status"] == "observing"
    assert healing.classify_step_health({"ok": False, "error": "network"})["status"] == "failed"


def test_classify_step_health_never_reports_timeout_as_healthy() -> None:
    assert healing.classify_step_health({"ok": True, "result": {"status": "timed_out"}})["status"] == "failed"
    assert healing.classify_step_health({"ok": True, "result": {"status": "timeout"}})["status"] == "failed"


def test_classify_step_health_never_reports_retryable_data_gap_as_healthy() -> None:
    for status in ("completed", "observing", "ready"):
        result = healing.classify_step_health({
            "ok": True,
            "result": {
                "status": status,
                "failure_category": "execution_slice_blocked",
            },
        })
        assert result == {"status": "failed", "reason": "execution_slice_blocked"}


def test_classify_step_health_keeps_explicit_oos_accumulation_as_waiting_condition() -> None:
    result = healing.classify_step_health({
        "name": "hard_data_gate", "ok": True,
        "result": {"status": "accumulating", "non_repairable_dates": ["20260701"], "repair_dates": []},
    })
    assert result == {"status": "waiting_condition", "reason": "accumulating"}


def test_classify_historical_gate_waits_only_for_explicit_normal_statuses() -> None:
    for status in ("accumulating", "retired", "retired_legacy_oos_contract"):
        result = healing.classify_step_health({
            "name": "sealed_holdout_data_gate", "ok": True,
            "result": {"status": status, "non_repairable_dates": ["20190910"], "repair_dates": []},
        })
        assert result == {"status": "waiting_condition", "reason": status}

    blocked = healing.classify_step_health({
        "name": "sealed_holdout_data_gate", "ok": True,
        "result": {"status": "blocked", "non_repairable_dates": ["20190910"], "repair_dates": []},
    })
    assert blocked == {"status": "failed", "reason": "blocked"}


def test_historical_gate_failure_evidence_precedes_wait_status() -> None:
    cases = [
        (
            {"name": "hard_data_gate", "ok": True, "result": {
                "status": "accumulating", "failure_category": "trade_calendar_unavailable",
            }},
            "trade_calendar_unavailable",
        ),
        (
            {"name": "hard_data_gate", "ok": False, "result": {"status": "accumulating"}},
            "module_returned_not_ok",
        ),
        (
            {"name": "sealed_holdout_data_gate", "ok": True, "result": {
                "status": "accumulating", "backend_available": False,
            }},
            "historical_data_backend_unavailable",
        ),
    ]
    for step, reason in cases:
        assert healing.classify_step_health(step) == {"status": "failed", "reason": reason}


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


def test_self_healing_does_not_retry_historical_point_in_time_gaps(monkeypatch) -> None:
    audit = {
        "status": "blocked", "non_repairable_dates": ["20260701"],
        "dates": [{"trade_date": "20260701", "eligible": False, "repairable": False}],
    }
    monkeypatch.setattr("app.data_platform.services.oos_hard_data.audit_oos_hard_data", lambda **_: audit)
    monkeypatch.setattr(
        "app.data_platform.services.oos_hard_data.repair_oos_hard_data_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not retry late historical data")),
    )

    result = healing.run_autonomous_self_healing([
        {"name": "hard_data_gate", "ok": True, "result": {"status": "accumulating", "non_repairable_dates": ["20260701"], "repair_dates": []}},
    ])

    assert result["status"] == "healthy"
    assert result["actions"] == []
    assert result["components"]["hard_data_gate"]["status"] == "waiting_condition"


def test_factor_repair_uses_current_experiment_gate_not_historical_oos_gate(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "app.services.factor_platform.run_factor_daily_cycle",
        lambda: calls.append("factor") or {"materialization": {"point_in_time": {"passed": True}}},
    )

    result = healing.run_autonomous_self_healing([
        {
            "name": "hard_data_gate", "ok": True,
            "result": {"status": "accumulating", "non_repairable_dates": ["20260701"], "repair_dates": []},
        },
        {
            "name": "decision_data_gate", "ok": True,
            "result": {"status": "ready", "decision_ready": True, "experiment_ready": True},
        },
        {
            "name": "factor_daily_cycle", "ok": False,
            "error": "point_in_time_contract_failed",
            "result": {"status": "failed"},
        },
    ])

    assert calls == ["factor"]
    assert result["components"]["factor_daily_cycle"]["status"] == "recovered"


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
