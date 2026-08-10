from app.services.factor_platform import _factor_future_evaluations


def test_candidate_factor_governance_excludes_creation_day_and_historical_replays():
    definition = {
        "status": "candidate",
        "created_at": "2026-07-23T15:56:27+08:00",
    }
    rows = [
        {"id": 1, "end_date": "20260722"},
        {"id": 2, "end_date": "20260723"},
        {"id": 3, "end_date": "20260724"},
        {"id": 4, "end_date": "20260724"},
        {"id": 5, "end_date": "20260727"},
    ]

    result = _factor_future_evaluations(definition, rows)

    assert [item["id"] for item in result] == [3, 5]


def test_active_factor_degradation_keeps_recent_historical_cross_sections():
    definition = {
        "status": "active",
        "created_at": "2026-07-23T15:56:27+08:00",
    }
    rows = [
        {"id": 1, "end_date": "20260722"},
        {"id": 2, "end_date": "20260723"},
    ]

    assert _factor_future_evaluations(definition, rows) == rows
