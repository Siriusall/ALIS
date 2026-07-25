import sqlite3
from contextlib import contextmanager

from app.data_platform.services import trade_calendar_contract as contract


class _Wrapper:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=()):
        return self.connection.execute(sql, params)


def _connection_context(connection):
    @contextmanager
    def _open():
        yield _Wrapper(connection)
        connection.commit()

    return _open


def test_fixed_calendar_range_reports_only_missing_expected_open_dates(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE dc_trade_calendar(exchange TEXT,is_open INTEGER,cal_date TEXT);
        INSERT INTO dc_trade_calendar VALUES('SSE',1,'20190910');
        INSERT INTO dc_trade_calendar VALUES('SSE',0,'20190911');
    """)
    monkeypatch.setattr(contract, "_conn", _connection_context(connection))

    result = contract.audit_trade_calendar_range(
        "20190910", "20190912", expected_dates=["20190910", "20190911", "20190912"],
    )

    assert result["present_dates"] == ["20190910"]
    assert result["missing_dates"] == ["20190911", "20190912"]
    assert result["coverage"] == 0.333333
