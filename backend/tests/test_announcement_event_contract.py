from app.data_platform.services.data_center import classify_announcement_event
from app.services.industry_event_engine import V1_EVENT_TYPES


def test_announcement_classifier_uses_public_v1_event_vocabulary() -> None:
    cases = {
        "关于回购公司股份的进展公告": "repurchase",
        "控股股东增持公司股份计划": "shareholder_increase",
        "股东股份质押公告": "pledge",
        "关于解除质押部分股份的公告": "pledge",
        "关于重大资产重组事项的公告": "ma_restructuring",
        "向特定对象发行股票（定增）预案": "private_placement",
        "收到中国证监会立案告知书": "investigation",
        "2026年半年度业绩预告": "earnings_forecast",
    }
    for title, expected in cases.items():
        event = classify_announcement_event(title, "2026-07-17T18:00:00")
        assert event is not None
        assert event["event_type"] == expected
        assert event["event_type"] in V1_EVENT_TYPES


def test_announcement_classifier_keeps_opposite_pledge_directions() -> None:
    assert classify_announcement_event("股东股份质押公告")["impact_direction"] == "negative"
    assert classify_announcement_event("关于解除质押股份的公告")["impact_direction"] == "positive"
