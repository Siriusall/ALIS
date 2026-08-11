from app.services.theme_revenue_exposure import _company_exposures, _matching_themes


def test_business_segment_theme_matching_keeps_multiple_supported_themes():
    themes = _matching_themes("人工智能芯片及集成电路产品")
    assert "人工智能" in themes
    assert "半导体" in themes


def test_business_segment_theme_matching_ignores_unrelated_text():
    assert _matching_themes("物业管理服务") == []


def test_company_exposure_is_deterministic_and_uses_latest_period_segments():
    rows = [
        {"id": 2, "period": "20251231", "available_time": "2026-03-01T09:00:00", "payload_json": '{"bz_item":"人工智能芯片","bz_sales":60}'},
        {"id": 3, "period": "20251231", "available_time": "2026-03-01T09:00:00", "payload_json": '{"bz_item":"物业服务","bz_sales":40}'},
    ]

    exposures, fingerprint = _company_exposures(rows)
    same_exposures, same_fingerprint = _company_exposures(rows)

    assert exposures["revenue:人工智能"]["revenue_share"] == 0.6
    assert exposures["revenue:半导体"]["matched_sales"] == 60.0
    assert fingerprint == same_fingerprint
    assert exposures == same_exposures
