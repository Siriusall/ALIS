import math

import pytest

from app.data_platform.services import data_center


def test_supported_vip_data_catalog_is_complete():
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
        "report_rc",
        "repurchase",
        "stk_holdertrade",
        "fund_nav",
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


def test_report_rc_key_keeps_broker_report_and_forecast_period_identity():
    base = {
        "ts_code": "000001.SZ",
        "report_date": "20260720",
        "org_name": "A券商",
        "author_name": "分析师甲",
        "report_title": "公司盈利预测",
        "report_type": "点评",
        "quarter": "2026Q4",
    }
    first = data_center._record_key("report_rc", base, 0)
    next_period = data_center._record_key(
        "report_rc", {**base, "quarter": "2027Q4"}, 0
    )
    next_broker = data_center._record_key(
        "report_rc", {**base, "org_name": "B券商"}, 0
    )

    assert len(first) == 64
    assert len({first, next_period, next_broker}) == 3


def test_monthly_macro_record_key_is_stable_by_month_not_dataframe_position():
    july = data_center._record_key("cn_ppi", {"month": "202607", "ppi_yoy": 1.2}, 0)
    july_reordered = data_center._record_key("cn_ppi", {"month": "202607", "ppi_yoy": 1.2}, 416)
    june = data_center._record_key("cn_ppi", {"month": "202606", "ppi_yoy": 1.1}, 0)

    assert july == july_reordered == "202607"
    assert june == "202606"
    assert june != july


def test_monthly_macro_key_normalization_keeps_latest_and_removes_duplicate():
    calls = []

    class Rows:
        def fetchall(self):
            return [
                {"id": 2, "record_key": "0", "payload_json": '{"month":"202606","ppi_yoy":4.1}', "updated_at": "2026-08-11"},
                {"id": 1, "record_key": "legacy", "payload_json": '{"month":"202606","ppi_yoy":4.0}', "updated_at": "2026-07-17"},
            ]

    class Connection:
        def execute(self, sql, params=()):
            calls.append((sql, params))
            return Rows()

    result = data_center._normalize_monthly_raw_record_keys(Connection(), "cn_ppi")

    assert result == {"changed": 1, "deleted_duplicates": 1}
    assert any("DELETE FROM dc_tushare_raw_records" in sql and params == (1,) for sql, params in calls)
    assert any("UPDATE dc_tushare_raw_records SET record_key" in sql and params == ("202606", 2) for sql, params in calls)


def test_fund_nav_empty_only_becomes_terminal_after_publish_lag(monkeypatch):
    monkeypatch.setattr(data_center, "_etf_nav_publish_cutoff", lambda _date: "20260807")

    assert data_center._mature_fund_nav_empty(
        {"end_date": "20260807"}, today_value=data_center.date(2026, 8, 11)
    ) is True
    assert data_center._mature_fund_nav_empty(
        {"end_date": "20260810"}, today_value=data_center.date(2026, 8, 11)
    ) is False


def test_optional_hsgt_empty_only_becomes_terminal_after_publish_lag(monkeypatch):
    monkeypatch.setattr(data_center, "_etf_nav_publish_cutoff", lambda _date: "20260807")

    assert data_center._mature_optional_daily_empty(
        "moneyflow_hsgt", {"trade_date": "20260807"}, today_value=data_center.date(2026, 8, 11)
    ) is True
    assert data_center._mature_optional_daily_empty(
        "moneyflow_hsgt", {"trade_date": "20260810"}, today_value=data_center.date(2026, 8, 11)
    ) is False


def test_valid_empty_cleanup_batches_coverage_and_resolution(monkeypatch):
    batches = []

    class Cursor:
        def executemany(self, sql, rows):
            batches.append((sql, list(rows)))

    class Raw:
        def cursor(self):
            return Cursor()

    class Connection:
        _is_postgres = True
        raw = Raw()

    class Context:
        def __enter__(self):
            return Connection()

        def __exit__(self, *_args):
            return False

    removed = []
    monkeypatch.setattr(data_center, "_conn", lambda: Context())
    monkeypatch.setattr(data_center, "redis_set_remove", lambda namespace, key: removed.append((namespace, key)))

    resolved = data_center._bulk_mark_requests_valid_empty(
        "fund_nav",
        [(11, {"ts_code": "159001.SZ", "end_date": "20260807"}, None),
         (12, {"ts_code": "159002.SZ", "end_date": "20260807"}, None)],
        reason="mature",
    )

    assert resolved == 2
    assert len(batches) == 2
    assert len(batches[0][1]) == len(batches[1][1]) == 2
    assert len(removed) == 2
    assert data_center._mature_optional_daily_empty(
        "moneyflow", {"trade_date": "20260807"}, today_value=data_center.date(2026, 8, 11)
    ) is False


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


def test_major_news_history_batches_days_by_source_and_preserves_daily_coverage(monkeypatch):
    calls = []
    coverage = []
    monkeypatch.setattr(data_center, "_request_is_complete", lambda *_args: False)
    monkeypatch.setattr(
        data_center,
        "_record_request_coverage",
        lambda api, params, limit, rows, **kwargs: coverage.append((api, params, rows, kwargs)),
    )
    monkeypatch.setattr(
        data_center,
        "_safe_sync_raw",
        lambda api, params: calls.append((api, params))
        or {"ok": True, "rows_read": 10, "rows_written": 10},
    )
    result = data_center.backfill_major_news_history(
        start_date="20230719", end_date="20230720", limit_days=7
    )
    assert result["days_processed"] == 2
    assert result["cursor"] == "20230720"
    assert result["has_more"] is False
    assert len(calls) == len(data_center.MAJOR_NEWS_SOURCES)
    assert len(coverage) == 2 * len(data_center.MAJOR_NEWS_SOURCES)
    assert all(params["start_date"].endswith("00:00:00") for _, params in calls)
    assert all(params["end_date"].endswith("23:59:59") for _, params in calls)


def test_major_news_history_splits_capped_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(data_center, "MAJOR_NEWS_SOURCES", ("同花顺",))
    monkeypatch.setattr(data_center, "_request_is_complete", lambda *_args: False)
    monkeypatch.setattr(data_center, "_record_request_coverage", lambda *_args, **_kwargs: None)

    def sync(_api, params):
        calls.append(params)
        is_wide = params["start_date"][:10] != params["end_date"][:10]
        return {
            "ok": True,
            "rows_read": 800 if is_wide else 100,
            "rows_written": 100,
        }

    monkeypatch.setattr(data_center, "_safe_sync_raw", sync)

    result = data_center.backfill_major_news_history(
        start_date="20230719", end_date="20230720", limit_days=7
    )

    assert result["ok"] is True
    assert len(calls) == 3
    assert result["requests_executed"] == 3


def test_event_research_history_uses_resumable_month_slices(monkeypatch):
    calls = []
    monkeypatch.setattr(data_center, "_request_is_complete", lambda *_args: False)
    monkeypatch.setattr(
        data_center,
        "_safe_sync_raw",
        lambda api, params: calls.append((api, params))
        or {"ok": True, "rows_read": 10, "rows_written": 10},
    )

    result = data_center.backfill_event_research_history(
        start_date="20260101",
        end_date="20260315",
        cursor="20260131",
        limit_months=1,
    )

    assert result["cursor"] == "20260228"
    assert result["has_more"] is True
    assert {api for api, _ in calls} == {
        "report_rc", "repurchase", "stk_holdertrade"
    }
    assert all(params == {"start_date": "20260201", "end_date": "20260228"} for _, params in calls)


def test_moneyflow_raw_coverage_uses_bounded_queries_and_stops_at_batch_limit(monkeypatch):
    queries = []
    verified = {}
    wanted = [f"202601{index:02d}" for index in range(1, 46)]
    missing = {wanted[0], wanted[21]}

    class Connection:
        batch = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _query, params):
            self.batch = list(params[1:-1])
            queries.append(self.batch)
            return self

        def fetchall(self):
            return [
                {"trade_date": trade_date, "row_count": 520}
                for trade_date in self.batch
                if trade_date not in missing
            ]

    monkeypatch.setattr(data_center, "_conn", lambda: Connection())

    pending = data_center._pending_raw_coverage_dates(
        "moneyflow",
        wanted,
        pending_limit=2,
        minimum_rows=500,
        require_point_in_time=True,
        chunk_size=20,
        verified_rows=verified,
    )

    assert pending == [wanted[0], wanted[21]]
    assert [len(batch) for batch in queries] == [20, 20]
    assert verified[wanted[1]] == 520


def test_moneyflow_coverage_timeout_is_deferred_without_upstream_calls(monkeypatch):
    monkeypatch.setattr(
        data_center,
        "_pending_raw_coverage_dates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("canceling statement due to statement timeout")
        ),
    )
    monkeypatch.setattr(
        data_center,
        "_parallel_tushare_map",
        lambda *_args, **_kwargs: pytest.fail("upstream must not run after coverage timeout"),
    )

    result = data_center.sync_tushare_date_batch(
        "moneyflow",
        trade_dates=["20260730", "20260731"],
        limit=30,
        require_rows=True,
        minimum_rows=500,
        require_point_in_time=True,
    )

    assert result["ok"] is False
    assert result["deferred"] is True
    assert result["failed"] == 1
    assert result["error_category"] == "coverage_query_timeout"


def test_data_center_catalog_seed_runs_once_per_process(monkeypatch):
    calls = []
    monkeypatch.setattr(data_center, "_data_center_seeded", False)
    monkeypatch.setattr(
        data_center,
        "_seed_data_center_catalog",
        lambda: calls.append(True) or {"ok": True, "catalog_count": 1},
    )

    first = data_center.ensure_data_center_seeded()
    second = data_center.ensure_data_center_seeded()

    assert first["ok"] is True
    assert second["ok"] is True
    assert calls == [True]
