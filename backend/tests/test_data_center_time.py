from datetime import datetime, timezone

from app.data_platform.services import data_center


def test_rate_limit_cooldown_accepts_aware_timestamp():
    now = datetime(2026, 8, 12, 15, 30, tzinfo=timezone.utc)
    assert data_center._rate_limit_cooldown_active(
        "2026-08-12T15:29:00+00:00", now=now
    )


def test_rate_limit_cooldown_accepts_legacy_naive_timestamp():
    now = datetime(2026, 8, 12, 15, 30)
    assert data_center._rate_limit_cooldown_active("2026-08-12T15:29:00", now=now)


def test_invalid_rate_limit_timestamp_is_retryable():
    assert not data_center._rate_limit_cooldown_active("invalid")
