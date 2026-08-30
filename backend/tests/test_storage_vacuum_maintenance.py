"""Unit tests for cold archive and dead tuple storage maintenance."""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from app.data_platform.services.cold_archive import (
    _normalise_date,
    vacuum_table_dead_tuples,
)


def test_normalise_date_valid():
    assert _normalise_date("2026-08-28", "start_date") == "20260828"
    assert _normalise_date("20260828", "start_date") == "20260828"


def test_normalise_date_invalid():
    with pytest.raises(ValueError, match="must be YYYYMMDD"):
        _normalise_date("202608", "start_date")


def test_vacuum_table_unauthorized():
    with pytest.raises(ValueError, match="not authorized"):
        vacuum_table_dead_tuples(table_name="forbidden_secrets_table")


def test_vacuum_table_execution(monkeypatch: pytest.MonkeyPatch):
    mock_conn = MagicMock()
    executed_queries = []

    class MockContext:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

        def execute(self, sql):
            executed_queries.append(sql)

    monkeypatch.setattr("app.data_platform.db._conn", lambda: MockContext())

    result = vacuum_table_dead_tuples(table_name="dc_tushare_raw_records", full=False, analyze=True)
    assert result["ok"] is True
    assert result["table"] == "dc_tushare_raw_records"
    assert "VACUUM ANALYZE public.dc_tushare_raw_records" in executed_queries
