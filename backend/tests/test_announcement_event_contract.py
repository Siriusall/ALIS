from types import SimpleNamespace
from contextlib import contextmanager

from app.data_platform.services import data_center
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
        "2026年半年度业绩预增的自愿性披露公告": "earnings_forecast",
        "2026 First Quarterly Report": "earnings",
        "优先股2026年股息发放实施公告": "dividend",
    }
    for title, expected in cases.items():
        event = classify_announcement_event(title, "2026-07-17T18:00:00")
        assert event is not None
        assert event["event_type"] == expected
        assert event["event_type"] in V1_EVENT_TYPES


def test_announcement_classifier_keeps_opposite_pledge_directions() -> None:
    assert classify_announcement_event("股东股份质押公告")["impact_direction"] == "negative"
    assert classify_announcement_event("关于解除质押股份的公告")["impact_direction"] == "positive"


def test_cninfo_fetch_preserves_provider_taxonomy(monkeypatch) -> None:
    captured = {}

    def fake_post(_url, *, data, headers, timeout):
        captured.update(data)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "announcements": [
                    {
                        "announcementId": "a1",
                        "secCode": "000001",
                        "secName": "平安银行",
                        "announcementTitle": "2026年半年度业绩预告",
                        "announcementTime": 1785501987000,
                        "adjunctUrl": "finalpage/a.pdf",
                        "announcementType": "0101||012111",
                        "announcementTypeName": "业绩预告",
                    }
                ]
            },
        )

    monkeypatch.setattr(data_center.requests, "post", fake_post)
    rows = data_center._fetch_cninfo_announcement_page(
        start_date="2026-07-01",
        end_date="2026-07-31",
        ts_code=None,
        page_num=1,
        page_size=50,
        category="category_yjygjxz_szsh",
    )

    assert captured["category"] == "category_yjygjxz_szsh"
    assert rows[0]["provider_type_codes"] == "0101||012111"
    assert rows[0]["provider_type_name"] == "业绩预告"


def test_full_reclassification_creates_previously_missed_event(monkeypatch) -> None:
    writes: list[tuple[str, tuple]] = []

    class Cursor:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    @contextmanager
    def fake_conn():
        class Connection:
            def execute(self, sql, params=()):
                if "FROM dc_announcement_raw" in sql:
                    return Cursor(
                        [
                            {
                                "announcement_row_id": 7,
                                "ts_code": "000001.SZ",
                                "title": "2026年半年度业绩预增的自愿性披露公告",
                                "publish_time": "2026-07-31T18:00:00",
                                "event_id": None,
                                "event_type": None,
                                "impact_direction": None,
                                "severity": None,
                                "event_date": None,
                                "summary": None,
                            }
                        ]
                    )
                writes.append((sql, params))
                return Cursor([])

        yield Connection()

    monkeypatch.setattr(data_center, "ensure_data_center_seeded", lambda: {})
    monkeypatch.setattr(data_center, "_conn", fake_conn)

    result = data_center.reclassify_announcement_events(limit=10)

    assert result["created"] == 1
    assert result["updated"] == 0
    assert any("INSERT INTO dc_announcement_event" in sql for sql, _params in writes)
