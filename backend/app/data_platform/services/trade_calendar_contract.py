"""Audited ownership contract for historical exchange calendars."""

from __future__ import annotations

from typing import Any, Iterable

from app.data_platform.db import _conn


TRADE_CALENDAR_CONTRACT = "alis_trade_calendar_range_v1"


def _normalized_dates(values: Iterable[str] | None) -> list[str]:
    return sorted({str(value or "").replace("-", "")[:8] for value in (values or []) if len(str(value or "").replace("-", "")[:8]) == 8})


def audit_trade_calendar_range(
    start_date: str,
    end_date: str,
    *,
    expected_dates: Iterable[str] | None = None,
    exchange: str = "SSE",
) -> dict[str, Any]:
    """Audit an exchange calendar range without modifying strategy data.

    ``expected_dates`` is used by fixed historical contracts such as the
    sealed holdout.  When omitted, every open session currently recorded in
    the requested range is treated as the expected set.
    """

    start = str(start_date or "").replace("-", "")[:8]
    end = str(end_date or "").replace("-", "")[:8]
    expected = _normalized_dates(expected_dates)
    with _conn() as conn:
        rows = conn.execute(
            """SELECT cal_date FROM dc_trade_calendar
               WHERE exchange=? AND is_open=1 AND cal_date BETWEEN ? AND ?
               ORDER BY cal_date""",
            (exchange, start, end),
        ).fetchall()
    present_in_range = [str(dict(row).get("cal_date") or "") for row in rows]
    required = expected or present_in_range
    present_set = set(present_in_range)
    present = [value for value in required if value in present_set]
    missing = [value for value in required if value not in present_set]
    coverage = len(present) / len(required) if required else 1.0
    return {
        "ok": not missing,
        "schema_version": TRADE_CALENDAR_CONTRACT,
        "status": "ready" if not missing else "blocked",
        "exchange": exchange,
        "start_date": start,
        "end_date": end,
        "expected_dates": required,
        "present_dates": present,
        "missing_dates": missing,
        "expected_sessions": len(required),
        "present_sessions": len(present),
        "missing_sessions": len(missing),
        "coverage": round(coverage, 6),
    }


def repair_trade_calendar_range(
    start_date: str,
    end_date: str,
    *,
    expected_dates: Iterable[str] | None = None,
    exchange: str = "SSE",
) -> dict[str, Any]:
    """Delegate calendar repair to the data platform's retained TuShare sync."""

    before = audit_trade_calendar_range(
        start_date,
        end_date,
        expected_dates=expected_dates,
        exchange=exchange,
    )
    if before["status"] == "ready":
        return {
            "ok": True,
            "schema_version": TRADE_CALENDAR_CONTRACT,
            "status": "already_ready",
            "before": before,
            "sync": {"ok": True, "skipped": True, "reason": "calendar_already_complete"},
            "after": before,
        }

    # Local import keeps the low-level calendar helpers independent from the
    # large data-center module while preserving raw ingestion/run evidence.
    from app.data_platform.services.data_center import sync_trade_calendar

    sync = sync_trade_calendar(str(start_date), str(end_date), exchange=exchange)
    after = audit_trade_calendar_range(
        start_date,
        end_date,
        expected_dates=expected_dates,
        exchange=exchange,
    )
    return {
        "ok": after["status"] == "ready",
        "schema_version": TRADE_CALENDAR_CONTRACT,
        "status": "repaired" if after["status"] == "ready" else "repair_partial",
        "before": before,
        "sync": sync,
        "after": after,
        "progress": {
            "before": before["present_sessions"],
            "after": after["present_sessions"],
            "delta": after["present_sessions"] - before["present_sessions"],
        },
    }
