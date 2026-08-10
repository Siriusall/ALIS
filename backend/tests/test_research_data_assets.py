from contextlib import contextmanager

from app.services import research_data_assets


def test_research_asset_audit_does_not_full_scan_theme_mapping_history(
    monkeypatch,
):
    queries: list[str] = []
    rows = iter([
        [],
        {"records": 10, "symbols": 2, "latest_available_time": "2026-08-01", "missing_pit": 0},
        {"mappings": 4_800_000},
        {"snapshot_dates": 1_600, "latest_date": "20260731"},
        {"exposures": 1, "companies": 1, "themes": 1, "latest_date": "20260731"},
        {"raw_records": 1, "latest_available_time": "2026-08-01"},
        {"events": 1, "avg_confidence": 0.8},
        {"observations": 2, "documents": 1, "reconciled": 0, "latest_available_time": "2026-08-01"},
    ])

    class Cursor:
        def __init__(self, value):
            self.value = value

        def fetchall(self):
            return self.value

        def fetchone(self):
            return self.value

    @contextmanager
    def fake_conn():
        class Connection:
            def execute(self, sql, _params=()):
                queries.append(" ".join(str(sql).split()).lower())
                return Cursor(next(rows))

        yield Connection()

    monkeypatch.setattr(research_data_assets, "_conn", fake_conn)
    monkeypatch.setattr(
        research_data_assets,
        "announcement_label_benchmark",
        lambda: {"status": "ready"},
    )
    monkeypatch.setattr(
        research_data_assets,
        "announcement_provider_reference_benchmark",
        lambda **_kwargs: {"status": "covered_type_holdout_ready"},
    )
    monkeypatch.setattr(
        research_data_assets,
        "announcement_manual_review_queue_status",
        lambda **_kwargs: {"status": "ready_for_human_review"},
    )

    result = research_data_assets.audit_research_data_assets()

    mapping_queries = [
        query for query in queries if "dc_theme_company_mapping" in query
    ]
    assert mapping_queries == [
        "select coalesce(n_live_tup,0) mappings from pg_stat_user_tables "
        "where relname='dc_theme_company_mapping'"
    ]
    assert result["theme_exposure"]["mappings_count_method"] == (
        "postgres_live_row_estimate"
    )
    assert result["data_available"] is True
    assert result["theme_exposure"]["snapshot_dates"] == 1_600
    assert result["industry_ocr_candidates"]["status"] == "candidate_only"
    assert result["industry_ocr_candidates"]["production_eligible"] is False
