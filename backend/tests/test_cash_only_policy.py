from types import SimpleNamespace

import app.services.quant_task_runner as runner


def test_cash_only_day_immediately_requires_defensive_asset_evaluation(monkeypatch):
    monkeypatch.setattr(runner, "list_quant_run_task_records", lambda task_id, limit=20: [
        {"detail": {"current_positions": []}},
        {"detail": {"current_positions": []}},
    ])
    captured = {}
    monkeypatch.setattr(runner, "create_quant_run_task_record", lambda **kwargs: captured.update(kwargs) or 1)
    monkeypatch.setattr(runner, "upsert_quant_replay_waterline", lambda **kwargs: None)
    monkeypatch.setattr(runner, "update_quant_run_task", lambda *args, **kwargs: None)
    result = SimpleNamespace(
        signal_date="2026-07-14", execute_date="2026-07-15", sold_count=0,
        bought_count=0, total_buy_cost=0, execution_id=None, message="no trade",
        detail={"current_positions": [], "daily_report": {}},
    )

    runner._persist_step_result(
        task_id=7, result=result, processed_total=3, step_total=3,
        strategy_id=1, exchange_group="HS", mode="strict_replay",
    )

    policy = captured["detail"]["cash_only_policy"]
    assert policy["consecutive_cash_days"] == 3
    assert policy["status"] == "blocked"
    assert policy["defensive_asset_evaluation"]["status"] == "required"
    assert "当日立即" in policy["defensive_asset_evaluation"]["decision"]
    assert captured["detail"]["daily_report"]["cash_only_policy"] == policy
