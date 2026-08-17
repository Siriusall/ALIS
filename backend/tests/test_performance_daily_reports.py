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


def test_multi_day_period_compounds_first_session_and_requires_full_benchmark_coverage(monkeypatch) -> None:
    monkeypatch.setattr(quant_runs, "quant_run_curve", lambda **_: {
        "series": [
            {"date": "2026-06-30", "strategy_equity": 100.0, "strategy_equity_cny": 100000.0,
             "benchmark_equity": 100.0, "drawdown_pct": 0.0},
            {"date": "2026-07-01", "strategy_equity": 110.0, "strategy_equity_cny": 110000.0,
             "benchmark_equity": 105.0, "drawdown_pct": 0.0},
            {"date": "2026-07-02", "strategy_equity": 121.0, "strategy_equity_cny": 121000.0,
             "benchmark_equity": 0.0, "drawdown_pct": 0.0},
        ],
        "summary": {},
    })
    monkeypatch.setattr(quant_runs, "list_operations", lambda **_: [])
    monkeypatch.setattr(quant_runs, "list_reviews", lambda **_: [])
    monkeypatch.setattr(quant_runs, "list_position_state_history", lambda **_: [])
    monkeypatch.setattr(quant_runs, "list_decision_snapshots", lambda **_: [])

    result = quant_runs.build_quant_run_performance(task_id=None)
    july = next(row for row in result["monthly"] if row["period"] == "2026-07")

    assert july["return_pct"] == 21.0
    assert july["absolute_return"] == 21000.0
    assert july["benchmark_return_pct"] is None
    assert july["benchmark_coverage"]["complete"] is False
    assert july["benchmark_coverage"]["return_points"] == 1


def test_account_curve_uses_raw_close_not_adjusted_research_bar(monkeypatch) -> None:
    class DataSource:
        def load_snapshot(self, _date):
            return []

        def get_price_on_date(self, _symbol, _date, field="close"):
            return 3000.0 if field == "close" else None

    monkeypatch.setattr(quant_runs, "list_operations", lambda **_kwargs: [{
        "symbol": "000001", "side": "BUY", "quantity": 100, "price": 5.0,
        "at": "2026-08-03T09:30:00", "commission": 0, "transfer_fee": 0, "stamp_tax": 0,
    }])
    monkeypatch.setattr(quant_runs, "list_consolidated_trade_dates", lambda: ["2026-08-03", "2026-08-04"])
    monkeypatch.setattr(quant_runs, "BacktestDataSource", DataSource)
    monkeypatch.setattr(quant_runs, "get_account_baseline", lambda: 200000.0)
    monkeypatch.setattr(quant_runs, "_load_index_close_map", lambda *_args: {"2026-08-03": 3000.0, "2026-08-04": 3000.0})
    monkeypatch.setattr(
        quant_runs,
        "_raw_position_close_maps",
        lambda *_args: ({("000001", "20260803"): 5.0, ("000001", "20260804"): 6.0}, {}),
    )

    curve = quant_runs.quant_run_curve(limit=30, benchmark="000300", task_id=None)

    assert curve["series"][0]["strategy_equity_cny"] == 200000.0
    assert curve["series"][1]["strategy_equity_cny"] == 200100.0
    assert curve["series"][1]["day_return_pct"] == 0.05


def test_account_curve_blocks_instead_of_carrying_a_prior_close(monkeypatch) -> None:
    class DataSource:
        def load_snapshot(self, _date):
            return []

        def get_price_on_date(self, _symbol, _date, field="close"):
            return 3000.0 if field == "close" else None

    monkeypatch.setattr(quant_runs, "list_operations", lambda **_kwargs: [{
        "symbol": "000001", "side": "BUY", "quantity": 100, "price": 5.0,
        "at": "2026-08-03T09:30:00", "commission": 0, "transfer_fee": 0, "stamp_tax": 0,
    }])
    monkeypatch.setattr(quant_runs, "list_consolidated_trade_dates", lambda: ["2026-08-03", "2026-08-04"])
    monkeypatch.setattr(quant_runs, "BacktestDataSource", DataSource)
    monkeypatch.setattr(quant_runs, "get_account_baseline", lambda: 200000.0)
    monkeypatch.setattr(quant_runs, "_load_index_close_map", lambda *_args: {"2026-08-03": 3000.0, "2026-08-04": 3000.0})
    monkeypatch.setattr(
        quant_runs,
        "_raw_position_close_maps",
        lambda *_args: ({("000001", "20260803"): 5.0}, {}),
    )

    curve = quant_runs.quant_run_curve(limit=30, benchmark="000300", task_id=None)

    assert curve["ok"] is False
    assert curve["status"] == "blocked"
    assert curve["reason"] == "exact_account_close_missing"
    assert curve["series"] == []
    assert curve["summary"]["missing_close_by_date"] == {"2026-08-04": ["000001"]}


def test_performance_does_not_persist_a_report_when_curve_is_blocked(monkeypatch) -> None:
    monkeypatch.setattr(quant_runs, "quant_run_curve", lambda **_kwargs: {
        "ok": False,
        "status": "blocked",
        "reason": "exact_account_close_missing",
        "series": [],
        "summary": {"missing_close_dates": ["2026-08-04"]},
    })
    monkeypatch.setattr(
        quant_runs,
        "save_performance_reports",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("blocked curve must not persist reports")),
    )

    result = quant_runs.build_quant_run_performance(persist_reports=True)

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["daily"] == []
    assert result["persisted_reports"] is None
