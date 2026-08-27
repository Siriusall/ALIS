from __future__ import annotations

import os
from datetime import date, datetime, time as dt_time
from typing import Any


def after_close_data_ready_time() -> dt_time:
    """Use the same configurable clock as daily refresh and T+1 execution."""
    hour = max(0, min(int(os.getenv("ALIS_DAILY_UPDATE_HOUR", "15")), 23))
    minute = max(0, min(int(os.getenv("ALIS_DAILY_UPDATE_MINUTE", "1")), 59))
    return dt_time(hour, minute)


class TradeCalendarNotReady(RuntimeError):
    """The exchange calendar cannot prove the requested target session.

    A weekday is not an exchange session.  Callers must surface this state and
    retry the calendar sync instead of silently selecting a guessed date.
    """

    code = "calendar_not_ready"

    def __init__(self, *, today: str, include_today: bool):
        self.today = today
        self.include_today = bool(include_today)
        super().__init__(
            f"交易所日历未就绪，无法确定目标交易日 today={today} "
            f"include_today={int(include_today)}"
        )


def _row_value(row: Any, key: str, default: str = "") -> str:
    if row is None:
        return default
    value = dict(row).get(key, default)
    return str(value) if value not in (None, "") else default


def target_trade_date(c: Any, *, now: datetime | None = None) -> str:
    """Return the trade date that daily market data should be judged against.

    Rule:
    - before the configured daily-update clock: use the latest open day strictly before today;
    - from that clock onward: if today is an open day, use today;
    - on non-trading days: use the latest open day not later than today.

    This keeps readiness, data assets, backfill planning and daily update on the
    same market-data availability contract.
    """

    now = now or datetime.now()
    today_s = now.strftime("%Y%m%d")
    include_today = now.time() >= after_close_data_ready_time()
    op = "<=" if include_today else "<"
    row = c.execute(
        f"SELECT MAX(cal_date) value FROM dc_trade_calendar WHERE is_open=1 AND cal_date {op} ?",
        (today_s,),
    ).fetchone()
    value = _row_value(row, "value")
    if value:
        return value
    # Do not infer an exchange session from the Gregorian weekday.  During
    # bootstrap, a calendar outage, or a partially published calendar, a
    # guessed date can make a missing market bundle look like a real empty day.
    raise TradeCalendarNotReady(today=today_s, include_today=include_today)


def target_trade_day(c: Any, *, now: datetime | None = None) -> date:
    value = target_trade_date(c, now=now)
    return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
