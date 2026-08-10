from __future__ import annotations

from contextlib import contextmanager

from app.data_platform.services import data_center
from app.services import announcement_label_benchmark as service


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


def test_manual_benchmark_reports_confusion_and_field_accuracy(monkeypatch) -> None:
    rows = [
        {
            "source_code": "cninfo",
            "announcement_id": "1",
            "is_event": 1,
            "event_type": "dividend",
            "impact_direction": "positive",
            "severity": "low",
            "raw_id": 1,
            "predicted_event_type": "dividend",
            "predicted_direction": "positive",
            "predicted_severity": "low",
        },
        {
            "source_code": "cninfo",
            "announcement_id": "2",
            "is_event": 1,
            "event_type": "repurchase",
            "impact_direction": "positive",
            "severity": "medium",
            "raw_id": 2,
            "predicted_event_type": None,
            "predicted_direction": None,
            "predicted_severity": None,
        },
        {
            "source_code": "cninfo",
            "announcement_id": "3",
            "is_event": 0,
            "event_type": "",
            "impact_direction": "neutral",
            "severity": "medium",
            "raw_id": 3,
            "predicted_event_type": "regulatory_risk",
            "predicted_direction": "negative",
            "predicted_severity": "high",
        },
        {
            "source_code": "cninfo",
            "announcement_id": "4",
            "is_event": 0,
            "event_type": "",
            "impact_direction": "neutral",
            "severity": "medium",
            "raw_id": 4,
            "predicted_event_type": None,
            "predicted_direction": None,
            "predicted_severity": None,
        },
    ]

    @contextmanager
    def fake_conn():
        class Connection:
            def execute(self, *_args, **_kwargs):
                return _Cursor(rows)

        yield Connection()

    monkeypatch.setattr(service, "_conn", fake_conn)
    result = service.announcement_label_benchmark()

    assert result["confusion"] == {
        "true_positive": 1,
        "false_positive": 1,
        "false_negative": 1,
        "true_negative": 1,
    }
    assert result["event_precision"] == 0.5
    assert result["event_recall"] == 0.5
    assert result["false_positive_rate"] == 0.5
    assert result["false_negative_rate"] == 0.5
    assert result["event_type_accuracy"] == 0.5
    assert result["status"] == "unmeasured_accuracy"


def test_provider_reference_benchmark_keeps_scope_explicit(monkeypatch) -> None:
    rows = [
        {
            "announcement_id": "1",
            "expected_event_type": "dividend",
            "official_category": "category_qyfpxzcs_szsh",
            "raw_id": 1,
            "title": "利润分配公告",
            "predicted_event_type": "dividend",
        },
        {
            "announcement_id": "2",
            "expected_event_type": "earnings_preannouncement",
            "official_category": "category_yjygjxz_szsh",
            "raw_id": 2,
            "title": "业绩预告",
            "predicted_event_type": None,
        },
        {
            "announcement_id": "3",
            "expected_event_type": "earnings",
            "official_category": "category_ndbg_szsh",
            "raw_id": 3,
            "title": "年度报告",
            "predicted_event_type": "earnings",
        },
    ]

    @contextmanager
    def fake_conn():
        class Connection:
            def execute(self, *_args, **_kwargs):
                return _Cursor(rows)

        yield Connection()

    monkeypatch.setattr(service, "_conn", fake_conn)
    result = service.announcement_provider_reference_benchmark()

    assert result["reference_family_accuracy"] == 0.666667
    assert result["per_type"]["earnings_preannouncement"]["recall"] == 0.0
    assert result["full_acceptance_status"] == "blocked_by_manual_gold_and_field_truth"
    assert result["status"] == "insufficient_reference_labels"


def test_provider_reference_sync_uses_official_categories_not_local_rules(monkeypatch) -> None:
    inserted: list[tuple] = []
    calls: list[str] = []

    def fake_fetch(**kwargs):
        category = kwargs["category"]
        calls.append(category)
        return [
            {
                "announcement_id": f"{category}-1",
                "ts_code": "000001.SZ",
                "title": "provider supplied title",
                "publish_time": "2026-07-31T18:00:00",
                "url": "https://example.invalid/a.pdf",
                "raw_text": "",
                "content_hash": category,
                "provider_type_codes": "official-code",
                "provider_type_name": "",
            }
        ]

    @contextmanager
    def fake_conn():
        class Connection:
            def execute(self, _sql, params=()):
                inserted.append(params)
                return _Cursor([])

        yield Connection()

    monkeypatch.setattr(data_center, "ensure_data_center_seeded", lambda: {})
    monkeypatch.setattr("app.data_platform.db.init_db", lambda: None)
    monkeypatch.setattr(data_center, "_fetch_cninfo_announcement_page", fake_fetch)
    monkeypatch.setattr(data_center, "_upsert_announcements", lambda rows: (len(rows), 0))
    monkeypatch.setattr(service, "_conn", fake_conn)
    monkeypatch.setattr(service.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        service,
        "announcement_provider_reference_benchmark",
        lambda **_kwargs: {"status": "insufficient_reference_labels"},
    )

    result = service.sync_cninfo_category_reference_labels(
        start_date="2025-01-01",
        end_date="2026-07-31",
        target_per_type=50,
    )

    assert set(calls) == {
        "category_yjygjxz_szsh",
        "category_qyfpxzcs_szsh",
        "category_ndbg_szsh",
        "category_bndbg_szsh",
        "category_yjdbg_szsh",
        "category_sjdbg_szsh",
    }
    assert result["reference_rows"] == 6
    assert len(inserted) == 6
    assert all(params[2] == service.CNINFO_CATEGORY_REFERENCE_CONTRACT for params in inserted)


def test_manual_review_queue_freezes_strata_without_creating_gold(monkeypatch) -> None:
    candidates = [
        {"source_code": "cninfo", "announcement_id": "1", "predicted_event_type": "dividend"},
        {"source_code": "cninfo", "announcement_id": "2", "predicted_event_type": "repurchase"},
        {"source_code": "cninfo", "announcement_id": "3", "predicted_event_type": ""},
        {"source_code": "cninfo", "announcement_id": "4", "predicted_event_type": ""},
    ]
    writes: list[tuple[str, tuple]] = []

    @contextmanager
    def fake_conn():
        class Connection:
            def execute(self, sql, params=()):
                if "FROM dc_announcement_raw" in sql:
                    return _Cursor(candidates)
                writes.append((sql, params))
                return _Cursor([])

        yield Connection()

    monkeypatch.setattr("app.data_platform.db.init_db", lambda: None)
    monkeypatch.setattr(service, "_conn", fake_conn)
    monkeypatch.setattr(
        service,
        "announcement_manual_review_queue_status",
        lambda **_kwargs: {"status": "insufficient_queue"},
    )

    result = service.materialize_announcement_manual_review_queue(target_size=200)

    assert result["selected_this_call"] == 4
    assert result["selection_strata"] == {
        "predicted:dividend": 1,
        "predicted:repurchase": 1,
        "unclassified_candidate": 2,
    }
    assert len(writes) == 4
    assert all("dc_announcement_label_review_queue" in sql for sql, _params in writes)
    assert not any("dc_announcement_label " in sql for sql, _params in writes)


def test_manual_review_queue_status_never_counts_predictions_as_labels(monkeypatch) -> None:
    rows = [
        {"announcement_id": "1", "stratum": "predicted:dividend", "has_manual_label": 0},
        {"announcement_id": "2", "stratum": "unclassified_candidate", "has_manual_label": 1},
    ]

    @contextmanager
    def fake_conn():
        class Connection:
            def execute(self, *_args, **_kwargs):
                return _Cursor(rows)

        yield Connection()

    monkeypatch.setattr(service, "_conn", fake_conn)
    monkeypatch.setattr(service, "announcement_label_benchmark", lambda: {"status": "unmeasured_accuracy"})
    result = service.announcement_manual_review_queue_status(include_items=True)

    assert result["reviewed"] == 1
    assert result["pending"] == 1
    assert result["status"] == "insufficient_queue"
    assert "never_become_gold" in result["policy"]
