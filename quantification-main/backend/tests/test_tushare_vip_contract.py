import math

import pytest

from app.data_platform.services import data_center


def test_vip_missing_data_catalog_is_complete():
    names = {item.api_name for item in data_center.TUSHARE_CATALOG}
    assert {
        "moneyflow_hsgt",
        "hk_hold",
        "margin",
        "margin_detail",
        "margin_secs",
        "top_list",
        "top_inst",
        "major_news",
        "fund_nav",
        "etf_sh_cons",
        "etf_sz_cons",
    } <= names


def test_major_news_uses_publication_time_and_content_identity():
    payload = {
        "src": "sina",
        "pub_time": "2026-07-17 09:31:22",
        "title": "算力产业催化",
        "content": "正文一",
    }
    assert data_center._point_in_time_available_time(
        "major_news", payload, fallback="fallback"
    ) == "2026-07-17T09:31:22"
    first = data_center._record_key("major_news", payload, 0)
    second = data_center._record_key("major_news", {**payload, "content": "正文二"}, 0)
    assert len(first) == 64
    assert first != second


def test_raw_payload_json_rejects_non_standard_nan():
    assert data_center._strict_json({"value": None}) == '{"value": null}'
    with pytest.raises(ValueError):
        data_center._strict_json({"value": math.nan})


def test_margin_record_key_keeps_each_exchange_row():
    sh = data_center._record_key(
        "margin", {"trade_date": "20260716", "exchange_id": "SSE"}, 0
    )
    sz = data_center._record_key(
        "margin", {"trade_date": "20260716", "exchange_id": "SZSE"}, 1
    )
    assert sh != sz
    assert "SSE" in sh
    assert "SZSE" in sz


def test_capital_structure_bundle_uses_real_official_api_names(monkeypatch):
    calls = []

    def fake_sync(api_name, params):
        calls.append((api_name, params))
        return {"ok": True, "rows_written": 1}

    monkeypatch.setattr(data_center, "_safe_sync_raw", fake_sync)
    result = data_center.sync_capital_structure_bundle("20260717")

    assert result["ok"] is True
    assert result["succeeded"] == 10
    assert result["results"][0]["api_name"] == "moneyflow_hsgt"
    assert {api for api, _ in calls} == {
        "moneyflow_hsgt",
        "margin",
        "margin_detail",
        "top_list",
        "top_inst",
        "hk_hold",
        "margin_secs",
    }
    assert not any(api.endswith("_vip") for api, _ in calls)
    assert ("hk_hold", {"trade_date": "20260717", "exchange": "SH"}) in calls
    assert ("margin_secs", {"trade_date": "20260717", "exchange": "BSE"}) in calls


def test_history_backfill_uses_exact_exchange_resume_and_hk_cutoff(monkeypatch):
    class FakeConn:
        def execute(self, *_args):
            return self

        def fetchall(self):
            return [{"cal_date": "20240816"}, {"cal_date": "20240819"}]

    class FakeContext:
        def __enter__(self):
            return FakeConn()

        def __exit__(self, *_args):
            return False

    calls = []
    monkeypatch.setattr(data_center, "_conn", lambda: FakeContext())
    monkeypatch.setattr(data_center, "_request_is_complete", lambda *_args: False)
    monkeypatch.setattr(
        data_center,
        "_safe_sync_raw",
        lambda api, params: calls.append((api, params)) or {"ok": True, "rows_written": 1},
    )

    result = data_center.backfill_capital_structure_history(
        start_date="20240816", end_date="20240819", limit_dates=2
    )

    assert result["dates_processed"] == 2
    assert result["cursor"] == "20240819"
    assert sum(1 for api, _ in calls if api == "hk_hold") == 2
    assert not any(api == "hk_hold" and params["trade_date"] == "20240819" for api, params in calls)


def test_etf_history_filters_unlisted_and_uses_listing_date(monkeypatch):
    class FakeConn:
        def execute(self, *_args):
            return self

        def fetchall(self):
            return [
                {"ts_code": "510300.SH", "payload_json": {"name": "沪深300ETF", "list_date": "20120528"}},
                {"ts_code": "510301.SH", "payload_json": {"name": "普通LOF", "list_date": "20120528"}},
                {"ts_code": "588999.SH", "payload_json": {"name": "未来ETF", "list_date": "20270101"}},
            ]

    class FakeContext:
        def __enter__(self):
            return FakeConn()

        def __exit__(self, *_args):
            return False

    calls = []
    monkeypatch.setattr(data_center, "_conn", lambda: FakeContext())
    monkeypatch.setattr(data_center, "_request_is_complete", lambda *_args: False)
    monkeypatch.setattr(
        data_center,
        "_safe_sync_raw",
        lambda api, params: calls.append((api, params)) or {"ok": True, "rows_written": 1},
    )
    result = data_center.backfill_etf_price_history(
        start_date="20200101", end_date="20260717", limit_funds=20
    )
    assert result["funds_processed"] == 1
    assert {api for api, _ in calls} == {"fund_daily", "fund_nav"}
    assert all(params["start_date"] == "20200101" for _, params in calls)


def test_etf_constituent_history_slices_both_exchanges_by_trade_date(monkeypatch):
    class FakeConn:
        def execute(self, *_args):
            return self

        def fetchall(self):
            return [{"cal_date": "20260716"}, {"cal_date": "20260717"}]

    class FakeContext:
        def __enter__(self):
            return FakeConn()

        def __exit__(self, *_args):
            return False

    calls = []
    monkeypatch.setattr(data_center, "_conn", lambda: FakeContext())
    monkeypatch.setattr(data_center, "_request_is_complete", lambda *_args: False)
    monkeypatch.setattr(
        data_center,
        "_safe_sync_raw",
        lambda api, params: calls.append((api, params)) or {"ok": True, "rows_written": 1},
    )

    result = data_center.backfill_etf_constituents_history(
        start_date="20260716", end_date="20260717", limit_dates=2
    )

    assert result["dates_processed"] == 2
    assert result["cursor"] == "20260717"
    assert len(calls) == 4
    assert {api for api, _ in calls} == {"etf_sh_cons", "etf_sz_cons"}
    assert {params["trade_date"] for _, params in calls} == {"20260716", "20260717"}


def test_major_news_history_slices_each_source_by_calendar_day(monkeypatch):
    calls = []
    monkeypatch.setattr(data_center, "_request_is_complete", lambda *_args: False)
    monkeypatch.setattr(
        data_center,
        "_safe_sync_raw",
        lambda api, params: calls.append((api, params)) or {"ok": True, "rows_written": 1},
    )
    result = data_center.backfill_major_news_history(
        start_date="20230719", end_date="20230720", limit_days=7
    )
    assert result["days_processed"] == 2
    assert result["cursor"] == "20230720"
    assert result["has_more"] is False
    assert len(calls) == 2 * len(data_center.MAJOR_NEWS_SOURCES)
    assert all(params["start_date"].endswith("00:00:00") for _, params in calls)
    assert all(params["end_date"].endswith("23:59:59") for _, params in calls)
