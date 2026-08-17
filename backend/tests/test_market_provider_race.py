import time
import json

import pandas as pd

from app.data_platform.services import data_center
from app.data_platform.services import market_data_discovery as discovery
from app.data_platform.services import tencent_market_snapshot as tencent
from app.data_platform.services import market_bulk_execution as bulk_execution


def _quote_line(trade_date="20260810"):
    fields = [""] * 90
    values = {
        0: "1", 1: "浦发银行", 2: "600000", 3: "9.29", 4: "9.21", 5: "9.20",
        30: f"{trade_date}161444", 31: "0.08", 32: "0.87", 33: "9.38", 34: "9.16",
        35: "9.29/625425/581544471", 36: "625425", 38: "0.19", 39: "6.15",
        44: "3094.11", 45: "3094.11", 46: "0.42", 47: "10.13", 48: "8.29", 49: "0.83",
    }
    for index, value in values.items():
        fields[index] = value
    return f'v_sh600000="{"~".join(fields)}";'


def test_tencent_parser_builds_three_provider_neutral_contracts():
    parsed = tencent._parse_quotes(_quote_line(), "20260810")
    quote = parsed["600000.SH"]
    assert quote["close"] == 9.29
    assert quote["amount"] == 581544.471
    assert quote["total_mv"] == 30_941_100.0
    assert quote["up_limit"] == 10.13
    assert tencent._parse_quotes(_quote_line("20260807"), "20260810") == {}


def test_market_bundle_bulk_fetches_only_probe_winners(monkeypatch):
    monkeypatch.setattr(
        discovery,
        "discover_market_core",
        lambda _date, _datasets=None: {
            "ok": True,
            "winners": {api: "tencent_market_snapshot" for api in ("daily", "daily_basic", "stk_limit")},
        },
    )
    monkeypatch.setattr(
        tencent,
        "sync_tencent_market_bundle",
        lambda _date, apis: {"ok": True, "provider": "tencent_market_snapshot", "apis": apis},
    )
    readiness = iter([
        (False, {"snapshot": {"ok": False}, "decision_contract": {
            "ok": False, "missing_apis": ["daily", "daily_basic", "stk_limit"], "incomplete_apis": [],
        }}),
        (True, {"snapshot": {"ok": True}, "decision_contract": {"ok": True}}),
    ])
    monkeypatch.setattr(data_center, "_decision_core_ready", lambda _date: next(readiness))

    result = data_center.sync_market_bundle("20260810")

    assert result["ok"] is True
    assert set(result["winners"].values()) == {"tencent_market_snapshot"}
    assert result["bulk_contract"] == "alis_market_progress_aware_hedged_checkpoint_v3"
    assert result["provider_results"][0]["apis"] == ["daily", "daily_basic", "stk_limit"]


def test_targeted_market_repair_keeps_probe_winner_source(monkeypatch):
    calls = []
    monkeypatch.setattr(
        discovery,
        "discover_first_published",
        lambda api, trade_date: {"ok": True, "winner": "tushare_proxy", "api": api, "trade_date": trade_date},
    )
    monkeypatch.setattr(discovery, "available_bulk_candidates", lambda *_args: [])
    monkeypatch.setattr(discovery, "mark_provider_bulk_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(data_center, "tushare_compatible_source_codes", lambda: ("tushare_proxy",))
    monkeypatch.setattr(data_center, "build_stock_daily_snapshot", lambda _date: {"ok": True})
    monkeypatch.setattr(
        data_center,
        "_safe_sync_raw",
        lambda api, params, **kwargs: calls.append((api, params, kwargs)) or {"ok": True, "rows_written": 5500},
    )

    result = data_center.sync_market_bundle("20260810", apis=["daily_basic"])

    assert result["ok"] is True
    assert result["provider"] == "tushare_proxy"
    assert calls == [("daily_basic", {"trade_date": "20260810"}, {"provider": "tushare_proxy"})]
    assert result["results"][0]["provider"] == "tushare_proxy"


def test_targeted_market_repair_falls_back_only_after_winner_bulk_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(
        discovery,
        "discover_first_published",
        lambda *_args: {"ok": True, "winner": "tushare_proxy"},
    )
    monkeypatch.setattr(discovery, "available_bulk_candidates", lambda *_args: [])
    monkeypatch.setattr(discovery, "mark_provider_bulk_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(data_center, "tushare_compatible_source_codes", lambda: ("tushare_proxy", "tushare"))
    monkeypatch.setattr(data_center, "build_stock_daily_snapshot", lambda _date: {"ok": True})

    def sync(api, params, **kwargs):
        provider = kwargs.get("provider", "tushare")
        calls.append(provider)
        return {"ok": provider == "tushare", "status": "failed" if provider == "tushare_proxy" else "completed"}

    monkeypatch.setattr(data_center, "_safe_sync_raw", sync)
    result = data_center.sync_market_bundle("20260810", apis=["adj_factor"])

    assert result["ok"] is True
    assert calls == ["tushare_proxy", "tushare"]
    assert result["results"][0]["provider_attempts"] == [
        {"provider": "tushare_proxy", "ok": False, "status": "failed"},
        {"provider": "tushare", "ok": True, "status": "completed"},
    ]


def test_index_proxy_winner_uses_structured_provider_not_web_fallback(monkeypatch):
    strict_rows = iter([None, {"source_code": "tushare_proxy"}])
    captured = []

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(data_center, "_conn", lambda: _Conn())
    monkeypatch.setattr(data_center, "_strict_benchmark_bar", lambda *_args: next(strict_rows))
    monkeypatch.setattr(
        discovery,
        "discover_first_published",
        lambda *_args: {"ok": True, "winner": "tushare_proxy"},
    )
    monkeypatch.setattr(
        data_center,
        "_safe_sync_raw",
        lambda api, params, **kwargs: captured.append({"api": api, "params": params, **kwargs})
        or {"ok": True, "rows_read": 1, "rows_written": 1},
    )
    monkeypatch.setattr(data_center, "_sync_monthly_index_weight_if_due", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.data_platform.services.eastmoney_index_daily_fallback.repair_hs300_index_daily_from_eastmoney",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("web fallback must not run")),
    )
    monkeypatch.setattr(discovery, "mark_provider_bulk_result", lambda *_args, **_kwargs: None)

    result = data_center.sync_index_bundle("000300.SH", "20260814", "20260814")

    assert result["ok"] is True
    assert {item["api"] for item in captured} >= {"index_daily"}
    assert any(
        item["api"] == "index_daily" and item["provider"] == "tushare_proxy"
        for item in captured
    )


def test_discovery_returns_first_available_probe_without_waiting_for_slow_source(monkeypatch):
    monkeypatch.setattr(discovery, "_cached_available", lambda *_args: None)
    monkeypatch.setattr(discovery, "_eligible_providers", lambda *_args: ["fast", "slow"])

    def probe(_dataset, provider, _date):
        if provider == "slow":
            time.sleep(0.15)
        return {"provider": provider, "status": "available", "latency_ms": 1 if provider == "fast" else 150}

    monkeypatch.setattr(discovery, "_run_probe", probe)
    started = time.monotonic()
    result = discovery.discover_first_published("stk_limit", "20260810")

    assert result["winner"] == "fast"
    assert time.monotonic() - started < 0.12


def test_three_core_probes_share_one_tencent_quote_request(monkeypatch):
    calls = []
    quote = {
        "trade_date": "20260810", "quote_time": "20260810161444",
        "open": 9.2, "high": 9.38, "low": 9.16, "close": 9.29,
        "vol": 625425.0, "amount": 581544.471,
        "turnover_rate": 0.19, "volume_ratio": 0.83,
        "total_mv": 30941100.0, "circ_mv": 30941100.0,
        "up_limit": 10.13, "down_limit": 8.29,
    }
    discovery._tencent_probe_cache.clear()
    monkeypatch.setattr(
        discovery,
        "_fetch_batch",
        lambda codes, date: calls.append((codes, date)) or {discovery.STOCK_PROBE_CODE: quote},
    )

    assert discovery._tencent_probe("daily", "20260810")["available"] is True
    assert discovery._tencent_probe("daily_basic", "20260810")["available"] is True
    assert discovery._tencent_probe("stk_limit", "20260810")["available"] is True
    assert len(calls) == 1


def test_probe_cadence_is_fast_after_close_then_backs_off():
    from datetime import datetime

    assert discovery._probe_interval_seconds(datetime(2026, 8, 10, 15, 1)) == 60
    assert discovery._probe_interval_seconds(datetime(2026, 8, 10, 15, 30)) == 180
    assert discovery._probe_interval_seconds(datetime(2026, 8, 10, 16, 1)) == 600


def test_hard_probe_failures_use_long_cooldown():
    from datetime import datetime

    now = datetime(2026, 8, 10, 16, 1)
    assert discovery._probe_cooldown_seconds("authentication", now, "adj_factor", "tushare") >= 86400
    assert discovery._probe_cooldown_seconds("permission", now, "adj_factor", "tushare") >= 86400
    assert discovery._probe_cooldown_seconds("upstream_not_ready", now, "adj_factor", "tushare") == 600


def test_old_probe_error_evidence_gets_new_hard_cooldown():
    from datetime import datetime

    item = {
        "status": "probe_failed",
        "probed_at": "2026-08-10T16:00:00+08:00",
        "evidence_json": json.dumps({"error": "Exception: 您的token不对，请确认。"}),
    }

    assert discovery._persisted_hard_failure_cooling(item, datetime(2026, 8, 10, 16, 30)) is True
    assert discovery._persisted_hard_failure_cooling(item, datetime(2026, 8, 12, 17, 0)) is False


def test_all_provider_probe_failures_expose_actionable_category(monkeypatch):
    monkeypatch.setattr(discovery, "_cached_available", lambda *_args: None)
    monkeypatch.setattr(discovery, "_eligible_providers", lambda *_args: ["tushare"])
    monkeypatch.setattr(
        discovery,
        "_run_probe",
        lambda *_args: {
            "provider": "tushare", "status": "probe_failed", "failure_category": "authentication",
        },
    )

    result = discovery.discover_first_published("adj_factor", "20260810")

    assert result["ok"] is False
    assert result["status"] == "provider_failed"
    assert result["failure_category"] == "authentication"


def test_probe_cooldown_exposes_persisted_failure_category(monkeypatch):
    monkeypatch.setattr(discovery, "_eligible_providers", lambda *_args: [])
    monkeypatch.setattr(discovery, "_cooldown_failure_category", lambda *_args: "authentication")

    result = discovery.discover_first_published("adj_factor", "20260810")

    assert result["status"] == "probe_cooldown"
    assert result["failure_category"] == "authentication"


def test_safe_tushare_sync_hard_failures_enter_long_cooldown(monkeypatch):
    monkeypatch.setattr(
        data_center,
        "sync_tushare_api_raw",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("HTTP 401 invalid token")),
    )

    result = data_center._safe_sync_raw("adj_factor", {"trade_date": "20260814"})

    assert result["ok"] is False
    assert result["category"] == "authentication"
    assert result["cooldown"] is True
    assert result["soft_failed"] is True
    assert result["cooldown_seconds"] >= 86400


def test_no_bulk_request_when_no_probe_has_published(monkeypatch):
    monkeypatch.setattr(
        discovery,
        "discover_market_core",
        lambda _date, _datasets=None: {"ok": False, "status": "not_published", "winners": {}},
    )
    monkeypatch.setattr(
        tencent,
        "sync_tencent_market_bundle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("bulk fetch must not run")),
    )
    monkeypatch.setattr(
        data_center,
        "_decision_core_ready",
        lambda _date: (False, {"snapshot": {"ok": False}, "decision_contract": {"ok": False}}),
    )

    result = data_center.sync_market_bundle("20260810")

    assert result["ok"] is False
    assert result["failure_category"] == "upstream_not_published"


def test_slow_primary_starts_qualified_hedge_and_returns_first_complete(monkeypatch):
    state = {"ready": False}
    monkeypatch.setattr(
        discovery, "discover_market_core",
        lambda *_args: {"ok": True, "winners": {"daily": "tencent_market_snapshot"}},
    )
    monkeypatch.setattr(discovery, "hedge_delay_seconds", lambda *_args: .01)
    monkeypatch.setattr(
        discovery, "available_bulk_candidates",
        lambda *_args: [{"provider": "tushare", "performance": {"eta_ms": 20, "reliability": 1.0}}],
    )
    monkeypatch.setattr(discovery, "mark_provider_bulk_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tencent, "sync_tencent_market_bundle", lambda *_args: time.sleep(.08) or {"ok": False})

    def tushare_bulk(*_args, **_kwargs):
        state["ready"] = True
        return {"ok": True, "rows_read": 5500, "status": "published"}

    monkeypatch.setattr(data_center, "_safe_sync_raw", tushare_bulk)
    monkeypatch.setattr(
        data_center, "_decision_core_ready",
        lambda _date: (state["ready"], {"snapshot": {"ok": state["ready"]}, "decision_contract": {
            "ok": state["ready"], "missing_apis": [] if state["ready"] else ["daily"], "incomplete_apis": [],
        }}),
    )

    result = data_center.sync_market_bundle("20260810")

    assert result["ok"] is True
    assert result["hedge_started"][0]["provider"] == "tushare"
    assert result["policy"].startswith("bounded_batches_with_checkpoint_resume")


def test_provider_performance_opens_circuit_after_consecutive_failures():
    with discovery._conn() as conn:
        for offset in range(3):
            conn.execute(
                """INSERT INTO dc_market_provider_performance
                   (dataset,provider,sample_date,probe_latency_ms,bulk_latency_ms,coverage,success,
                    failure_kind,completed_at,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(dataset,provider,sample_date) DO UPDATE SET success=excluded.success""",
                ("test_slice", "broken_source", f"2026080{offset + 1}", 10, 100, 0, 0,
                 "timeout", "2026-08-10T15:01:00+08:00", "{}"),
            )

    performance = discovery.provider_performance("test_slice", "broken_source")

    assert performance["circuit_open"] is True
    assert performance["consecutive_failures"] == 3


def test_daily_basic_reconciles_non_null_fields_and_keeps_field_lineage():
    trade_date = "20990102"
    code = "600000.SH"
    fallback = {
        "ts_code": code, "trade_date": trade_date, "turnover_rate": .2,
        "volume_ratio": 1.1, "pe_ttm": 8.5, "pb": 1.0,
        "total_mv": 1000.0, "circ_mv": 800.0,
        "_field_sources": {field: "tencent_market_snapshot" for field in (
            "turnover_rate", "volume_ratio", "pe_ttm", "pb", "total_mv", "circ_mv"
        )},
    }
    with discovery._conn() as conn:
        conn.execute(
            "DELETE FROM dc_tushare_raw_records WHERE api_name=? AND record_key=?",
            ("daily_basic", f"{code}|{trade_date}"),
        )
        conn.execute(
            "DELETE FROM dc_tushare_raw_record_version WHERE api_name=? AND record_key=?",
            ("daily_basic", f"{code}|{trade_date}"),
        )
        conn.execute(
            """INSERT INTO dc_tushare_raw_records
               (api_name,record_key,ts_code,trade_date,period,ann_date,payload_json,
                source_code,available_time,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(api_name,record_key) DO UPDATE SET payload_json=excluded.payload_json,
                source_code=excluded.source_code""",
            ("daily_basic", f"{code}|{trade_date}", code, trade_date, "", "", json.dumps(fallback),
             "tencent_market_snapshot", "2099-01-02T15:00:00", "2099-01-02T15:01:00"),
        )
    frame = pd.DataFrame([{
        "ts_code": code, "trade_date": trade_date, "turnover_rate": .3,
        "volume_ratio": 1.1, "pe_ttm": None, "pb": 1.0,
        "total_mv": 1001.0, "circ_mv": 800.0,
    }])

    assert data_center._upsert_raw_records("daily_basic", frame) == 1

    with discovery._conn() as conn:
        row = conn.execute(
            "SELECT payload_json,source_code FROM dc_tushare_raw_records WHERE api_name=? AND record_key=?",
            ("daily_basic", f"{code}|{trade_date}"),
        ).fetchone()
        audits = conn.execute(
            """SELECT field_name,mismatch_count FROM dc_market_provider_field_audit
               WHERE dataset='daily_basic' AND trade_date=?""", (trade_date,),
        ).fetchall()
    payload = json.loads(dict(row)["payload_json"])
    assert payload["turnover_rate"] == .3
    assert payload["pe_ttm"] == 8.5
    assert payload["_field_sources"]["turnover_rate"] == "tushare"
    assert payload["_field_sources"]["pe_ttm"] == "tencent_market_snapshot"
    assert dict(row)["source_code"] == "tushare"
    assert {dict(item)["field_name"]: dict(item)["mismatch_count"] for item in audits}["turnover_rate"] == 1


def test_immutable_market_payload_keeps_verified_fallback_fields_and_hash():
    primary = {
        "ts_code": "600000.SH",
        "trade_date": "20990102",
        "turnover_rate": 0.3,
        "pe_ttm": None,
        "total_mv": None,
    }
    fallback = {
        "ts_code": "600000.SH",
        "trade_date": "20990102",
        "turnover_rate": 0.2,
        "pe_ttm": 8.5,
        "total_mv": 1000.0,
        "_field_sources": {
            "turnover_rate": "tencent_market_snapshot",
            "pe_ttm": "tencent_market_snapshot",
            "total_mv": "tencent_market_snapshot",
        },
    }

    merged, lineage = data_center._merge_verified_market_fallback_fields(
        "daily_basic", primary, fallback, "tencent_market_snapshot",
    )
    merged["_field_sources"] = lineage
    merged["_field_reconciliation_contract"] = "non_null_authoritative_field_merge_v1"

    assert merged["turnover_rate"] == 0.3
    assert merged["pe_ttm"] == 8.5
    assert merged["total_mv"] == 1000.0
    assert lineage["turnover_rate"] == "tushare"
    assert lineage["pe_ttm"] == "tencent_market_snapshot"

    metadata = data_center.observation_metadata(
        "daily_basic",
        merged,
        source_code="tushare",
        observed_at="2099-01-02T15:01:00+08:00",
        economic_time="2099-01-02T15:00:00+08:00",
        provenance="tushare:direct_fetch",
    )
    assert metadata["content_hash"] == data_center.payload_content_hash(
        merged,
        source_code="tushare",
        economic_time=metadata["economic_available_time"],
    )


def test_market_field_lineage_retains_compatible_provider_name():
    primary = {"ts_code": "600000.SH", "trade_date": "20990102", "close": 10.1}
    fallback = {
        "ts_code": "600000.SH",
        "trade_date": "20990102",
        "close": 10.0,
        "open": 9.9,
        "_field_sources": {"close": "tencent_market_snapshot", "open": "tencent_market_snapshot"},
    }

    merged, lineage = data_center._merge_verified_market_fallback_fields(
        "daily",
        primary,
        fallback,
        "tencent_market_snapshot",
        primary_source="tushare_proxy",
    )

    assert merged["close"] == 10.1
    assert merged["open"] == 9.9
    assert lineage["close"] == "tushare_proxy"
    assert lineage["open"] == "tencent_market_snapshot"


def test_snapshot_dependency_metadata_restores_api_name_for_strict_gate(monkeypatch):
    observed = "2099-01-02T15:01:00+08:00"

    def versions(_connection, api_name, **_kwargs):
        return [{
            "ts_code": "600000.SH",
            "trade_date": "20990102",
            "source_code": "tushare",
            "economic_available_time": "2099-01-02T15:00:00+08:00",
            "observed_at": observed,
            "first_observed_at": observed,
            "observation_provenance": "tushare:direct_fetch",
            "content_hash": f"{api_name}-hash",
            "created_at": observed,
            "payload_json": json.dumps({"ts_code": "600000.SH", "trade_date": "20990102"}),
        }]

    monkeypatch.setattr(data_center, "load_raw_record_versions", versions)
    metadata = data_center._raw_source_metadata_map(object(), "20990102")
    items = metadata["600000.SH"]

    assert {item["api_name"] for item in items} == {
        "daily", "daily_basic", "adj_factor", "stk_limit", "suspend_d",
    }
    assert data_center._decision_source_observation_ready(items) is True


def test_tencent_batches_cancel_unstarted_work_and_resume_checkpoint(monkeypatch):
    trade_date = "20990103"
    codes = [f"{index:06d}.SH" for index in range(6)]
    with discovery._conn() as conn:
        conn.execute("DELETE FROM dc_market_bulk_batch_checkpoint WHERE trade_date=?", (trade_date,))
        conn.execute("DELETE FROM dc_market_bulk_execution WHERE trade_date=?", (trade_date,))
    calls = []
    monkeypatch.setattr(tencent, "BATCH_SIZE", 2)
    monkeypatch.setattr(tencent, "MAX_WORKERS", 1)
    monkeypatch.setattr(
        tencent, "_fetch_batch",
        lambda batch, _date: calls.append(list(batch)) or {code: {"ts_code": code} for code in batch},
    )
    original_record = tencent.record_batch
    cancelled_once = {"value": False}

    def record_then_cancel(*args, **kwargs):
        result = original_record(*args, **kwargs)
        if not cancelled_once["value"]:
            cancelled_once["value"] = True
            bulk_execution.request_cancel(trade_date, tencent.SOURCE_CODE, tencent.EXECUTION_SCOPE, reason="test")
        return result

    monkeypatch.setattr(tencent, "record_batch", record_then_cancel)
    first, first_meta = tencent.fetch_tencent_market_snapshot(codes, trade_date, return_meta=True)
    assert first_meta["cancelled"] is True
    assert len(calls) == 1
    assert len(first) == 2

    monkeypatch.setattr(tencent, "record_batch", original_record)
    second, second_meta = tencent.fetch_tencent_market_snapshot(codes, trade_date, return_meta=True)
    assert second_meta["cancelled"] is False
    assert second_meta["resumed_batches"] == 1
    assert len(calls) == 3
    assert len(second) == 6


def test_dynamic_hedge_uses_live_remaining_eta(monkeypatch):
    monkeypatch.setattr(
        discovery, "provider_performance",
        lambda _dataset, provider: {"p75_ms": 10_000 if provider == "primary" else 5_000},
    )
    decision = discovery.hedge_decision(
        "daily", "primary", "backup", elapsed_ms=4_000,
        primary_progress={"completed_batches": 10, "total_batches": 80, "eta_ms": 20_000},
    )
    assert decision["start"] is True
    assert decision["reason"] == "candidate_eta_advantage"
