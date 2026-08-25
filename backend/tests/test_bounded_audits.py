from __future__ import annotations

from contextlib import contextmanager

from app.services import decision_store, manual_review_queue, performance_reports


class _Cursor:
    def __init__(self, rows=None):
        self.rows = rows or []

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Conn:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.sql = []
        self.params = []

    def execute(self, sql, params=()):
        self.sql.append(str(sql))
        self.params.append(tuple(params) if params else ())
        return _Cursor(self.rows)


def test_market_state_history_uses_requested_date_window(monkeypatch):
    conn = _Conn([{"trade_date": "2026-08-01", "market_state": "risk_on", "state_confidence": 0.8, "model_version": "v1"}])

    @contextmanager
    def fake_conn():
        yield conn

    monkeypatch.setattr(performance_reports, "data_conn", fake_conn)
    result = performance_reports.load_market_state_history(start_date="2026-08-01", end_date="2026-08-05")

    assert "SELECT *" not in conn.sql[0].upper()
    assert "trade_date >= ?" in conn.sql[0]
    assert "trade_date <= ?" in conn.sql[0]
    assert conn.params[0] == ("2026-08-01", "2026-08-05")
    assert result["20260801"]["state"] == "risk_on"


def test_performance_validation_has_explicit_projection_and_bound(monkeypatch):
    conn = _Conn([])

    @contextmanager
    def fake_conn():
        yield conn

    monkeypatch.setattr(performance_reports, "business_conn", fake_conn)
    result = performance_reports.validate_performance_reports()

    assert result["ok"] is False
    sql = conn.sql[0].upper()
    assert "SELECT *" not in sql
    assert "LIMIT 5000" in sql


def test_manual_review_triage_has_explicit_projection_and_bound(monkeypatch):
    conn = _Conn([])

    @contextmanager
    def fake_conn():
        yield conn

    monkeypatch.setattr(manual_review_queue, "_TRIAGE_TABLE_READY", True)
    monkeypatch.setattr(manual_review_queue, "_conn", fake_conn)
    assert manual_review_queue.list_manual_review_triage() == {}

    sql = conn.sql[0].upper()
    assert "SELECT *" not in sql
    assert "ORDER BY UPDATED_AT DESC" in sql
    assert "LIMIT 5000" in sql


def test_pool_members_default_read_is_bounded(monkeypatch):
    conn = _Conn([])

    @contextmanager
    def fake_conn():
        yield conn

    monkeypatch.setattr(decision_store, "_conn", fake_conn)
    assert decision_store.list_pool_members("candidate") == []
    assert "LIMIT ?" in conn.sql[0].upper()
    assert conn.params[0][-1] == 5000
