from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pandas as pd

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

    class Pro:
        def fut_mapping(self, **_kwargs):
            return pd.DataFrame([{"trade_date": "20260721", "mapping_ts_code": "CU2608.SHF"}])

        def fut_daily(self, **_kwargs):
            return pd.DataFrame([{"ts_code": "CU2608.SHF", "trade_date": "20260721", "close": 80100, "settle": 80000}])

    monkeypatch.setattr(service, "get_tushare_client", lambda: SimpleNamespace(pro=Pro()))
    monkeypatch.setattr(service, "_now", lambda: "2026-07-23T10:00:00")
    monkeypatch.setattr(service, "_upsert_industry_metric_definitions", lambda rows: len(rows))
    monkeypatch.setattr(service, "_upsert_industry_metric_values", lambda rows: written_values.extend(rows) or len(rows))
    spec = service.FuturesProxySpec("copper", "CU.SHF", "沪铜连续期货价格代理", ("铜",))

    result = service.materialize_futures_price_proxies(start_date="20260701", end_date="20260722", specs=(spec,))

    assert result["values"] == 1
    assert written_values[0]["value"] == 80000.0
    assert written_values[0]["available_time"] == "2026-07-23T10:00:00"


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
