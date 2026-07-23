from app.services.strategy_statistics import compare_strategy_windows


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
