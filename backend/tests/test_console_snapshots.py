from __future__ import annotations

from app.services import console_snapshots as snapshots


def test_build_preserves_section_failure(monkeypatch):
    monkeypatch.setattr(snapshots, "_readers", lambda _page: {
        "ready": lambda: {"ok": True, "items": [1]},
        "broken": lambda: (_ for _ in ()).throw(RuntimeError("database timeout")),
    })
    monkeypatch.setattr(snapshots, "_save_durable", lambda *_: None)
    monkeypatch.setattr(snapshots, "set_json", lambda *_args, **_kwargs: True)

    payload = snapshots.build("overview")

    assert payload["status"] == "error"
    assert payload["sections"]["ready"]["status"] == "ready"
    assert payload["sections"]["broken"]["status"] == "error"
    assert payload["data"]["broken"]["status"] == "unavailable"


def test_durable_snapshot_is_returned_stale_while_rebuilding(monkeypatch):
    durable = {"page": "overview", "version": "old", "status": "ready", "stale": False, "refreshing": False}
    monkeypatch.setattr(snapshots, "get_json", lambda _key: None)
    monkeypatch.setattr(snapshots, "_load_durable", lambda _page: durable)
    monkeypatch.setattr(snapshots, "set_json", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(snapshots, "_refresh_in_background", lambda _page: True)

    payload = snapshots.read("overview")

    assert payload["version"] == "old"
    assert payload["stale"] is True
    assert payload["refreshing"] is True
