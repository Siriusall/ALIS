from __future__ import annotations

import json
from threading import Event, Thread

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
    from app.services import application_runtime_projection

    projected = {"status": "ok", "ready": True, "projection_status": "current"}
    monkeypatch.setattr(application_runtime_projection, "read_health", lambda: projected)

    readers = snapshots._readers("decision")
    health = readers["health"]()

    assert health == projected
    projection = readers["tradingReady"]()
    assert projection["schema_version"] == "alis_trading_readiness_projection_v1"
    assert projection["status"] == "waiting"


def test_snapshot_historical_audits_reuse_durable_summary(monkeypatch):
    from app.data_platform.routes import data_center

    monkeypatch.setenv("ALIS_CONSOLE_SNAPSHOT_HISTORICAL_AUDIT_MODE", "persisted")
    monkeypatch.setattr(
        snapshots,
        "_load_durable",
        lambda page_key: {
            "data": {
                "oos": {"ok": True, "status": "ready", "eligible_sessions": 126},
                "sealed": {"ok": True, "status": "ready", "windows": 8},
            }
        } if page_key == "data" else None,
    )
    monkeypatch.setattr(
        data_center,
        "data_center_oos_hard_data",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("raw audit must not run")),
    )
    monkeypatch.setattr(
        data_center,
        "data_center_sealed_strategy_holdout",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("raw audit must not run")),
    )

    readers = snapshots._readers("overview")

    assert readers["oos"]()["eligible_sessions"] == 126
    assert readers["sealed"]()["windows"] == 8


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


def test_manual_refresh_coalesces_an_already_queued_snapshot(monkeypatch):
    calls: list[tuple[str, tuple[object, ...]]] = []
    deleted: list[str] = []

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
            calls.append((str(sql), tuple(params)))
            return Result(None)

    monkeypatch.setattr(snapshots, "_ensure_table", lambda: None)
    monkeypatch.setattr(snapshots, "_conn", lambda: Connection())
    monkeypatch.setattr(snapshots, "redis_delete", lambda key: deleted.append(key) or True)

    queued = snapshots.request_refresh("overview")

    assert queued is False
    assert "WHERE console_page_snapshots.invalidated_at IS NULL" in calls[0][0]
    assert deleted == []


@pytest.mark.parametrize("page_key", ("overview", "data"))
def test_snapshot_watermarks_do_not_trigger_another_refresh(monkeypatch, page_key):
    from app.data_platform.routes import data_center

    received: list[bool] = []
    monkeypatch.setattr(
        data_center,
        "data_center_asset_watermarks",
        lambda *, trigger_refresh_if_stale: received.append(trigger_refresh_if_stale) or {},
    )

    snapshots._readers(page_key)["watermarks"]()

    assert received == [False]


def test_snapshot_refresh_defaults_match_page_freshness_contract():
    assert snapshots._ttl("overview") == 600
    assert snapshots._ttl("decision") == 600
    assert snapshots._ttl("portfolio") == 600
    assert snapshots._ttl("operations") == 600
    assert snapshots._ttl("runtime") == 30
    assert snapshots._ttl("data") == 600
    assert snapshots._ttl("models") == 600
    assert snapshots._ttl("research") == 600
    assert snapshots._ttl("factors") == 600
    assert snapshots._ttl("evidence") == 600


def test_runtime_snapshot_is_prioritized_ahead_of_business_pages(monkeypatch):
    monkeypatch.setattr(snapshots, "_ensure_table", lambda: None)

    class Rows:
        def fetchall(self):
            return []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args, **_kwargs):
            return Rows()

    monkeypatch.setattr(snapshots, "_conn", lambda: Connection())

    assert snapshots._pages_due_for_refresh()[0] == "runtime"


def test_degraded_envelope_metadata_is_not_readable_evidence():
    value = {
        "ok": False,
        "status": "degraded",
        "data_available": False,
        "component": "decision_audit",
        "checked_at": "2026-08-25T16:35:00+08:00",
        "decisions": [],
        "reviews": [],
        "error": {"code": "diagnostic_unavailable"},
    }

    assert snapshots._has_readable_evidence(value) is False
    _, section = snapshots._section("evidence", "decisionAudit", lambda: value)
    assert section["status"] == "degraded"


def test_snapshot_separates_document_and_business_freshness():
    metadata = snapshots._business_metadata(
        {
            "daily": {"status": "ready", "latest_trade_date": "20260911"},
            "research": {"ok": False, "status": "partial", "data_as_of": "20260829"},
        },
        {"daily": {"status": "ready"}, "research": {"status": "ready"}},
    )

    assert metadata["document_status"] == "ready"
    assert metadata["business_status"] == "partial"
    assert metadata["data_as_of"] == "20260911"
    assert "research.status" in metadata["business_issues"]


def test_evidence_snapshot_freezes_first_decision_detail_and_verification(monkeypatch):
    from app.api.routes import research

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        research,
        "research_decisions",
        lambda *, limit, compact: {"ok": True, "items": [{"id": 51}], "compact": compact},
    )
    monkeypatch.setattr(
        research,
        "research_decision_detail",
        lambda decision_id: calls.append(("detail", decision_id)) or {
            "id": decision_id,
            "market_state": {"state": "S1"},
        },
    )
    monkeypatch.setattr(
        research,
        "research_evidence_verify",
        lambda decision_id: calls.append(("verify", decision_id)) or {
            "id": decision_id,
            "verified": True,
            "status": "verified",
        },
    )

    readers = snapshots._readers("evidence")

    assert readers["decisionDetail"]()["market_state"]["state"] == "S1"
    assert readers["evidenceVerification"]()["verified"] is True
    assert calls == [("detail", 51), ("verify", 51)]


def test_research_snapshot_reuses_selector_lists_for_optional_details(monkeypatch):
    from app.api.routes import research

    calls: list[tuple[str, object]] = []
    pools = {"items": [{"ts_code": "600000.SH", "name": "浦发银行"}]}
    industries = {"items": [{"industry_code": "bank", "industry": "银行"}]}
    monkeypatch.setattr(
        research,
        "research_pools",
        lambda: calls.append(("pools", None)) or pools,
    )
    monkeypatch.setattr(
        research,
        "research_industries",
        lambda *, compact: calls.append(("industries", compact)) or industries,
    )
    monkeypatch.setattr(
        research,
        "research_console_stock_evidence",
        lambda code: calls.append(("stock", code)) or {"ts_code": code},
    )
    monkeypatch.setattr(
        research,
        "research_industry_detail",
        lambda code: calls.append(("industry", code)) or {"industry_code": code},
    )

    readers = snapshots._readers("research")

    assert readers["pools"]() is pools
    assert readers["researchStockEvidence"]()["ts_code"] == "600000.SH"
    assert readers["industries"]() is industries
    assert readers["researchIndustryEvidence"]()["industry_code"] == "bank"
    assert calls == [
        ("pools", None),
        ("stock", "600000.SH"),
        ("industries", True),
        ("industry", "bank"),
    ]


def test_optional_research_detail_failure_degrades_only_its_section():
    class Research:
        @staticmethod
        def research_console_stock_evidence(_code):
            raise RuntimeError("detail database timeout")

    value, section = snapshots._section(
        "research",
        "researchStockEvidence",
        lambda: snapshots._research_detail_evidence(
            Research,
            "stock",
            lambda: {"items": [{"ts_code": "600000.SH"}]},
        ),
    )

    assert value["status"] == "degraded"
    assert value["data_available"] is False
    assert value["error"]["code"] == "research_detail_unavailable"
    assert "database timeout" not in json.dumps(value)
    assert section["status"] == "degraded"


def test_optional_research_detail_failure_still_commits_partial_snapshot(monkeypatch):
    monkeypatch.setattr(snapshots, "_readers", lambda _page: {
        "pools": lambda: {"items": [{"ts_code": "600000.SH"}]},
        "researchStockEvidence": lambda: {
            "ok": False,
            "status": "degraded",
            "data_available": False,
            "component": "researchStockEvidence",
            "items": [],
            "error": {"code": "research_detail_unavailable"},
        },
    })
    monkeypatch.setattr(
        snapshots,
        "_ensure_snapshot_record",
        lambda _page: {
            "page_key": "research",
            "version": "research-lkg",
            "status": "ready",
            "invalidated_at": None,
            "refresh_generation": 4,
        },
    )
    failed = []
    saved = []
    monkeypatch.setattr(
        snapshots,
        "_record_failed_build",
        lambda *args: failed.append(args) or True,
    )
    monkeypatch.setattr(
        snapshots,
        "_save_durable",
        lambda *args: saved.append(args) or True,
    )

    payload = snapshots.build("research")

    assert payload["status"] == "degraded"
    assert payload["sections"]["pools"]["status"] == "ready"
    assert payload["sections"]["researchStockEvidence"]["status"] == "degraded"
    assert failed == []
    assert len(saved) == 1


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
            if str(sql).startswith("SELECT version,status,content_hash"):
                return Result({"version": "newer-snapshot", "status": "ready", "content_hash": "different"})
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


def test_snapshot_payload_compression_round_trip():
    payload = {
        "page": "overview",
        "version": "overview-v1",
        "generated_at": "2026-08-29T00:00:00+00:00",
        "status": "ready",
        "sections": {"health": {"status": "ready"}},
        "data": {"items": [{"name": "重复数据"}] * 100},
    }

    serialized, compressed, content_hash, raw_size, compressed_size = snapshots._encode_payload(payload)

    assert snapshots._decode_payload({"payload_gzip": compressed, "payload_json": ""}) == payload
    assert raw_size == len(serialized.encode("utf-8"))
    assert compressed_size < raw_size
    assert len(content_hash) == 64


def test_new_snapshot_rows_store_only_compressed_body(monkeypatch):
    calls: list[tuple[str, tuple[object, ...]]] = []

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
            calls.append((str(sql), tuple(params)))
            if str(sql).startswith("SELECT version,status,content_hash"):
                return Result({"version": "", "status": "warming_up", "content_hash": ""})
            return Result({"refresh_generation": 2})

    monkeypatch.setattr(snapshots, "_ensure_table", lambda: None)
    monkeypatch.setattr(snapshots, "_conn", lambda: Connection())
    payload = {
        "page": "overview", "version": "v2", "generated_at": "now", "as_of": "now",
        "status": "ready", "sections": {}, "data": {"items": ["large"] * 100},
    }

    assert snapshots._save_durable("overview", payload, expected_generation=2) is True
    update_params = calls[1][1]
    assert update_params[4] == "{}"
    assert isinstance(update_params[5], bytes)
    assert len(update_params[5]) < update_params[7]


def test_semantic_hash_ignores_snapshot_wrapper_timestamps():
    first = {
        "page": "overview", "version": "v1", "generated_at": "one", "as_of": "one",
        "status": "ready", "sections": {}, "data": {"value": 1},
    }
    second = {**first, "version": "v2", "generated_at": "two", "as_of": "two"}

    assert snapshots._semantic_payload_hash(first) == snapshots._semantic_payload_hash(second)


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


def test_redis_cache_rejects_same_generation_with_old_build_timestamp():
    metadata = {
        "version": "overview-v2", "generated_at": "2026-08-21T10:00:00+00:00",
        "status": "ready", "invalidated_at": None, "refresh_generation": 12,
    }
    cached = {"version": "overview-v2", "generated_at": "2026-08-21T09:00:00+00:00",
              "refresh_generation": 12}
    assert snapshots._cache_matches_metadata(cached, metadata) is False


def test_redis_cache_rejects_same_generation_with_old_payload_hash():
    current = {"page": "overview", "version": "overview-v2", "status": "ready",
               "sections": {"health": {"status": "ready"}}, "data": {"value": "new"}}
    metadata = {**current, "content_hash": snapshots._semantic_payload_hash(current),
                "invalidated_at": None, "refresh_generation": 12}
    cached = {**current, "data": {"value": "old"}, "refresh_generation": 12}
    assert snapshots._cache_matches_metadata(cached, metadata) is False


def test_expired_snapshot_is_stale_even_with_matching_redis_and_no_invalidation(monkeypatch):
    payload = {"page": "research", "version": "old", "status": "ready",
               "generated_at": "2020-01-01T00:00:00+00:00", "data": {"evidence": "retained"}}
    metadata = {**payload, "page_key": "research", "invalidated_at": None}
    monkeypatch.setattr(snapshots, "_snapshot_metadata", lambda _: metadata)
    monkeypatch.setattr(snapshots, "_load_durable", lambda _: payload)
    monkeypatch.setattr(snapshots, "get_json", lambda _: payload)
    monkeypatch.setattr(snapshots, "redis_delete", lambda _: None)
    monkeypatch.setattr(snapshots, "set_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("expired cache")))
    result = snapshots.read("research")
    assert result["stale"] is True
    assert result["refreshing"] is True
    assert result["data"] == payload["data"]


def test_workload_yield_waits_before_scanning_again(monkeypatch):
    scans = []
    monkeypatch.setattr(snapshots, "_pages_due_for_refresh", lambda: scans.append(1) or ["research"])
    monkeypatch.setattr(snapshots, "_snapshot_build_should_yield", lambda _: True)
    monkeypatch.setattr(snapshots, "build", lambda _: pytest.fail("must yield"))
    class StopAfterWait:
        def is_set(self):
            assert len(scans) <= 1, "busy loop while database is occupied"
            return False
        def wait(self, seconds):
            assert seconds >= 1
            return True
    snapshots.run_snapshot_worker_forever(StopAfterWait())
    assert len(scans) == 1


def test_db_local_snapshot_cannot_be_starved_by_research(monkeypatch):
    monkeypatch.setenv("ALIS_CONSOLE_SNAPSHOT_BOUNDED_RESEARCH_YIELD", "1")
    monkeypatch.setattr(snapshots, "optional_network_work_allowed", lambda: True)
    monkeypatch.setattr(snapshots, "_data_ingestion_active", lambda: False)
    monkeypatch.setattr(snapshots, "_control_work_active", lambda include_research=True: include_research)
    monkeypatch.setattr(snapshots, "_snapshot_metadata", lambda _: {"generated_at": "2020-01-01T00:00:00Z"})
    assert snapshots._snapshot_build_should_yield("research") is False
    monkeypatch.setattr(snapshots, "_snapshot_metadata", lambda _: {"generated_at": snapshots._now()})
    assert snapshots._snapshot_build_should_yield("research") is True
    monkeypatch.setattr(snapshots, "_snapshot_metadata", lambda _: {"generated_at": "2020-01-01T00:00:00Z"})
    monkeypatch.setattr(snapshots, "_control_work_active", lambda **_: True)
    assert snapshots._snapshot_build_should_yield("research") is False
    monkeypatch.setattr(snapshots, "_control_work_active", lambda **_: False)
    monkeypatch.setattr(snapshots, "_data_ingestion_active", lambda: True)
    assert snapshots._snapshot_build_should_yield("research") is True


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
    monkeypatch.setattr(snapshots, "_snapshot_metadata", lambda page: metadata if page == "overview" else None)
    monkeypatch.setattr(snapshots, "get_json", lambda key: cached if key == snapshots._redis_key("overview") else None)
    monkeypatch.setattr(snapshots, "_load_durable", lambda page: durable if page == "overview" else None)
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


def test_oos_api_uses_bounded_progressive_audit_and_preserves_status(monkeypatch):
    from app.data_platform.routes import data_center

    received = []
    monkeypatch.setattr(
        data_center,
        "audit_progressive_oos",
        lambda *, include_dates: received.append(include_dates) or {
            "status": "accumulating",
            "required_sessions": 252,
        },
    )

    result = data_center.data_center_oos_hard_data(include_dates=False)

    assert received == [False]
    assert result["status"] == "accumulating"
    assert result["required_sessions"] == 252


def test_repeated_page_builds_reuse_formal_audit_summaries(monkeypatch):
    from app.data_platform.routes import data_center

    calls: list[str] = []

    def fake_oos(*, include_dates: bool):
        calls.append("oos")
        return {"include_dates": include_dates, "status": "ready"}

    def fake_sealed(*, include_dates: bool):
        calls.append("sealed")
        return {"include_dates": include_dates, "status": "ready"}

    monkeypatch.setattr(data_center, "data_center_oos_hard_data", fake_oos)
    monkeypatch.setattr(data_center, "data_center_sealed_strategy_holdout", fake_sealed)
    monkeypatch.setattr(snapshots, "_diagnostic_cache", {})

    first = snapshots._readers("overview")
    second = snapshots._readers("operations")

    assert first["oos"]() == second["oos"]()
    assert first["sealed"]() == second["sealed"]()
    assert calls == ["oos", "sealed"]


def test_related_page_invalidation_retires_shared_diagnostic_cache(monkeypatch):
    from app.data_platform.routes import data_center

    calls: list[int] = []

    def fake_oos(*, include_dates: bool):
        calls.append(len(calls) + 1)
        return {"include_dates": include_dates, "revision": calls[-1]}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _sql, _params=()):
            return None

    monkeypatch.setattr(data_center, "data_center_oos_hard_data", fake_oos)
    monkeypatch.setattr(snapshots, "_diagnostic_cache", {})
    monkeypatch.setattr(snapshots, "_diagnostic_cache_generation", 0)
    monkeypatch.setattr(snapshots, "_ensure_table", lambda: None)
    monkeypatch.setattr(snapshots, "_conn", lambda: Connection())
    monkeypatch.setattr(snapshots, "redis_delete", lambda _key: True)

    assert snapshots._readers("overview")["oos"]()["revision"] == 1
    assert snapshots._readers("operations")["oos"]()["revision"] == 1

    snapshots.invalidate(["data"])

    assert snapshots._readers("overview")["oos"]()["revision"] == 2
    assert calls == [1, 2]


def test_inflight_diagnostic_cannot_restore_cache_after_invalidation(monkeypatch):
    started = Event()
    release = Event()
    result: list[dict[str, bool]] = []

    def slow_reader():
        started.set()
        assert release.wait(timeout=2)
        return {"old": True}

    monkeypatch.setattr(snapshots, "_diagnostic_cache", {})
    monkeypatch.setattr(snapshots, "_diagnostic_cache_generation", 0)
    worker = Thread(
        target=lambda: result.append(
            snapshots._cached_diagnostic("progressive_oos", slow_reader)
        )
    )
    worker.start()
    assert started.wait(timeout=2)

    snapshots._clear_diagnostic_cache()
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result == [{"old": True}]
    assert snapshots._diagnostic_cache == {}


def test_evidence_snapshot_replaces_fastapi_query_defaults(monkeypatch):
    from app.api.routes import quant_runs, research

    calls = []
    monkeypatch.setattr(
        research,
        "research_decision_audit",
        lambda *, limit, compact: calls.append(("audit", limit, compact)) or {"ok": True},
    )
    monkeypatch.setattr(
        quant_runs,
        "quant_run_simulation_ledger",
        lambda *, execution_id, limit: calls.append(("ledger", execution_id, limit)) or {"ok": True},
    )

    readers = snapshots._readers("evidence")

    assert readers["decisionAudit"]() == {"ok": True}
    assert readers["ledgerDetail"]() == {"ok": True}
    assert calls == [("audit", 100, False), ("ledger", None, 20)]
