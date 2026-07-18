from app.api.routes import quant_runs


def test_performance_persists_a_daily_snapshot_with_the_same_metrics(monkeypatch) -> None:
    monkeypatch.setattr(quant_runs, "quant_run_curve", lambda **_: {
        "series": [
            {"date": "2026-07-15", "strategy_equity": 100.0, "strategy_equity_cny": 100000.0,
             "benchmark_equity": 100.0, "drawdown_pct": 0.0},
            {"date": "2026-07-16", "strategy_equity": 102.0, "strategy_equity_cny": 102000.0,
             "benchmark_equity": 101.0, "drawdown_pct": -0.01},
        ],
        "summary": {"source": "test"},
    })
    monkeypatch.setattr(quant_runs, "list_operations", lambda **_: [])
    monkeypatch.setattr(quant_runs, "list_reviews", lambda **_: [])
    monkeypatch.setattr(quant_runs, "list_position_state_history", lambda **_: [])
    monkeypatch.setattr(quant_runs, "list_decision_snapshots", lambda **_: [])
    captured: dict[str, object] = {}

    def save(task_id, reports):
        captured["task_id"] = task_id
        captured["reports"] = reports
        return sum(len(rows) for rows in reports.values())

    monkeypatch.setattr(quant_runs, "save_performance_reports", save)

    result = quant_runs.quant_run_performance(task_id=None)

    assert [row["period"] for row in result["daily"]] == ["2026-07-15", "2026-07-16"]
    assert result["daily"][1]["return_pct"] == 0.0
    assert set(captured["reports"]) == {"daily", "weekly", "monthly", "yearly"}
    assert result["persisted_reports"] == 5
