from __future__ import annotations

from datetime import date, datetime, time as dt_time, timedelta
from typing import Any


AFTER_CLOSE_DATA_READY_TIME = dt_time(16, 0, 0)


def _row_value(row: Any, key: str, default: str = "") -> str:
    if row is None:
        return default
    value = dict(row).get(key, default)
    return str(value) if value not in (None, "") else default


def target_trade_date(c: Any, *, now: datetime | None = None) -> str:
    """Return the trade date that daily market data should be judged against.

    Rule:
    - before 16:00 local time: use the latest open day strictly before today;
    - from 16:00 onward: if today is an open day, use today;
    - on non-trading days: use the latest open day not later than today.

    This keeps readiness, data assets, backfill planning and daily update on the
    same market-data availability contract.
    """

    now = now or datetime.now()
    today_s = now.strftime("%Y%m%d")
    include_today = now.time() >= AFTER_CLOSE_DATA_READY_TIME
    op = "<=" if include_today else "<"
    row = c.execute(
        f"SELECT MAX(cal_date) value FROM dc_trade_calendar WHERE is_open=1 AND cal_date {op} ?",
        (today_s,),
    ).fetchone()
    value = _row_value(row, "value")
    if value:
        return value
    # Calendar may be empty during first bootstrap; use a conservative weekday
    # fallback so the system still starts.
    fallback = now.date() if include_today else now.date() - timedelta(days=1)
    while fallback.weekday() >= 5:
        fallback -= timedelta(days=1)
    return fallback.strftime("%Y%m%d")


def target_trade_day(c: Any, *, now: datetime | None = None) -> date:
    value = target_trade_date(c, now=now)
    return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
