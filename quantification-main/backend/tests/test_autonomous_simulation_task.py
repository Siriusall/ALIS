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
