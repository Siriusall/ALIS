from datetime import datetime

from app.data_platform.services.market_calendar import target_trade_date


class _Cursor:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return {"value": self.value}


class _Connection:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        include_today = "cal_date <= ?" in sql
        return _Cursor("20260827" if include_today else "20260826")


def test_target_trade_date_switches_at_configured_daily_update_clock(monkeypatch):
    monkeypatch.setenv("ALIS_DAILY_UPDATE_HOUR", "15")
    monkeypatch.setenv("ALIS_DAILY_UPDATE_MINUTE", "1")
    connection = _Connection()

    before = target_trade_date(connection, now=datetime(2026, 8, 27, 15, 0, 59))
    due = target_trade_date(connection, now=datetime(2026, 8, 27, 15, 1, 0))

    assert before == "20260826"
    assert due == "20260827"
    assert "cal_date < ?" in connection.calls[0][0]
    assert "cal_date <= ?" in connection.calls[1][0]


def test_target_trade_date_honors_custom_daily_update_clock(monkeypatch):
    monkeypatch.setenv("ALIS_DAILY_UPDATE_HOUR", "16")
    monkeypatch.setenv("ALIS_DAILY_UPDATE_MINUTE", "5")
    connection = _Connection()

    before = target_trade_date(connection, now=datetime(2026, 8, 27, 16, 4, 59))
    due = target_trade_date(connection, now=datetime(2026, 8, 27, 16, 5, 0))

    assert before == "20260826"
    assert due == "20260827"
