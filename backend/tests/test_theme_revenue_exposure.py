from app.services.theme_revenue_exposure import _matching_themes


def test_business_segment_theme_matching_keeps_multiple_supported_themes():
    themes = _matching_themes("人工智能芯片及集成电路产品")
    assert "人工智能" in themes
    assert "半导体" in themes


def test_business_segment_theme_matching_ignores_unrelated_text():
    assert _matching_themes("物业管理服务") == []
