from __future__ import annotations

import json

import pytest

from app.schemas.position import PositionResponse, PositionsListResponse
from app.services import console_snapshots as snapshots


def test_build_preserves_section_failure(monkeypatch):
    monkeypatch.setattr(snapshots, "_readers", lambda _page: {
        "ready": lambda: {"ok": True, "items": [1]},
        "broken": lambda: (_ for _ in ()).throw(RuntimeError("database timeout")),
    })
    monkeypatch.setattr(
        snapshots,
        "_ensure_snapshot_record",
        lambda _page: {
            "page_key": "overview",
            "version": "",
            "status": "warming_up",
            "invalidated_at": None,
            "refresh_generation": 3,
        },
    )
    failed = []
    monkeypatch.setattr(snapshots, "_record_failed_build", lambda *args: failed.append(args) or True)
    saved = []
    monkeypatch.setattr(snapshots, "_save_durable", lambda *args: saved.append(args) or True)

    payload = snapshots.build("overview")

    assert payload["status"] == "error"
    assert payload["sections"]["ready"]["status"] == "ready"
    assert payload["sections"]["broken"]["status"] == "error"
    assert payload["data"]["broken"]["status"] == "unavailable"
    assert payload["data"]["broken"]["error"]["code"] == "snapshot_section_unavailable"
    assert "database timeout" not in json.dumps(payload)
    assert len(failed) == 1
    assert failed[0][1] == 3
    assert failed[0][2]["data"]["broken"]["error"]["code"] == "snapshot_section_unavailable"
    assert saved == []


def test_control_snapshot_readers_do_not_require_api_loopback_redis(monkeypatch):
    from app import main

    seen: dict[str, object] = {}
    monkeypatch.setattr(main, "_health_payload", lambda *, require_redis=True: seen.setdefault("health", require_redis) or {})

    readers = snapshots._readers("decision")
    readers["health"]()

    assert seen == {"health": False}
    projection = readers["tradingReady"]()
    assert projection["schema_version"] == "alis_trading_readiness_projection_v1"
    assert projection["status"] == "waiting"


def test_durable_snapshot_is_returned_stale_while_queued(monkeypatch):
    metadata = {
        "page_key": "overview",
        "version": "old",
        "status": "ready",
        "invalidated_at": "2026-08-21T00:00:00+00:00",
        "refresh_generation": 2,
    }
    durable = {
        "page": "overview",
        "version": "old",
        "status": "ready",
        "stale": False,
        "refreshing": False,
        "refresh_generation": 2,
    }
    monkeypatch.setattr(snapshots, "get_json", lambda _key: None)
    monkeypatch.setattr(snapshots, "_snapshot_metadata", lambda _page: metadata)
    monkeypatch.setattr(snapshots, "_load_durable", lambda _page: durable)
    mirrored = []
    monkeypatch.setattr(snapshots, "set_json", lambda *args, **kwargs: mirrored.append((args, kwargs)) or True)
    payload = snapshots.read("overview")

    assert payload["version"] == "old"
    assert payload["stale"] is True
    assert payload["refreshing"] is True
    assert mirrored == []


def test_missing_snapshot_never_builds_in_browser_read(monkeypatch):
    monkeypatch.setattr(snapshots, "get_json", lambda _key: None)
    monkeypatch.setattr(snapshots, "_snapshot_metadata", lambda _page: None)
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


def test_postgres_snapshot_schema_check_never_attempts_runtime_ddl(monkeypatch):
    calls: list[str] = []

    class Result:
        def fetchone(self):
            return {"table_name": "console_page_snapshots"}

    class Connection:
        _is_postgres = True

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, _params=()):
            calls.append(str(sql))
            return Result()

    monkeypatch.setattr(snapshots, "_schema_ready", False)
    monkeypatch.setattr(snapshots, "_conn", lambda: Connection())

    snapshots._ensure_table()

    assert len(calls) == 1
    assert "to_regclass" in calls[0]
    assert "create table" not in calls[0].lower()
    assert "create index" not in calls[0].lower()


def test_postgres_snapshot_schema_check_fails_closed_when_table_is_missing(monkeypatch):
    class Result:
        def fetchone(self):
            return {"table_name": None}

    class Connection:
        _is_postgres = True

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _sql, _params=()):
            return Result()

    monkeypatch.setattr(snapshots, "_schema_ready", False)
    monkeypatch.setattr(snapshots, "_conn", lambda: Connection())

    with pytest.raises(RuntimeError, match="schema is missing"):
        snapshots._ensure_table()


def test_as_json_serializes_positions_list_response_for_durable_payload():
    response = PositionsListResponse(
        positions=[
            PositionResponse(
                id=7,
                symbol="600000",
                price=12.34,
                quantity=100,
                name="浦发银行",
                current_price=12.5,
                current_value=1250.0,
                profit_loss=16.0,
                profit_loss_pct=1.3,
                strategy_next="下次调仓：每日",
            )
        ],
        data_source_time="2026-08-21T15:00:00+08:00",
    )

    encoded = snapshots._as_json(response)

    assert json.loads(json.dumps(encoded, ensure_ascii=False)) == encoded
    assert encoded["positions"][0]["symbol"] == "600000"
    assert encoded["positions"][0]["current_value"] == 1250.0
    assert encoded["data_source_time"] == "2026-08-21T15:00:00+08:00"


def test_old_generation_save_cannot_overwrite_snapshot_after_invalidation(monkeypatch):
    state = {
        "refresh_generation": 9,
        "payload_json": json.dumps({"version": "newer-snapshot"}),
    }

    class Result:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=()):
            assert "WHERE page_key=? AND refresh_generation=?" in sql
            assert params[-1] == 8
            if params[-1] != state["refresh_generation"]:
                return Result(None)
            state["payload_json"] = params[4]
            return Result({"refresh_generation": params[-1]})

    monkeypatch.setattr(snapshots, "_ensure_table", lambda: None)
    monkeypatch.setattr(snapshots, "_conn", lambda: Connection())
    old_payload = {
        "page": "overview",
        "version": "old-builder-result",
        "generated_at": "2026-08-21T00:00:00+00:00",
        "as_of": "2026-08-21T00:00:00+00:00",
        "status": "ready",
    }

    saved = snapshots._save_durable("overview", old_payload, expected_generation=8)

    assert saved is False
    assert json.loads(state["payload_json"]) == {"version": "newer-snapshot"}


def test_failed_rebuild_retains_last_known_good_durable_payload(monkeypatch):
    state = {
        "version": "last-known-good",
        "status": "ready",
        "payload_json": json.dumps({"version": "last-known-good", "data": {"stable": True}}),
        "last_attempt_generation": -1,
    }
    queries = []

    class Result:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=()):
            queries.append(sql)
            if "last_attempt_generation" in sql:
                assert params[0] == 4
                assert params[-1] == 4
                state["last_attempt_generation"] = params[0]
                return Result({"version": state["version"], "status": state["status"]})
            raise AssertionError("a failed rebuild must not replace a last-known-good snapshot")

    monkeypatch.setattr(snapshots, "_ensure_table", lambda: None)
    monkeypatch.setattr(snapshots, "_conn", lambda: Connection())
    candidate = {
        "sections": {
            "positions": {
                "status": "error",
                "error": {"code": "snapshot_section_unavailable"},
            }
        }
    }

    recorded = snapshots._record_failed_build("overview", 4, candidate)

    assert recorded is True
    assert state["version"] == "last-known-good"
    assert json.loads(state["payload_json"])["data"] == {"stable": True}
    assert state["last_attempt_generation"] == 4
    assert len(queries) == 1


def test_redis_cache_requires_matching_version_generation_and_valid_authority():
    metadata = {
        "version": "overview-v2",
        "status": "ready",
        "invalidated_at": None,
        "refresh_generation": 12,
    }
    cached = {"version": "overview-v2", "refresh_generation": 12}

    assert snapshots._cache_matches_metadata(cached, metadata) is True
    assert snapshots._cache_matches_metadata({**cached, "version": "overview-v1"}, metadata) is False
    assert snapshots._cache_matches_metadata({**cached, "refresh_generation": 11}, metadata) is False
    assert snapshots._cache_matches_metadata(cached, {**metadata, "invalidated_at": "2026-08-21T00:00:00+00:00"}) is False


def test_read_discards_invalidated_redis_snapshot_without_recaching_lkg(monkeypatch):
    metadata = {
        "page_key": "overview",
        "version": "overview-v2",
        "status": "ready",
        "invalidated_at": "2026-08-21T00:00:00+00:00",
        "refresh_generation": 12,
    }
    cached = {"page": "overview", "version": "overview-v2", "refresh_generation": 12}
    durable = {
        "page": "overview",
        "version": "overview-v2",
        "status": "ready",
        "refresh_generation": 12,
        "data": {"stable": True},
    }
    deleted = []
    mirrored = []
    monkeypatch.setattr(snapshots, "_snapshot_metadata", lambda _page: metadata)
    monkeypatch.setattr(snapshots, "get_json", lambda _key: cached)
    monkeypatch.setattr(snapshots, "_load_durable", lambda _page: durable)
    monkeypatch.setattr(snapshots, "redis_delete", lambda key: deleted.append(key) or True)
    monkeypatch.setattr(snapshots, "set_json", lambda *args, **kwargs: mirrored.append((args, kwargs)) or True)

    payload = snapshots.read("overview")

    assert deleted == [snapshots._redis_key("overview")]
    assert payload["version"] == "overview-v2"
    assert payload["stale"] is True
    assert payload["refreshing"] is True
    assert mirrored == []


def test_decision_snapshot_reader_passes_primitive_limit_days(monkeypatch):
    from app.api.routes import decision

    received: list[object] = []

    def fake_decision_summary(*, limit_days: int):
        received.append(limit_days)
        return {"limit_days": limit_days}

    monkeypatch.setattr(decision, "decision_summary", fake_decision_summary)

    result = snapshots._readers("decision")["decision"]()

    assert result == {"limit_days": 30}
    assert received == [30]
    assert type(received[0]) is int


@pytest.mark.parametrize("page_key", ("overview", "operations", "data"))
def test_snapshot_oos_and_sealed_readers_request_compact_audits(monkeypatch, page_key):
    from app.data_platform.routes import data_center

    calls: list[tuple[str, bool]] = []

    def fake_oos(*, include_dates: bool):
        calls.append(("oos", include_dates))
        return {"include_dates": include_dates}

    def fake_sealed(*, include_dates: bool):
        calls.append(("sealed", include_dates))
        return {"include_dates": include_dates}

    monkeypatch.setattr(data_center, "data_center_oos_hard_data", fake_oos)
    monkeypatch.setattr(data_center, "data_center_sealed_strategy_holdout", fake_sealed)

    readers = snapshots._readers(page_key)

    assert readers["oos"]() == {"include_dates": False}
    assert readers["sealed"]() == {"include_dates": False}
    assert calls == [("oos", False), ("sealed", False)]
