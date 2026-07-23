from __future__ import annotations

from datetime import date, timedelta

from app.services import walk_forward_validation as wf


def _dates(count: int) -> list[str]:
    start = date(2020, 1, 1)
    return [(start + timedelta(days=index)).isoformat() for index in range(count)]


class FakeDates:
    def __init__(self, dates: list[str]):
        self.dates = dates

    def get_available_trade_dates(self, start: str, end: str) -> list[str]:
        return self.dates


def test_window_builder_enforces_purge_and_embargo_boundaries():
    dates = _dates(80)
    config = wf.WalkForwardConfig(train_sessions=20, validation_sessions=10, test_sessions=10, purge_sessions=3, embargo_sessions=2, step_sessions=10, minimum_windows=3)
    windows = wf.build_purged_walk_forward_windows(dates, config)
    assert len(windows) == 4
    first = windows[0]
    assert first["train"]["end"] < first["purge"]["start"]
    assert first["purge"]["end"] < first["validation"]["start"]
    assert first["validation"]["end"] < first["embargo"]["start"]
    assert first["embargo"]["end"] < first["test"]["start"]


def test_walk_forward_binds_champion_and_applies_promotion_gates(monkeypatch):
    dates = _dates(90)
    monkeypatch.setattr(wf, "resolve_production_strategy_champion", lambda: {
        "champion_id": 42, "champion_version": "champion-v1", "strategy_controller_version": "controller-v1",
    })

    def replay_runner(**kwargs):
        test_dates = [value for value in dates if kwargs["start_date"] <= value <= kwargs["end_date"]]
        equity = [{"date": value, "total_value": 100_000 + index * 100} for index, value in enumerate(test_dates)]
        return {
            "production_strategy_version": "champion-v1",
            "metrics": {"total_return": .02, "benchmark_return": .01, "excess_return": .01, "max_drawdown": -.03},
            "summary": {"total_fees": 20, "trading_days": len(test_dates), "decision_count": max(1, len(test_dates) - 1)}, "equity_series": equity,
            "data_quality": {"point_in_time": {"passed": True}},
        }

    state_cycle = ("S0", "S1", "S2", "S3")
    result = wf.run_purged_walk_forward_validation(
        config=wf.WalkForwardConfig(train_sessions=20, validation_sessions=5, test_sessions=8, purge_sessions=2, embargo_sessions=1, step_sessions=8, minimum_windows=5),
        data_source=FakeDates(dates), replay_runner=replay_runner,
        state_loader=lambda values: {value: state_cycle[index % 4] for index, value in enumerate(values)},
    )
    assert result["executed_window_count"] >= 5
    assert result["status"] == "passed"
    assert result["production_strategy_version"] == "champion-v1"
    assert all(result["promotion_gates"].values())


def test_default_protocol_reports_insufficient_history_for_fewer_than_five_windows(monkeypatch):
    monkeypatch.setattr(wf, "resolve_production_strategy_champion", lambda: {
        "champion_id": 42, "champion_version": "champion-v1", "strategy_controller_version": "controller-v1",
    })
    result = wf.run_purged_walk_forward_validation(data_source=FakeDates(_dates(1514)), execute=False)
    assert result["available_window_count"] == 4
    assert result["required_window_count"] == 5
    assert result["capacity_status"] == "insufficient_data"


def test_preview_identifies_explicit_challenger(monkeypatch):
    monkeypatch.setattr(wf, "resolve_production_strategy_champion", lambda: {
        "champion_id": 42, "champion_version": "champion-v1",
        "strategy_controller_version": "controller-v1", "strategy_id": 1,
    })
    result = wf.run_purged_walk_forward_validation(
        data_source=FakeDates(_dates(1537)), strategy_id=2, execute=False,
    )
    assert result["evaluated_strategy_id"] == 2
    assert result["evaluation_role"] == "challenger"


def test_walk_forward_emits_window_progress_and_resumes_checkpoint(monkeypatch):
    dates = _dates(90)
    monkeypatch.setattr(wf, "resolve_production_strategy_champion", lambda: {
        "champion_id": 42, "champion_version": "champion-v1",
        "strategy_controller_version": "controller-v1", "strategy_id": 1,
    })
    calls: list[str] = []

    def replay_runner(**kwargs):
        calls.append(kwargs["start_date"])
        test_dates = [value for value in dates if kwargs["start_date"] <= value <= kwargs["end_date"]]
        return {
            "production_strategy_version": "champion-v1",
            "metrics": {"total_return": .01, "benchmark_return": 0, "excess_return": .01, "max_drawdown": -.01},
            "summary": {"total_fees": 1, "trading_days": len(test_dates), "decision_count": 1},
            "equity_series": [{"date": value, "total_value": 100_000 + index} for index, value in enumerate(test_dates)],
            "data_quality": {"point_in_time": {"passed": True}},
        }

    config = wf.WalkForwardConfig(
        train_sessions=20, validation_sessions=5, test_sessions=8,
        purge_sessions=2, embargo_sessions=1, step_sessions=8, minimum_windows=2,
    )
    events: list[dict] = []
    first = wf.run_purged_walk_forward_validation(
        config=config, max_windows=2, data_source=FakeDates(dates),
        replay_runner=replay_runner, state_loader=lambda values: {},
        progress_callback=events.append,
    )
    assert [event["completed_windows"] for event in events] == [1, 2]
    checkpoint = [first["windows"][0]]
    calls.clear()
    events.clear()
    resumed = wf.run_purged_walk_forward_validation(
        config=config, max_windows=2, data_source=FakeDates(dates),
        replay_runner=replay_runner, state_loader=lambda values: {},
        progress_callback=events.append, checkpoint_results=checkpoint,
    )
    assert len(calls) == 1
    assert len(resumed["windows"]) == 2
    assert events[0]["completed_windows"] == 2
