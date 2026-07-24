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
    windows = [
        {"data_complete": True, "point_in_time_passed": True, "metrics": {"excess_return": value}}
        for value in (.03, .02, .01, -.01, .005)
    ]
    one_trial = deflated_sharpe_ratio(windows, trial_count=1)
    many_trials = deflated_sharpe_ratio(windows, trial_count=20)
    assert one_trial["trial_count"] == 1
    assert many_trials["trial_count"] == 20
    assert many_trials["expected_max_sharpe_under_selection"] > one_trial["expected_max_sharpe_under_selection"]
    assert many_trials["probability_sharpe_exceeds_selection_bias"] < one_trial["probability_sharpe_exceeds_selection_bias"]


def test_deflated_sharpe_requires_at_least_five_formal_windows():
    windows = [{"metrics": {"excess_return": .02}} for _ in range(4)]
    result = deflated_sharpe_ratio(windows, trial_count=3)
    assert result["probability_sharpe_exceeds_selection_bias"] is None
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
