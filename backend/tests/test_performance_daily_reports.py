from app.api.routes import quant_runs
import pytest


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

    read_result = quant_runs.quant_run_performance(task_id=None)
    assert read_result["read_only"] is True
    assert read_result["persisted_reports"] is None
    assert "reports" not in captured

    result = quant_runs.build_quant_run_performance(task_id=None, persist_reports=True)

    assert [row["period"] for row in result["daily"]] == ["2026-07-15", "2026-07-16"]
    assert result["daily"][1]["return_pct"] == 2.0
    assert result["daily"][1]["benchmark_return_pct"] == 1.0
    assert set(captured["reports"]) == {"daily", "weekly", "monthly", "yearly"}
    assert result["persisted_reports"] == 5


def test_durable_performance_refresh_exposes_persistence_failure(monkeypatch) -> None:
    monkeypatch.setattr(quant_runs, "quant_run_curve", lambda **_: {"series": [], "summary": {}})
    monkeypatch.setattr(quant_runs, "list_operations", lambda **_: [])
    monkeypatch.setattr(quant_runs, "list_reviews", lambda **_: [])
    monkeypatch.setattr(quant_runs, "list_position_state_history", lambda **_: [])
    monkeypatch.setattr(quant_runs, "list_decision_snapshots", lambda **_: [])
    monkeypatch.setattr(quant_runs, "save_performance_reports", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db down")))

    with pytest.raises(RuntimeError, match="db down"):
        quant_runs.build_quant_run_performance(persist_reports=True)
