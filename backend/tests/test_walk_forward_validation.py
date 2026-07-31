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


def test_failure_attribution_measures_cash_drag_and_blocked_order_codes():
    replay = {
        "initial_cash": 100,
        "metrics": {"excess_return": -.11, "turnover_rate": .25},
        "summary": {"total_fees": 1, "trade_count": 3, "decision_count": 2},
        "equity_series": [
            {"date": "2026-01-01", "total_value": 100, "cash": 50, "stock_value": 50},
            {"date": "2026-01-02", "total_value": 105, "cash": 50, "stock_value": 55},
            {"date": "2026-01-03", "total_value": 110, "cash": 40, "stock_value": 70},
        ],
        "benchmark_series": [
            {"date": "2026-01-01", "total_value": 100},
            {"date": "2026-01-02", "total_value": 110},
            {"date": "2026-01-03", "total_value": 121},
        ],
        "blocked_events": [
            {"code": "limit_up"}, {"code": "limit_up"}, {"code": "insufficient_cash"},
        ],
    }

    result = wf._failure_attribution(replay, {"2026-01-02": "S0", "2026-01-03": "S0"})

    assert result["status"] == "complete"
    assert result["primary_cause"] == "market_exposure"
    assert result["evidence"]["average_cash_weight"] == 0.48809524
    assert result["evidence"]["estimated_cash_drag_return"] == 0.09761905
    assert result["evidence"]["blocked_by_code"] == {"insufficient_cash": 1, "limit_up": 2}


def test_failure_attribution_keeps_unknown_when_benchmark_path_is_missing():
    result = wf._failure_attribution({
        "metrics": {"excess_return": -.08},
        "summary": {"decision_count": 1},
        "equity_series": [
            {"date": "2026-01-01", "total_value": 100, "cash": 20},
            {"date": "2026-01-02", "total_value": 92, "cash": 20},
        ],
    }, {})

    assert result["status"] == "partial"
    assert result["primary_cause"] == "unknown"
    assert any("基准净值" in item for item in result["limitations"])


def test_paired_daily_returns_freezes_aligned_non_overlapping_returns():
    result = wf._paired_daily_returns({
        "equity_series": [
            {"date": "2026-01-01", "total_value": 100},
            {"date": "2026-01-02", "total_value": 102},
            {"date": "2026-01-03", "total_value": 101},
        ],
        "benchmark_series": [
            {"date": "2026-01-01", "total_value": 100},
            {"date": "2026-01-02", "total_value": 101},
            {"date": "2026-01-03", "total_value": 103},
        ],
    })
    assert result["status"] == "complete"
    assert result["observation_count"] == 2
    assert result["observations"][0] == {
        "date": "2026-01-02", "strategy_return": .02, "benchmark_return": .01,
    }


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
            "data_quality": {"point_in_time": {"passed": True}, "benchmark": {"passed": True}},
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


def test_walk_forward_blocks_any_window_with_incomplete_benchmark(monkeypatch):
    dates = _dates(90)
    monkeypatch.setattr(wf, "resolve_production_strategy_champion", lambda: {
        "champion_id": 42,
        "champion_version": "champion-v1",
        "strategy_controller_version": "controller-v1",
    })
    calls = 0

    def replay_runner(**kwargs):
        nonlocal calls
        calls += 1
        test_dates = [
            value for value in dates
            if kwargs["start_date"] <= value <= kwargs["end_date"]
        ]
        return {
            "production_strategy_version": "champion-v1",
            "metrics": {
                "total_return": .02,
                "benchmark_return": .01,
                "excess_return": .01,
                "max_drawdown": -.03,
            },
            "summary": {
                "total_fees": 20,
                "trading_days": len(test_dates),
                "decision_count": max(1, len(test_dates) - 1),
            },
            "equity_series": [
                {"date": value, "total_value": 100_000 + index * 100}
                for index, value in enumerate(test_dates)
            ],
            "data_quality": {
                "point_in_time": {"passed": True},
                "benchmark": {
                    "passed": calls != 1,
                    "missing_dates": [test_dates[-1]] if calls == 1 else [],
                },
            },
        }

    result = wf.run_purged_walk_forward_validation(
        config=wf.WalkForwardConfig(
            train_sessions=20,
            validation_sessions=5,
            test_sessions=8,
            purge_sessions=2,
            embargo_sessions=1,
            step_sessions=8,
            minimum_windows=5,
        ),
        data_source=FakeDates(dates),
        replay_runner=replay_runner,
        state_loader=lambda values: {},
    )

    assert result["status"] == "insufficient_data"
    assert result["promotion_gates"]["benchmark_data_all_windows"] is False
    assert result["windows"][0]["benchmark_data_complete"] is False
    assert "benchmark_data_all_windows" in result["blocking_reasons"]


def test_default_protocol_reports_insufficient_history_for_fewer_than_five_windows(monkeypatch):
    monkeypatch.setattr(wf, "resolve_production_strategy_champion", lambda: {
        "champion_id": 42, "champion_version": "champion-v1", "strategy_controller_version": "controller-v1",
    })
    result = wf.run_purged_walk_forward_validation(data_source=FakeDates(_dates(1514)), execute=False)
    assert result["available_window_count"] == 4
    assert result["required_window_count"] == 5
    assert result["capacity_status"] == "insufficient_data"


def test_walk_forward_rejects_positive_but_economically_thin_excess(monkeypatch):
    dates = _dates(90)
    monkeypatch.setattr(wf, "resolve_production_strategy_champion", lambda: {
        "champion_id": 42, "champion_version": "champion-v1", "strategy_controller_version": "controller-v1",
    })

    def replay_runner(**kwargs):
        test_dates = [value for value in dates if kwargs["start_date"] <= value <= kwargs["end_date"]]
        return {
            "production_strategy_version": "champion-v1",
            "metrics": {"total_return": .0101, "benchmark_return": .01, "excess_return": .0001, "max_drawdown": -.01},
            "summary": {"total_fees": 1, "trading_days": len(test_dates), "decision_count": 1},
            "equity_series": [{"date": value, "total_value": 100_000 + index} for index, value in enumerate(test_dates)],
            "data_quality": {"point_in_time": {"passed": True}, "benchmark": {"passed": True}},
        }

    result = wf.run_purged_walk_forward_validation(
        config=wf.WalkForwardConfig(train_sessions=20, validation_sessions=5, test_sessions=8, purge_sessions=2, embargo_sessions=1, step_sessions=8, minimum_windows=5),
        data_source=FakeDates(dates), replay_runner=replay_runner,
        state_loader=lambda values: {value: ("S0", "S1", "S2", "S3")[index % 4] for index, value in enumerate(values)},
    )
    assert result["summary"]["aggregate_excess_return"] > 0
    assert result["summary"]["annualized_relative_excess_return"] < .03
    assert result["promotion_gates"]["annualized_relative_excess_at_least_3pct"] is False
    assert result["status"] == "failed"


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


def test_walk_forward_forwards_controller_candidate_without_rebinding_champion(monkeypatch):
    dates = _dates(90)
    monkeypatch.setattr(wf, "resolve_production_strategy_champion", lambda: {
        "champion_id": 42, "champion_version": "champion-v1",
        "strategy_controller_version_id": 31,
        "strategy_controller_version": "controller-v1", "strategy_id": 1,
    })
    monkeypatch.setattr(wf, "get_model_version", lambda version_id: {
        "id": version_id,
        "model_key": "strategy_controller",
        "version": "controller-candidate-v55",
    })
    seen: list[int | None] = []

    def replay_runner(**kwargs):
        seen.append(kwargs.get("strategy_controller_version_id"))
        test_dates = [value for value in dates if kwargs["start_date"] <= value <= kwargs["end_date"]]
        return {
            "production_strategy_version": "champion-v1", "metrics": {"total_return": .01, "benchmark_return": 0, "excess_return": .01, "max_drawdown": -.01},
            "summary": {"total_fees": 1, "trading_days": len(test_dates), "decision_count": 1},
            "equity_series": [{"date": value, "total_value": 100_000 + index} for index, value in enumerate(test_dates)],
            "data_quality": {"point_in_time": {"passed": True}, "benchmark": {"passed": True}},
        }

    result = wf.run_purged_walk_forward_validation(
        config=wf.WalkForwardConfig(train_sessions=20, validation_sessions=5, test_sessions=8, purge_sessions=2, embargo_sessions=1, step_sessions=8, minimum_windows=2),
        max_windows=2, data_source=FakeDates(dates), replay_runner=replay_runner,
        state_loader=lambda values: {}, strategy_controller_version_id=55,
    )
    assert seen == [55, 55]
    assert result["production_strategy_version"] == "champion-v1"
    assert result["strategy_controller_version_id"] == 55
    assert result["strategy_controller_version"] == "controller-candidate-v55"


def test_walk_forward_forwards_complete_frozen_research_package(monkeypatch):
    dates = _dates(90)
    monkeypatch.setattr(wf, "resolve_production_strategy_champion", lambda: {
        "champion_id": 42, "champion_version": "champion-v1",
        "strategy_controller_version_id": 31,
        "strategy_controller_version": "controller-v1", "strategy_id": 1,
        "factor_bindings": {}, "model_bindings": {}, "market_state_binding": {},
    })
    monkeypatch.setattr(wf, "get_model_version", lambda version_id: {
        "id": version_id,
        "model_key": "strategy_controller",
        "version": "controller-candidate-v55",
    })
    seen: list[dict] = []

    def replay_runner(**kwargs):
        seen.append(kwargs)
        test_dates = [value for value in dates if kwargs["start_date"] <= value <= kwargs["end_date"]]
        return {
            "production_strategy_version": "champion-v1",
            "metrics": {
                "total_return": .01, "benchmark_return": 0,
                "excess_return": .01, "max_drawdown": -.01,
            },
            "summary": {
                "total_fees": 1, "trading_days": len(test_dates), "decision_count": 1,
            },
            "equity_series": [
                {"date": value, "total_value": 100_000 + index}
                for index, value in enumerate(test_dates)
            ],
            "data_quality": {"point_in_time": {"passed": True}, "benchmark": {"passed": True}},
        }

    factor_bindings = {"momentum_20": {"definition_id": 1, "version": "v1"}}
    model_bindings = {
        "medium_horizon_lambdarank_v1": {
            "id": 21, "model_key": "medium_horizon_lambdarank_v1",
        },
    }
    market_state_binding = {
        "production_model": "transparent_four_state_v1",
        "change_model": "bocpd_gaussian_v1",
    }
    result = wf.run_purged_walk_forward_validation(
        config=wf.WalkForwardConfig(
            train_sessions=20, validation_sessions=5, test_sessions=8,
            purge_sessions=2, embargo_sessions=1, step_sessions=8, minimum_windows=2,
        ),
        max_windows=2,
        data_source=FakeDates(dates),
        replay_runner=replay_runner,
        state_loader=lambda values: {},
        strategy_id=2,
        strategy_controller_version_id=55,
        factor_bindings=factor_bindings,
        model_bindings=model_bindings,
        market_state_binding=market_state_binding,
        research_package_hash="package-hash-1",
    )

    assert len(seen) == 2
    assert all(item["factor_bindings"] == factor_bindings for item in seen)
    assert all(item["model_bindings"] == model_bindings for item in seen)
    assert all(item["market_state_binding"] == market_state_binding for item in seen)
    assert all(item["research_package_hash"] == "package-hash-1" for item in seen)
    assert result["factor_bindings"] == factor_bindings
    assert result["model_bindings"] == model_bindings
    assert result["market_state_binding"] == market_state_binding


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
            "data_quality": {"point_in_time": {"passed": True}, "benchmark": {"passed": True}},
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
