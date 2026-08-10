from app.data_platform.services import assets


def test_cold_read_returns_persisted_snapshot_and_refreshes_in_background(monkeypatch):
    assets._assets_cache.update({"at": 0.0, "value": None})
    assets._assets_refresh_state.update({"running": False, "last_error": None})
    monkeypatch.setattr(assets, "get_json", lambda *_: None)
    monkeypatch.setattr(
        "app.data_platform.services.asset_watermarks.load_asset_catalog_snapshot",
        lambda: {"ok": True, "items": [{"key": "daily"}], "snapshot": {"stale": True}},
    )
    monkeypatch.setattr(assets, "_start_assets_refresh", lambda: True)

    result = assets.get_data_assets()

    assert result["items"] == [{"key": "daily"}]
    assert result["snapshot"]["stale"] is True
    assert result["refresh"]["started"] is True


def test_force_refresh_keeps_expensive_path_explicit(monkeypatch):
    monkeypatch.setattr(assets, "_compute_data_assets", lambda: {"items": [{"key": "daily"}]})
    monkeypatch.setattr(assets, "_store_assets", lambda payload: {**payload, "stored": True})

    result = assets.get_data_assets(force=True)

    assert result["stored"] is True
