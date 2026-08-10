import time

from app.data_platform.services import data_center
from app.data_platform.services import market_data_discovery as discovery
from app.data_platform.services import tencent_market_snapshot as tencent


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
    assert result["bulk_contract"] == "alis_market_first_publisher_bulk_v1"
    assert result["provider_results"][0]["apis"] == ["daily", "daily_basic", "stk_limit"]


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
