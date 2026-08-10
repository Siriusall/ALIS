from app.services.strategy_statistics import (
    compare_strategy_windows,
    deflated_sharpe_ratio,
    paired_block_bootstrap_compounded_excess_ci,
)


def test_strategy_statistics_reports_deterministic_positive_window_evidence():
    windows = [
        {"data_complete": True, "point_in_time_passed": True, "metrics": {"excess_return": .02, "max_drawdown": -.10, "benchmark_max_drawdown": -.12}}
        for _ in range(5)
    ]
    result = compare_strategy_windows(windows, replications=100)
    assert result["window_count"] == 5
    assert result["statistical_support"] is True
    assert result["bootstrap_mean_excess_ci_95"][0] == .02


def test_strategy_statistics_does_not_claim_support_with_mixed_windows():
    windows = [
        {"data_complete": True, "point_in_time_passed": True, "metrics": {"excess_return": value}}
        for value in (.04, -.03, -.02, .01, -.01)
    ]
    assert compare_strategy_windows(windows)["statistical_support"] is False


def test_deflated_sharpe_penalizes_repeated_strategy_trials():
    windows = _daily_oos_windows()
    for window in windows:
        for index, observation in enumerate(window["paired_daily_returns"]["observations"]):
            observation["strategy_return"] = .01012 if index % 2 == 0 else -.00988
            observation["benchmark_return"] = 0.0
    one_trial = deflated_sharpe_ratio(windows, trial_count=1)
    many_trials = deflated_sharpe_ratio(windows, trial_count=20)
    assert one_trial["trial_count"] == 1
    assert many_trials["trial_count"] == 20
    assert many_trials["expected_max_sharpe_under_selection"] > one_trial["expected_max_sharpe_under_selection"]
    assert many_trials["probability_sharpe_exceeds_selection_bias"] < one_trial["probability_sharpe_exceeds_selection_bias"]


def test_deflated_sharpe_requires_daily_contract_and_five_formal_windows():
    windows = _daily_oos_windows()[:4]
    result = deflated_sharpe_ratio(windows, trial_count=3)
    assert result["probability_sharpe_exceeds_selection_bias"] is None
    assert result["passed"] is False

    missing_daily_contract = deflated_sharpe_ratio(
        [{"metrics": {"excess_return": .02}} for _ in range(5)], trial_count=3,
    )
    assert missing_daily_contract["daily_return_contract_complete"] is False
    assert missing_daily_contract["passed"] is False


def test_deflated_sharpe_rejects_overlapping_oos_trade_dates():
    windows = _daily_oos_windows()
    windows[1]["paired_daily_returns"]["observations"][0]["date"] = windows[0]["paired_daily_returns"]["observations"][0]["date"]
    result = deflated_sharpe_ratio(windows, trial_count=3)
    assert result["unique_trade_dates"] is False
    assert result["passed"] is False


def test_shadow_block_bootstrap_is_paired_and_deterministic():
    observations = [
        {"candidate_return": .01, "champion_return": 0.0}
        for _ in range(20)
    ]
    first = paired_block_bootstrap_compounded_excess_ci(observations, replications=100)
    second = paired_block_bootstrap_compounded_excess_ci(observations, replications=100)
    assert first == second
    assert first["compounded_excess_ci_95"][0] > 0


def _daily_oos_windows() -> list[dict]:
    return [
        {
            "data_complete": True,
            "point_in_time_passed": True,
            "paired_daily_returns": {
                "schema_version": "alis_oos_paired_daily_returns_v1",
                "status": "complete",
                "observations": [
                    {
                        "date": f"202{window_index}-{day // 28 + 1:02d}-{day % 28 + 1:02d}",
                        "strategy_return": .002 if day % 4 else -.001,
                        "benchmark_return": .0005,
                    }
                    for day in range(20)
                ],
            },
        }
        for window_index in range(5)
    ]
