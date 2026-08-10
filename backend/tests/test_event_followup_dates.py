from datetime import date

from app.services.industry_event_engine import _followup_date


def test_followup_uses_event_or_publish_time() -> None:
    value, needs_review = _followup_date("20260701", "", 30)
    assert value == "2026-07-16"
    assert not needs_review

    value, needs_review = _followup_date("", "2026-07-01T15:00:00", 30)
    assert value == "2026-07-16"
    assert not needs_review


def test_followup_never_leaves_a_missing_date() -> None:
    value, needs_review = _followup_date("", "", 45)
    assert value == date.today().isoformat()
    assert needs_review
