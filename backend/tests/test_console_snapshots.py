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


def test_durable_snapshot_is_returned_stale_while_queued(monkeypatch):
    durable = {"page": "overview", "version": "old", "status": "ready", "stale": False, "refreshing": False}
    monkeypatch.setattr(snapshots, "get_json", lambda _key: None)
    monkeypatch.setattr(snapshots, "_load_durable", lambda _page: durable)
    monkeypatch.setattr(snapshots, "set_json", lambda *_args, **_kwargs: True)
    payload = snapshots.read("overview")

    assert payload["version"] == "old"
    assert payload["stale"] is False
    assert payload["refreshing"] is False


def test_missing_snapshot_never_builds_in_browser_read(monkeypatch):
    monkeypatch.setattr(snapshots, "get_json", lambda _key: None)
    monkeypatch.setattr(snapshots, "_load_durable", lambda _page: None)
    monkeypatch.setattr(snapshots, "build", lambda _page: (_ for _ in ()).throw(AssertionError("must not build")))

    payload = snapshots.read("overview")

    assert payload["status"] == "warming_up"
    assert payload["refreshing"] is True


def test_snapshot_refresh_defaults_match_page_freshness_contract():
    assert snapshots._ttl("overview") == 30
    assert snapshots._ttl("decision") == 30
    assert snapshots._ttl("portfolio") == 30
    assert snapshots._ttl("operations") == 15
    assert snapshots._ttl("data") == 60
    assert snapshots._ttl("models") == 300


def test_busy_data_platform_defers_optional_snapshot_work(monkeypatch):
    class Context:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def execute(self, *_args):
            return self
        def fetchone(self):
            return {"count": 2}

    monkeypatch.setattr(snapshots, "data_platform_conn", lambda: Context())
    assert snapshots._data_platform_is_busy() is True
