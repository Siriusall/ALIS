from app.services.advanced_controls import (
    bayesian_update,
    black_litterman_weights,
    evaluate_slo,
    freeze_evidence,
    multi_period_oos_report,
    trade_attribution,
)


def test_black_litterman_is_capped_and_explainable():
    result = black_litterman_weights({"A": 0.5, "B": 0.5}, {"A": 0.3, "B": -0.1}, {"A": 1, "B": 1}, max_weight=.6)
    assert abs(sum(result["weights"].values()) - 1) < 1e-6
    assert max(result["weights"].values()) <= .6
    assert result["adjustments"]


def test_bayesian_review_and_attribution():
    assert bayesian_update(.5, [{"source": "x", "likelihood_ratio": 2}])["posterior"] > .5
    result = trade_attribution(entry_price=10, exit_price=12, quantity=10, benchmark_return=.1, industry_return=.15, execution_cost=1)
    assert result["gross_pnl"] == 20
    assert "selection_attribution" in result


def test_slo_and_oos_fail_closed():
    assert evaluate_slo([{"status": "ok", "latency_ms": 10, "freshness_seconds": 1}])["status"] == "ready"
    assert multi_period_oos_report([{"period": "2024", "observations": 10, "holdout_lift": 1, "walk_forward_lift": -1}])["status"] == "failed"


def test_evidence_freeze_is_stable():
    first = freeze_evidence({"b": 2, "a": 1})
    second = freeze_evidence({"a": 1, "b": 2})
    assert first["evidence_hash"] == second["evidence_hash"]
