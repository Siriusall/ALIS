from __future__ import annotations

from contextlib import nullcontext
from app.services import free_research_data as service
from app.services.announcement_label_benchmark import _normalise_label


def test_metric_code_is_stable_and_industry_specific() -> None:
    assert service._metric_code("futures_proxy", "copper", "铜") == service._metric_code("futures_proxy", "copper", "铜")
    assert service._metric_code("futures_proxy", "copper", "铜") != service._metric_code("futures_proxy", "copper", "铝")


def test_materialize_financial_series_keeps_cutoff_availability(monkeypatch) -> None:
    written_defs: list[dict] = []
    written_values: list[dict] = []
    monkeypatch.setattr(service, "_cutoff_dates", lambda _: ["2026-03-31"])
    monkeypatch.setattr(service, "_conn", lambda: nullcontext(object()))
    monkeypatch.setattr(service, "_derived_industry_proxies", lambda _conn, *, as_of_time: {
        "铜": {"observations": {
            "demand_orders_sales": 58.0,
            "inventory_cycle": 55.0,
            "profit_diffusion": 66.0,
            "industry_valuation": 42.0,
        }, "evidence": {"source": "pit"}},
    })
    monkeypatch.setattr(service, "_upsert_industry_metric_definitions", lambda rows: written_defs.extend(rows) or len(rows))
    monkeypatch.setattr(service, "_upsert_industry_metric_values", lambda rows: written_values.extend(rows) or len(rows))

    result = service.materialize_financial_industry_series(max_periods=4)

    assert result["values"] == 4
    assert {row["available_time"] for row in written_values} == {"2026-03-31T23:59:59"}
    assert {row["source_code"] for row in written_defs} == {service.FINANCIAL_SOURCE}
    assert {row["metric_name"] for row in written_defs} == {
        "行业需求/销量（点时营收扩散代理）",
        "行业库存周期（点时库存营收比代理）",
        "行业盈利扩散（点时财务聚合）",
        "行业估值（点时财务聚合）",
    }


def test_futures_proxy_backfill_is_available_when_materialised(monkeypatch) -> None:
    written_values: list[dict] = []

    monkeypatch.setattr(service, "_ensure_futures_proxy_cache", lambda: None)
    monkeypatch.setattr(service, "_discover_futures_mapping", lambda *_args: (
        {"20260721": {"mapped_contract": "CU2608.SHF", "provider": "source_a", "available_time": "2026-07-23T10:00:00"}},
        {"status": "available", "winner": "source_a"},
    ))
    monkeypatch.setattr(service, "_discover_futures_prices", lambda *_args: (
        {"20260721": {"price": 80000, "provider": "source_b", "available_time": "2026-07-23T10:00:00"}},
        {"status": "available", "winner": "source_b"},
    ))
    monkeypatch.setattr(service, "_now", lambda: "2026-07-23T10:00:00")
    monkeypatch.setattr(service, "_upsert_industry_metric_definitions", lambda rows: len(rows))
    monkeypatch.setattr(service, "_upsert_industry_metric_values", lambda rows: written_values.extend(rows) or len(rows))
    spec = service.FuturesProxySpec("copper", "CU.SHF", "沪铜连续期货价格代理", ("铜",))

    result = service.materialize_futures_price_proxies(start_date="20260701", end_date="20260722", specs=(spec,))

    assert result["values"] == 1
    assert written_values[0]["value"] == 80000.0
    assert written_values[0]["available_time"] == "2026-07-23T10:00:00"


def test_exact_futures_mapping_cache_skips_network_probe(monkeypatch) -> None:
    monkeypatch.setattr(service, "_cached_mappings", lambda *_args: {
        "20260721": {"mapped_contract": "CU2608.SHF", "provider": "cached_source"},
    })
    monkeypatch.setattr(
        "app.data_platform.services.research_source_discovery.race_tushare_sources",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("exact cache must win before network")),
    )

    mappings, evidence = service._discover_futures_mapping("CU.SHF", "20260721", "20260721")

    assert mappings["20260721"]["mapped_contract"] == "CU2608.SHF"
    assert evidence["status"] == "cache_hit"
    assert evidence["network_requests"] == 0


def test_missing_futures_group_is_not_hidden_by_other_successes(monkeypatch) -> None:
    monkeypatch.setattr(service, "_ensure_futures_proxy_cache", lambda: None)
    monkeypatch.setattr(service, "_discover_futures_mapping", lambda root, *_args: (
        {} if root == "SC.INE" else {"20260721": {"mapped_contract": "CU2608.SHF", "provider": "source_a"}},
        {"status": "not_published" if root == "SC.INE" else "available"},
    ))
    monkeypatch.setattr(service, "_discover_futures_prices", lambda *_args: (
        {"20260721": {"price": 80000, "provider": "source_b", "available_time": "2026-07-23T10:00:00"}},
        {"status": "available"},
    ))
    monkeypatch.setattr(service, "_upsert_industry_metric_definitions", lambda rows: len(rows))
    monkeypatch.setattr(service, "_upsert_industry_metric_values", lambda rows: len(rows))
    specs = (
        service.FuturesProxySpec("copper", "CU.SHF", "铜", ("铜",)),
        service.FuturesProxySpec("crude_oil", "SC.INE", "原油", ("石油开采",)),
    )

    result = service.materialize_futures_price_proxies(start_date="20260721", end_date="20260721", specs=specs)

    assert result["status"] == "degraded"
    assert result["missing_sources"] == ["crude_oil"]


def test_cn_ppi_price_proxy_preserves_raw_available_time(monkeypatch) -> None:
    written_values: list[dict] = []

    class Cursor:
        def fetchall(self):
            return [{"payload_json": '{"month":"202606","ppi_mp_qm_yoy":16.5}', "available_time": "2026-07-23T10:00:00"}]

    class Conn:
        def execute(self, *_args):
            return Cursor()

    monkeypatch.setattr(service, "_conn", lambda: nullcontext(Conn()))
    monkeypatch.setattr(service, "_upsert_industry_metric_definitions", lambda rows: len(rows))
    monkeypatch.setattr(service, "_upsert_industry_metric_values", lambda rows: written_values.extend(rows) or len(rows))

    result = service.materialize_cn_ppi_price_proxies(specs=(("mining", "ppi_mp_qm_yoy", ("煤炭开采",)),))

    assert result["values"] == 1
    assert written_values[0]["value"] == 16.5
    assert written_values[0]["available_time"] == "2026-07-23T10:00:00"


def test_announcement_label_requires_type_for_event() -> None:
    try:
        _normalise_label({"announcement_id": "abc", "is_event": "true"})
    except ValueError as exc:
        assert "event_type" in str(exc)
    else:
        raise AssertionError("event labels need a reviewed type")


def test_announcement_non_event_label_has_empty_type() -> None:
    label = _normalise_label({"announcement_id": "abc", "is_event": "false", "event_type": "earnings"})
    assert label["is_event"] == 0
    assert label["event_type"] == ""
