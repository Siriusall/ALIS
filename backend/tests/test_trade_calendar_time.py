from datetime import date, datetime

from app.core import trade_calendar


def test_default_trade_date_uses_business_timezone(monkeypatch):
    class FixedDateTime:
        @classmethod
        def now(cls, _tz):
            return datetime(2026, 8, 12, 14, 0, tzinfo=_tz)

    monkeypatch.setattr(trade_calendar, "datetime", FixedDateTime)
    class Cursor:
        def fetchone(self):
            return {"value": "20260811"}

    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def execute(self, *_args, **_kwargs):
            return Cursor()

    monkeypatch.setattr(trade_calendar, "data_conn", lambda: Connection())
    assert trade_calendar.get_last_trade_date(for_data_availability=True) == date(2026, 8, 11)
