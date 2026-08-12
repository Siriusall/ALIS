from datetime import datetime

from app.data_platform.services import backfill


def test_resume_due_accepts_legacy_naive_business_time():
    now = datetime(2026, 8, 12, 15, 30, tzinfo=backfill._BUSINESS_TZ)
    assert backfill._resume_due("2026-08-12T15:29:00", now=now)


def test_resume_due_normalizes_utc_timestamp():
    now = datetime(2026, 8, 12, 15, 30, tzinfo=backfill._BUSINESS_TZ)
    assert backfill._resume_due("2026-08-12T07:29:00+00:00", now=now)


def test_invalid_resume_time_fails_open_for_recovery():
    assert backfill._resume_due("not-a-time")
