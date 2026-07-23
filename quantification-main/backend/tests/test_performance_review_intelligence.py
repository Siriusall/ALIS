from app.services.performance_reports import enrich_performance_period


def test_period_review_groups_market_model_cost_and_errors() -> None:
    report = {
        "start_date": "2026-07-01",
        "end_date": "2026-07-02",
        "return_pct": -1.0,
        "benchmark_return_pct": 1.0,
        "turnover_rate": 0.4,
        "traded_amount": 10000.0,
    }
    points = [
        {"date": "2026-07-01", "daily_return_pct": 0.0, "daily_benchmark_return_pct": None},
        {"date": "2026-07-02", "daily_return_pct": -1.0, "daily_benchmark_return_pct": 1.0},
    ]
    operations = [
        {"side": "BUY", "price": 10.0, "quantity": 500, "commission": 5.0, "transfer_fee": 1.0, "stamp_tax": 0.0},
        {"side": "SELL", "price": 10.0, "quantity": 500, "commission": 5.0, "transfer_fee": 1.0, "stamp_tax": 5.0},
    ]
    decisions = [
        {"id": 7, "decision_time": "2026-07-02T15:00:00", "decision_type": "buy", "model_version": "rank-v1", "ts_code": "000001.SZ"}
    ]
    reviews = [
        {"id": 1, "decision_snapshot_id": 7, "review_time": "2026-07-03", "error_type": "模型失效"},
        {"id": 2, "decision_snapshot_id": 7, "review_time": "2026-07-04", "error_type": "数据质量"},
    ]
    states = {
        "20260701": {"state": "S2", "confidence": 0.8, "model_version": "market-v1"},
        "20260702": {"state": "S2", "confidence": 0.6, "model_version": "market-v1"},
    }

    enriched = enrich_performance_period(
        report,
        points=points,
        operations=operations,
        decisions=decisions,
        reviews=reviews,
        market_states=states,
        blocked_events=[{"code": "market_risk_gate"}],
        version_manifest={"manifest_hash": "abc"},
    )

    assert enriched["schema_version"] == "formal_performance_report_v2"
    assert enriched["costs"] == {
        "commission": 10.0, "transfer_fee": 2.0, "stamp_tax": 5.0, "total": 17.0
    }
    assert enriched["market_state_breakdown"]["S2"]["sessions"] == 2
    assert enriched["market_state_breakdown"]["S2"]["return_pct"] == -1.0
    assert enriched["model_version_breakdown"]["rank-v1"]["error_types"] == {"数据质量": 1}
    assert enriched["error_breakdown"]["blocked_signal_codes"] == {"market_risk_gate": 1}
    assert {item["code"] for item in enriched["learning_signals"]} == {
        "negative_excess_return", "high_turnover", "review_errors", "blocked_signals"
    }
