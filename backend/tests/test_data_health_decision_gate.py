from app.services import data_health


def _report(rows: list[int], *, latest_coverage: dict[str, float] | None = None) -> dict:
    return {
        "market_snapshots": {
            "recent_days": [
                {
                    "date": f"202607{index + 1:02d}",
                    "symbol_rows": value,
                    "core_factor_coverage": latest_coverage if index == len(rows) - 1 and latest_coverage is not None else {
                        "close": 0.99, "volume": 0.99, "turnover": 0.99, "float_mv": 0.99, "total_mv": 0.99, "pe_ttm": 0.80, "pb": 0.99,
                    },
                }
                for index, value in enumerate(rows)
            ],
        },
    }


def _readiness(*, ok: bool = True) -> dict:
    return {
        "ok": ok,
        "summary": "数据状态正常" if ok else "目标交易日未完成",
        "latest_decision_ready_trade_date": "20260706" if ok else "20260705",
    }


def test_latest_decision_data_gate_requires_current_coverage_and_adj_factor(monkeypatch):
    monkeypatch.setattr("app.data_platform.services.daily_update.simple_health", lambda: _readiness())
    monkeypatch.setattr(data_health, "build_data_health_report", lambda **_: _report([1000, 1005, 995, 1002, 1001, 1004]))
    monkeypatch.setattr(data_health, "_latest_adj_factor_coverage", lambda _: {"trade_date": "20260706", "rows_read": 1000, "minimum_rows": 500, "ready": True})

    result = data_health.assess_latest_decision_data_health()

    assert result["status"] == "ready"
    assert result["decision_ready"] is True
    assert result["experiment_ready"] is True
    assert result["latest_snapshot"]["row_count_ratio"] > .99


def test_latest_decision_data_gate_pauses_experiments_on_coverage_drift(monkeypatch):
    monkeypatch.setattr("app.data_platform.services.daily_update.simple_health", lambda: _readiness())
    monkeypatch.setattr(data_health, "build_data_health_report", lambda **_: _report([1000, 1010, 990, 1005, 995, 700]))
    monkeypatch.setattr(data_health, "_latest_adj_factor_coverage", lambda _: {"trade_date": "20260706", "rows_read": 1000, "minimum_rows": 500, "ready": True})

    result = data_health.assess_latest_decision_data_health()

    assert result["status"] == "blocked"
    assert result["experiment_ready"] is False
    assert "latest_snapshot_row_count_drift" in {item["code"] for item in result["issues"]}


def test_latest_decision_data_gate_keeps_execution_ready_but_pauses_research_without_adj_factor(monkeypatch):
    monkeypatch.setattr("app.data_platform.services.daily_update.simple_health", lambda: _readiness())
    monkeypatch.setattr(data_health, "build_data_health_report", lambda **_: _report([1000, 1005, 995, 1002, 1001, 1004]))
    monkeypatch.setattr(data_health, "_latest_adj_factor_coverage", lambda _: {"trade_date": "20260706", "rows_read": 0, "minimum_rows": 500, "ready": False})

    result = data_health.assess_latest_decision_data_health()

    assert result["status"] == "degraded"
    assert result["decision_ready"] is True
    assert result["experiment_ready"] is False
    assert result["issues"][0]["code"] == "latest_adj_factor_not_ready"
