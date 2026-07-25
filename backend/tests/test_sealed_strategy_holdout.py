import sqlite3
from contextlib import contextmanager

import pytest

from app.services import sealed_strategy_holdout as holdout


class _SQLiteWrapper:
    _is_postgres = False
    _is_mysql = False

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def execute(self, sql, params=()):
        return self.connection.execute(sql, params)


def _connection_context(connection: sqlite3.Connection):
    @contextmanager
    def _open():
        yield _SQLiteWrapper(connection)
        connection.commit()

    return _open


def _ready_audit(**_kwargs):
    return {"status": "ready", "eligible_sessions": 126, "required_sessions": 126}


def _reference(*, end: str = "20260731") -> dict:
    return {
        "hard_data_window": {"start": "20200323", "end": end},
        "production_strategy_version_id": 50,
        "strategy_controller_version": "controller-v1",
    }


def _replay(*, total_return: float, benchmark_return: float = 0.0, pit: bool = True) -> dict:
    return {
        "metrics": {"total_return": total_return, "benchmark_return": benchmark_return},
        "summary": {"trade_count": 10},
        "data_quality": {"point_in_time": {"passed": pit}},
        "blocked_events": [],
    }


def test_audit_blocks_when_retained_hard_api_slices_are_missing(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE dc_trade_calendar(exchange TEXT,is_open INTEGER,cal_date TEXT);
        CREATE TABLE dc_stock_daily_snapshot(
            trade_date TEXT,close REAL,volume REAL,turnover REAL,available_time TEXT
        );
        INSERT INTO dc_trade_calendar VALUES('SSE',1,'20190910'),('SSE',1,'20190911');
        INSERT INTO dc_stock_daily_snapshot VALUES('20190910',1,1,1,'2019-09-10T15:00:00');
        INSERT INTO dc_stock_daily_snapshot VALUES('20190911',1,1,1,'2019-09-11T15:00:00');
    """)
    monkeypatch.setattr(holdout, "_data_conn", _connection_context(connection))
    monkeypatch.setattr(holdout, "SEALED_HOLDOUT_SESSIONS", 2)
    monkeypatch.setattr(holdout, "SEALED_HOLDOUT_DATES", ("20190910", "20190911"))
    monkeypatch.setattr(holdout, "MINIMUM_DAILY_ROWS", 1)
    monkeypatch.setattr(holdout, "MINIMUM_MARKET_FIELD_RATIO", 1.0)
    monkeypatch.setattr(holdout, "_date_api_sets", lambda *_args: {api: {"20190910"} for api in holdout.HARD_APIS})
    monkeypatch.setattr(holdout, "audit_trade_calendar_range", lambda *_args, **_kwargs: {
        "present_dates": ["20190910", "20190911"],
        "present_sessions": 2,
        "missing_sessions": 0,
        "missing_dates": [],
    })

    result = holdout.audit_sealed_strategy_holdout(include_dates=True)

    assert result["status"] == "blocked"
    assert result["eligible_sessions"] == 1
    assert result["repair_dates"] == ["20190911"]
    assert result["dates"][1]["reasons"] == ["missing_hard_api_slices"]


def test_repair_is_bounded_and_passes_exact_gap_details(monkeypatch):
    details = [
        {"trade_date": f"201909{day:02d}", "eligible": False, "missing_apis": ["daily"], "reasons": ["missing_hard_api_slices"]}
        for day in range(10, 18)
    ]
    monkeypatch.setattr(holdout, "audit_sealed_strategy_holdout", lambda **_kwargs: {
        "repair_dates": [item["trade_date"] for item in details], "dates": details, "eligible_sessions": 1,
    })
    monkeypatch.setattr(holdout, "repair_trade_calendar_range", lambda *_args, **_kwargs: {"ok": True, "status": "repaired"})
    calls = []
    monkeypatch.setattr(
        holdout,
        "repair_oos_hard_data_batch",
        lambda dates, **kwargs: calls.append((dates, kwargs)) or {"ok": True},
    )

    result = holdout.repair_sealed_strategy_holdout(limit=5)

    expected = [item["trade_date"] for item in details[:5]]
    assert result["dates"] == expected
    assert calls[0][0] == expected
    assert calls[0][1]["date_gaps"][expected[0]]["reasons"] == ["missing_hard_api_slices"]
    assert result["calendar_repair"]["status"] == "repaired"


def test_calendar_gap_is_separate_from_retained_api_gap(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE dc_stock_daily_snapshot(
            trade_date TEXT,close REAL,volume REAL,turnover REAL,available_time TEXT
        );
        INSERT INTO dc_stock_daily_snapshot VALUES('20190910',1,1,1,'2019-09-10T15:00:00');
    """)
    monkeypatch.setattr(holdout, "_data_conn", _connection_context(connection))
    monkeypatch.setattr(holdout, "SEALED_HOLDOUT_SESSIONS", 1)
    monkeypatch.setattr(holdout, "SEALED_HOLDOUT_DATES", ("20190910",))
    monkeypatch.setattr(holdout, "MINIMUM_DAILY_ROWS", 1)
    monkeypatch.setattr(holdout, "MINIMUM_MARKET_FIELD_RATIO", 1.0)
    monkeypatch.setattr(holdout, "_date_api_sets", lambda *_args: {api: {"20190910"} for api in holdout.HARD_APIS})
    monkeypatch.setattr(holdout, "audit_trade_calendar_range", lambda *_args, **_kwargs: {
        "present_dates": [], "present_sessions": 0, "missing_sessions": 1, "missing_dates": ["20190910"],
    })

    result = holdout.audit_sealed_strategy_holdout(include_dates=True)

    assert result["dates"][0]["missing_apis"] == []
    assert result["dates"][0]["reasons"] == ["trade_calendar_missing"]
    assert result["calendar_missing_dates"] == ["20190910"]


def test_holdout_result_is_redacted_idempotent_and_single_consumer(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    monkeypatch.setattr(holdout, "_conn", _connection_context(connection))
    monkeypatch.setattr(holdout, "audit_sealed_strategy_holdout", _ready_audit)
    monkeypatch.setattr(holdout, "get_model_version", lambda model_id: {"config": {"strategy_id": model_id}})
    monkeypatch.setattr(holdout, "resolve_production_strategy_champion", lambda: {"champion_id": 50, "strategy_id": 1})
    calls = []

    def runner(*, model_version_id, **_kwargs):
        calls.append(model_version_id)
        return _replay(total_return=.12, benchmark_return=.05) if model_version_id else _replay(total_return=.08)

    first = holdout.run_sealed_strategy_holdout(57, reference_contract=_reference(), replay_runner=runner)
    retry = holdout.run_sealed_strategy_holdout(57, reference_contract=_reference(), replay_runner=runner)
    other = holdout.run_sealed_strategy_holdout(58, reference_contract=_reference(), replay_runner=runner)

    assert first["status"] == "passed" and first["passed"] is True
    assert set(first) == {"schema_version", "status", "passed", "contract_hash", "policy"}
    assert retry["reused"] is True
    assert other["status"] == "cohort_already_consumed"
    assert calls == [57, None]


def test_holdout_fails_when_candidate_does_not_beat_both_reference_and_benchmark(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    monkeypatch.setattr(holdout, "_conn", _connection_context(connection))
    monkeypatch.setattr(holdout, "audit_sealed_strategy_holdout", _ready_audit)
    monkeypatch.setattr(holdout, "get_model_version", lambda model_id: {"config": {"strategy_id": model_id}})
    monkeypatch.setattr(holdout, "resolve_production_strategy_champion", lambda: {"champion_id": 50, "strategy_id": 1})

    def runner(*, model_version_id, **_kwargs):
        return _replay(total_return=.09, benchmark_return=.10) if model_version_id else _replay(total_return=.08)

    result = holdout.run_sealed_strategy_holdout(57, reference_contract=_reference(), replay_runner=runner)

    assert result["status"] == "failed"
    assert result["passed"] is False
    assert "candidate_metrics" not in result


def test_holdout_resumes_same_candidate_after_crash_in_running_state(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    monkeypatch.setattr(holdout, "_conn", _connection_context(connection))
    monkeypatch.setattr(holdout, "audit_sealed_strategy_holdout", _ready_audit)
    monkeypatch.setattr(holdout, "get_model_version", lambda model_id: {"config": {"strategy_id": model_id}})
    monkeypatch.setattr(holdout, "resolve_production_strategy_champion", lambda: {"champion_id": 50, "strategy_id": 1})

    with pytest.raises(RuntimeError, match="simulated crash"):
        holdout.run_sealed_strategy_holdout(
            57,
            reference_contract=_reference(),
            replay_runner=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("simulated crash")),
        )

    def runner(*, model_version_id, **_kwargs):
        return _replay(total_return=.12, benchmark_return=.05) if model_version_id else _replay(total_return=.08)

    recovered = holdout.run_sealed_strategy_holdout(57, reference_contract=_reference(), replay_runner=runner)
    assert recovered["status"] == "passed"
    row = connection.execute("SELECT status,passed FROM strategy_sealed_holdout_results").fetchone()
    assert dict(row) == {"status": "passed", "passed": 1}
