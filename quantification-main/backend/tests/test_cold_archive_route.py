from app.data_platform.routes import data_center


def test_cold_archive_route_passes_explicit_body_contract(monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(data_center, "archive_raw_records", lambda **kwargs: seen.update(kwargs) or {"ok": True})
    request = data_center.ColdArchiveRequest(api_names=["index_daily"], start_date="20240101", end_date="20240131", max_rows=100)

    assert data_center.data_center_archive_raw(request) == {"ok": True}
    assert seen == {"api_names": ["index_daily"], "start_date": "20240101", "end_date": "20240131", "max_rows": 100}
