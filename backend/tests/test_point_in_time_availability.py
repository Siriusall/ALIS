from app.data_platform.services.data_center import _point_in_time_available_time


def test_financial_availability_uses_actual_disclosure_date():
    result = _point_in_time_available_time(
        "income",
        {"ann_date": "20250401", "f_ann_date": "20250403", "end_date": "20241231"},
        fallback="2026-07-16T10:00:00",
    )
    assert result == "2025-04-03T23:59:59"


def test_daily_market_availability_uses_market_close():
    result = _point_in_time_available_time("daily", {"trade_date": "20250715"}, fallback="2026-07-16T10:00:00")
    assert result == "2025-07-15T15:00:00"


def test_invalid_source_date_keeps_ingestion_fallback():
    result = _point_in_time_available_time("income", {"ann_date": ""}, fallback="2026-07-16T10:00:00")
    assert result == "2026-07-16T10:00:00"
