from __future__ import annotations

from app.services.point_in_time import filter_available_records, signal_available_time


def test_signal_cutoff_is_market_close_not_end_of_day():
    assert signal_available_time("2026-07-22") == "2026-07-22T15:00:00"
    assert signal_available_time("20260722") == "2026-07-22T15:00:00"


def test_future_and_missing_records_are_excluded_in_strict_mode():
    rows = [
        {"id": 1, "available_time": "2026-07-22T14:59:59"},
        {"id": 2, "available_time": "2026-07-22T15:00:01"},
        {"id": 3, "available_time": ""},
    ]
    accepted, audit = filter_available_records(rows, cutoff="2026-07-22T15:00:00", source="announcement")
    assert [item["id"] for item in accepted] == [1]
    assert audit["future_excluded_count"] == 1
    assert audit["missing_available_time_count"] == 1
    assert audit["passed"] is False
