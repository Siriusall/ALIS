"""Compatibility accessors backed by the authoritative exchange calendar."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.data_platform.db import _conn as data_conn
from app.data_platform.services.market_calendar import TradeCalendarNotReady, target_trade_date

_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def is_trade_day(d: date) -> bool:
    """Return the exchange-recorded status for one exact calendar day."""
    day = d.strftime("%Y%m%d")
    with data_conn() as c:
        row = c.execute(
            "SELECT is_open FROM dc_trade_calendar WHERE exchange='SSE' AND cal_date=? LIMIT 1",
            (day,),
        ).fetchone()
    if row is None:
        raise TradeCalendarNotReady(today=day, include_today=True)
    return bool(int(dict(row).get("is_open") or 0))


def get_last_trade_date(as_of: date | None = None, for_data_availability: bool = False) -> date:
    """Return a proven SSE session, never a static-holiday approximation."""
    current = datetime.now(_BUSINESS_TZ)
    requested = as_of or current.date()
    with data_conn() as c:
        if for_data_availability:
            evaluation_now = current.replace(
                year=requested.year,
                month=requested.month,
                day=requested.day,
            )
            value = target_trade_date(c, now=evaluation_now)
        else:
            row = c.execute(
                "SELECT MAX(cal_date) value FROM dc_trade_calendar WHERE exchange='SSE' AND is_open=1 AND cal_date<=?",
                (requested.strftime("%Y%m%d"),),
            ).fetchone()
            value = str(dict(row).get("value") or "") if row is not None else ""
            if not value:
                raise TradeCalendarNotReady(today=requested.strftime("%Y%m%d"), include_today=True)
    return date(int(value[:4]), int(value[4:6]), int(value[6:8]))


def get_last_trade_date_str(as_of: date | None = None, for_data_availability: bool = False) -> str:
    """Return the authoritative latest session as ``YYYYMMDD``."""
    return get_last_trade_date(as_of, for_data_availability).strftime("%Y%m%d")
