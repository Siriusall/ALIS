import pytest

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

    monkeypatch.setattr(runner, "create_quant_run_task", fake_create)
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
        "replay_mode": "autonomous_daily",
    }


def test_autonomous_plan_uses_only_plan_due_on_latest_session(monkeypatch):
    monkeypatch.setattr(
        "app.services.profit_loop.list_pending_daily_rebalance_execution_inputs",
        lambda: [
            {"signal_date": "20260724", "execute_date": "20260727"},
            {"signal_date": "20260723", "execute_date": "20260724"},
        ],
    )

    plan = runner._build_autonomous_plan_context([
        "2026-07-23", "2026-07-24", "2026-07-27",
    ])

    assert plan["ok"] is True
    assert plan["start_idx"] == 1
    assert plan["current_execute_date"] == "2026-07-27"


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


@pytest.mark.parametrize(
    "materialization",
    [
        {"status": "compatibility_preview_only", "persisted": False},
        {"status": "blocked_by_decision_data", "persisted": True},
    ],
)
def test_due_prior_plan_is_independent_of_today_materialization(
    monkeypatch,
    materialization,
):
    monkeypatch.setattr(
        "app.services.profit_loop.materialize_daily_rebalance_plan",
        lambda: materialization,
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
    assert context["today_materialization"] == materialization


def test_autonomous_daily_mode_is_not_downgraded_to_replay():
    assert runner._normalized_replay_mode("autonomous_daily") == "autonomous_daily"
    assert runner._normalized_replay_mode("unknown") == "pragmatic_recover"
