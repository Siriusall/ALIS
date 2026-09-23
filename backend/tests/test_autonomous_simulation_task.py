import pytest
from contextlib import nullcontext

from app.services import production_strategy
from app.services import quant_task_runner as runner


def test_ensure_autonomous_simulation_task_reuses_scheduler_owned_task(monkeypatch):
    existing = {
        "id": 91,
        "status": "paused",
        "replay_mode": "autonomous_daily",
        "strategy_id": 1,
        "top_n": 20,
    }
    monkeypatch.setattr(
        production_strategy,
        "resolve_production_strategy_champion",
        lambda: {
            "strategy_id": 1,
            "champion_version": "champion-v1",
            "strategy": {"content": {"rebalance": {"top_n": 20}}},
            "strategy_policy": {},
        },
    )
    monkeypatch.setattr(runner, "list_active_quant_run_tasks", lambda: [existing])

    result = runner.ensure_autonomous_simulation_task()

    assert result["created"] is False
    assert result["task"] == existing


def test_startup_reconciles_abandoned_stopping_task(monkeypatch):
    stopping = {
        "id": 93,
        "status": "stopping",
        "replay_mode": "autonomous_daily",
        "strategy_id": 1,
        "top_n": 20,
    }
    transitions = []
    monkeypatch.setattr(runner, "list_active_quant_run_tasks", lambda: [stopping])
    monkeypatch.setattr(
        runner,
        "_transition_task_status",
        lambda task_id, target, event, **fields: transitions.append((task_id, target, event, fields)) or True,
    )

    result = runner.reconcile_quant_tasks_on_startup()

    assert result["stopping_finalized"] == 1
    assert transitions[0][0:3] == (93, "stopped", "manual_stop_finalize")
    assert transitions[0][3]["replay_status"] == "stopped_after_restart"


def test_ensure_autonomous_simulation_task_starts_from_latest_data(monkeypatch):
    captured = {}
    monkeypatch.setattr(runner, "list_active_quant_run_tasks", lambda: [])
    monkeypatch.setattr(runner, "list_consolidated_trade_dates", lambda: ["20260721", "20260722"])
    monkeypatch.setattr(
        production_strategy,
        "resolve_production_strategy_champion",
        lambda: {
            "strategy_id": 7,
            "champion_version": "alis-production-v3",
            "strategy_policy": {"top_n": 12},
            "strategy": {},
        },
    )

    def fake_create(**kwargs):
        captured.update(kwargs)
        return 92

    monkeypatch.setattr(runner, "create_or_reuse_autonomous_quant_run_task", lambda **kwargs: (fake_create(**kwargs), True))
    monkeypatch.setattr(
        runner,
        "get_quant_run_task",
        lambda task_id: {"id": task_id, "status": "pending", "replay_mode": "autonomous_daily"},
    )

    result = runner.ensure_autonomous_simulation_task()

    assert result["created"] is True
    assert result["task"]["id"] == 92
    assert captured == {
        "strategy_id": 7,
        "top_n": 12,
        "budget_ratio": 1.0,
        "exchange_group": "HS",
        "start_date": "20260722",
        "max_steps": 1,
    }


def test_autonomous_plan_uses_only_plan_due_on_latest_session(monkeypatch):
    monkeypatch.setattr(
        "app.services.profit_loop.list_pending_daily_rebalance_execution_inputs",
        lambda: [
            {
                "signal_date": "20260724",
                "execute_date": "20260727",
                "required_defensive_ts_codes": ["510880.SH"],
            },
            {"signal_date": "20260723", "execute_date": "20260724"},
        ],
    )

    plan = runner._build_autonomous_plan_context([
        "2026-07-23", "2026-07-24", "2026-07-27",
    ])

    assert plan["ok"] is True
    assert plan["start_idx"] == 1
    assert plan["current_execute_date"] == "2026-07-27"
    assert plan["required_defensive_ts_codes"] == ["510880.SH"]


def test_trade_date_identity_accepts_compact_and_hyphenated_values():
    assert runner._trade_date_key("20260730") == "20260730"
    assert runner._trade_date_key("2026-07-30") == "20260730"
    assert runner._trade_date_key("not-a-date") == ""


def test_waiting_schedule_projects_next_frozen_plan(monkeypatch):
    class Connection:
        def execute(self, *_args, **_kwargs):
            return self

        def fetchone(self):
            return {"trade_date": "20260811", "planned_execute_date": "20260812"}

    monkeypatch.setattr(runner, "business_conn", lambda: nullcontext(Connection()))

    assert runner._waiting_schedule_projection() == {
        "replay_gap_days": 1,
        "replay_current_execute_date": "20260812",
        "replay_target_execute_date": "20260812",
        "last_message": "等待正式计划 20260811→20260812 到期执行",
    }


def test_waiting_schedule_clears_dates_without_frozen_plan(monkeypatch):
    class Connection:
        def execute(self, *_args, **_kwargs):
            return self

        def fetchone(self):
            return None

    monkeypatch.setattr(runner, "business_conn", lambda: nullcontext(Connection()))

    projection = runner._waiting_schedule_projection()

    assert projection["replay_gap_days"] == 0
    assert projection["replay_current_execute_date"] is None
    assert projection["replay_target_execute_date"] is None


def test_autonomous_plan_never_executes_stale_plan_late(monkeypatch):
    monkeypatch.setattr(
        "app.services.profit_loop.list_pending_daily_rebalance_execution_inputs",
        lambda: [{"signal_date": "20260723", "execute_date": "20260724"}],
    )

    plan = runner._build_autonomous_plan_context([
        "2026-07-23", "2026-07-24", "2026-07-27",
    ])

    assert plan == {"ok": False, "reason": "no_due_formal_plan"}


def test_autonomous_plan_waits_when_no_due_formal_plan(monkeypatch):
    monkeypatch.setattr(
        "app.services.profit_loop.list_pending_daily_rebalance_execution_inputs",
        lambda: [{"signal_date": "20260724", "execute_date": "20260727"}],
    )

    plan = runner._build_autonomous_plan_context(["2026-07-24"])

    assert plan == {"ok": False, "reason": "no_due_formal_plan"}


def test_due_prior_plan_has_priority_over_today_materialization(monkeypatch):
    called = {"materialize": 0}

    def materialize():
        called["materialize"] += 1
        return {"status": "unexpected"}

    monkeypatch.setattr(
        "app.services.profit_loop.materialize_daily_rebalance_plan",
        materialize,
    )
    monkeypatch.setattr(
        "app.services.profit_loop.list_pending_daily_rebalance_execution_inputs",
        lambda: [{"signal_date": "20260724", "execute_date": "20260727"}],
    )

    context = runner._materialize_current_plan_and_find_due_context([
        "2026-07-24", "2026-07-25", "2026-07-27",
    ])

    assert context["ok"] is True
    assert context["start_idx"] == 0
    assert context["current_execute_date"] == "2026-07-27"
    assert context["today_materialization"] == {
        "ok": True,
        "status": "deferred_until_due_plan_processed",
        "reason": "due_frozen_plan_has_execution_priority",
    }
    assert called["materialize"] == 0


def test_due_plan_is_not_blocked_by_today_decision_data(monkeypatch):
    monkeypatch.setattr(
        runner,
        "_build_autonomous_plan_context",
        lambda _dates: {
            "ok": True,
            "signal_date": "20260817",
            "current_execute_date": "20260818",
        },
    )
    monkeypatch.setattr(
        runner,
        "_materialize_current_plan_and_find_due_context",
        lambda _dates: (_ for _ in ()).throw(AssertionError("must not materialize today first")),
    )

    context = runner._resolve_autonomous_plan_context(
        ["2026-08-17", "2026-08-18"],
        decision_data_ready=False,
    )

    assert context["ok"] is True
    assert context["current_execute_date"] == "20260818"
    assert context["today_materialization"]["status"] == "deferred_until_due_plan_processed"


def test_no_due_plan_materializes_latest_completed_signal_after_wall_clock_rollover(monkeypatch):
    monkeypatch.setattr(
        runner,
        "_build_autonomous_plan_context",
        lambda _dates: {"ok": False, "reason": "no_due_formal_plan"},
    )
    calls = []
    monkeypatch.setattr(
        runner,
        "_materialize_current_plan_and_find_due_context",
        lambda dates: calls.append(dates) or {"ok": False, "reason": "no_due_formal_plan"},
    )

    context = runner._resolve_autonomous_plan_context(
        ["2026-08-18"],
        decision_data_ready=False,
    )

    assert context["ok"] is False
    assert calls == [["2026-08-18"]]


def test_today_materializes_when_no_plan_is_due(monkeypatch):
    runner._plan_materialization_retry_after.clear()
    materialization = {"status": "planned", "persisted": True}
    captured = {}
    monkeypatch.setattr(
        "app.services.profit_loop.materialize_daily_rebalance_plan",
        lambda **kwargs: captured.update(kwargs) or materialization,
    )
    monkeypatch.setattr(
        "app.services.profit_loop.list_pending_daily_rebalance_execution_inputs",
        lambda: [],
    )

    context = runner._materialize_current_plan_and_find_due_context(["2026-07-27"])

    assert context["ok"] is False
    assert context["today_materialization"] == materialization
    assert captured["signal_date"] == "2026-07-27"


def test_materialization_uses_latest_decision_ready_signal_date(monkeypatch):
    runner._plan_materialization_retry_after.clear()
    captured = {}
    monkeypatch.setattr(
        runner,
        "_latest_completed_signal_date",
        lambda dates: "2026-09-22",
    )
    monkeypatch.setattr(
        "app.services.profit_loop.list_pending_daily_rebalance_execution_inputs",
        lambda: [],
    )
    monkeypatch.setattr(
        "app.services.profit_loop.materialize_daily_rebalance_plan",
        lambda **kwargs: captured.update(kwargs) or {"status": "planned", "persisted": True},
    )

    context = runner._materialize_current_plan_and_find_due_context([
        "2026-09-22", "2026-09-23",
    ])

    assert context["today_materialization"]["persisted"] is True
    assert captured["signal_date"] == "2026-09-22"


def test_plan_query_timeout_enters_bounded_cooldown(monkeypatch):
    runner._plan_materialization_retry_after.clear()
    calls = {"count": 0}

    def timeout():
        calls["count"] += 1
        raise RuntimeError("canceling statement due to statement timeout")

    monkeypatch.setattr(
        "app.services.profit_loop.materialize_daily_rebalance_plan",
        lambda **_kwargs: timeout(),
    )
    monkeypatch.setattr(
        "app.services.profit_loop.list_pending_daily_rebalance_execution_inputs",
        lambda: [],
    )

    first = runner._materialize_current_plan_and_find_due_context(["2026-08-20"])
    second = runner._materialize_current_plan_and_find_due_context(["2026-08-20"])

    assert first["today_materialization"]["status"] == "database_timeout_cooldown"
    assert second["today_materialization"]["status"] == "database_timeout_cooldown"
    assert calls["count"] == 1
    runner._plan_materialization_retry_after.clear()


def test_successful_progress_ignores_uncommitted_task_fields():
    signal, execute = runner._successful_task_progress(
        [{"signal_date": "2026-08-07", "execute_date": "2026-08-10"}],
        {
            "last_successful_signal_date": "2026-08-06",
            "last_successful_execute_date": "2026-08-07",
        },
    )

    assert signal == "2026-08-07"
    assert execute == "2026-08-10"


def test_autonomous_daily_mode_is_not_downgraded_to_replay():
    assert runner._normalized_replay_mode("autonomous_daily") == "autonomous_daily"
    assert runner._normalized_replay_mode("unknown") == "pragmatic_recover"


def test_autonomous_execution_data_gate_cannot_be_disabled_by_runtime_config(monkeypatch):
    task_id = 97
    task = {
        "id": task_id,
        "status": "pending",
        "strategy_id": 3,
        "top_n": 10,
        "budget_ratio": 1.0,
        "exchange_group": "HS",
        "replay_mode": "autonomous_daily",
    }
    gate_calls = []
    pauses = []
    execution_attempted = []

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(runner.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(runner, "get_quant_run_task", lambda _task_id: dict(task))
    monkeypatch.setattr(
        runner,
        "get_quant_run_task_runtime_config",
        lambda _task_id: {"enable_data_health_gate": False},
    )
    monkeypatch.setattr(runner, "upsert_quant_run_task_health", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "get_quant_run_task_health",
        lambda _task_id: {
            "last_step_latency_ms": 0,
            "avg_step_latency_ms": 0,
            "step_count": 0,
            "risk_hit_count": 0,
            "consecutive_pause_count": 0,
        },
    )
    monkeypatch.setattr(runner, "_transition_task_status", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(runner, "claim_quant_run_task_start", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(runner, "evaluate_model_readiness_gate", runner.no_pause)
    monkeypatch.setattr(runner, "require_consolidated_trade_dates", lambda **_kwargs: ["20260810", "20260811"])
    monkeypatch.setattr(runner, "list_quant_run_task_records", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(runner, "get_quant_replay_waterline", lambda **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "_build_autonomous_plan_context",
        lambda _dates: {
            "ok": True,
            "start_idx": 0,
            "batch_end_idx": 0,
            "target_end_idx": 0,
            "gap_days": 1,
            "batch_steps": 1,
            "current_execute_date": "20260811",
            "target_execute_date": "20260811",
            "required_defensive_ts_codes": ["510880.SH"],
        },
    )

    def blocked_execution_gate(execute_date, defensive_ts_codes):
        gate_calls.append((execute_date, list(defensive_ts_codes)))
        return runner.pause_decision(
            code="paused_frozen_execution_data_not_ready",
            message="冻结执行数据未就绪",
            recovery_condition="补齐冻结执行数据后自动重试",
            exit_condition="当日无法补齐则记录漏跑",
        )

    monkeypatch.setattr(runner, "_evaluate_frozen_execution_data_gate", blocked_execution_gate)
    monkeypatch.setattr(runner, "_ensure_close_data_ready", lambda: None)
    monkeypatch.setattr(runner, "_refresh_missing_frozen_defensive_assets", lambda *_args: None)
    monkeypatch.setattr(
        runner,
        "_handle_pause",
        lambda _task_id, **kwargs: pauses.append(kwargs),
    )
    monkeypatch.setattr(runner, "_preflight_check_step", lambda **_kwargs: runner.no_pause())

    def attempted_execution(**_kwargs):
        execution_attempted.append(True)
        raise AssertionError("frozen plan must not be claimed when T+1 data is incomplete")

    monkeypatch.setattr(runner, "_execute_step_with_retry", attempted_execution)

    assert runner.run_quant_task_async(task_id) is True
    assert gate_calls == [("20260811", ["510880.SH"])]
    assert [item["replay_status"] for item in pauses] == ["paused_frozen_execution_data_not_ready"]
    assert execution_attempted == []
