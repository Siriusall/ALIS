from datetime import datetime, timezone

from app.services import continuous_strategy_research as research


def test_expert_age_interprets_legacy_naive_timestamp_as_utc8():
    now = datetime(2026, 8, 12, 7, 30, tzinfo=timezone.utc)
    age = research._age_days_from_timestamp("2026-08-12T15:00:00", now=now)
    assert round(age * 24 * 60) == 30


def test_expert_age_normalizes_aware_timestamp():
    now = datetime(2026, 8, 12, 15, 30, tzinfo=research._BUSINESS_TZ)
    age = research._age_days_from_timestamp("2026-08-12T07:30:00+00:00", now=now)
    assert round(age * 24 * 60) == 0


def test_invalid_expert_timestamp_is_expired_not_an_interface_error():
    assert research._age_days_from_timestamp("bad") > research.EXPERT_MAX_AGE_DAYS
