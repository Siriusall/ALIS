from __future__ import annotations

from app.services.point_in_time import (
    filter_available_records,
    is_available_at,
    normalize_explicit_timestamp,
    normalize_timestamp,
    point_in_time_contract,
    signal_available_time,
)


def test_signal_cutoff_is_market_close_not_end_of_day():
    assert signal_available_time("2026-07-22") == "2026-07-22T15:00:00+08:00"
    assert signal_available_time("20260722") == "2026-07-22T15:00:00+08:00"


def test_future_and_missing_records_are_excluded_in_strict_mode():
    rows = [
        {"id": 1, "available_time": "2026-07-22T14:59:59"},
        {"id": 2, "available_time": "2026-07-22T15:00:01"},
        {"id": 3, "available_time": ""},
    ]
    accepted, audit = filter_available_records(rows, cutoff="2026-07-22T15:00:00+08:00", source="announcement")
    assert [item["id"] for item in accepted] == [1]
    assert audit["future_excluded_count"] == 1
    assert audit["missing_available_time_count"] == 1
    assert audit["passed"] is False


def test_timezone_qualified_cutoff_uses_instant_not_lexical_ordering():
    cutoff = signal_available_time("20260722")

    assert is_available_at("2026-07-22T15:00:00+08:00", cutoff) is True
    assert is_available_at("2026-07-22T15:00:01+08:00", cutoff) is False
    assert is_available_at("2026-07-22T07:00:00Z", cutoff) is True


def test_empty_required_source_cannot_pass_point_in_time_audit():
    accepted, audit = filter_available_records([], cutoff=signal_available_time("20260722"), source="daily")

    assert accepted == []
    assert audit["passed"] is False


def test_formal_timestamp_parser_rejects_naive_without_changing_legacy_parser():
    assert normalize_timestamp("2026-07-22T14:59:59") == "2026-07-22T14:59:59+08:00"
    assert normalize_explicit_timestamp("2026-07-22T14:59:59") == ""
    assert normalize_explicit_timestamp("2026-07-22T06:59:59Z") == "2026-07-22T14:59:59+08:00"


def test_formal_availability_filter_can_require_explicit_offsets():
    accepted, audit = filter_available_records(
        [{"id": 1, "observed_at": "2026-07-22T14:59:59"}],
        cutoff=signal_available_time("20260722"),
        source="daily",
        require_explicit_offset=True,
    )
    assert accepted == []
    assert audit["missing_available_time_count"] == 1
    assert audit["passed"] is False


def test_formal_pit_contract_rejects_naive_decision_cutoff():
    contract = point_in_time_contract(
        cutoff="2026-07-22T15:00:00",
        sources=[{"source": "daily", "passed": True}],
    )
    assert contract["passed"] is False
    assert contract["cutoff_valid"] is False
    assert contract["decision_cutoff"] == ""
