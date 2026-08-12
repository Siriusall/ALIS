from datetime import date

from app.api.routes import positions


def test_position_hold_period_uses_business_date(monkeypatch):
    class FixedDateTime:
        @classmethod
        def now(cls, _tz):
            from datetime import datetime
            return datetime(2026, 8, 12, 1, 0, tzinfo=_tz)

    monkeypatch.setattr(positions, "datetime", FixedDateTime)
    result = positions._strategy_next_for_position("2026-08-10", 2, "daily")
    assert result == "已到持仓期满，可卖出"
