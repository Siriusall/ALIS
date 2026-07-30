from app.data_platform.services import data_center


def test_stk_limit_full_page_uses_bounded_offset_pagination(monkeypatch):
    calls = []
    coverage = [
        {"retained_rows": 0, "expected_stock_rows": 5500, "covered_stock_rows": 0, "coverage": 0.0, "ready": False},
        {"retained_rows": 5800, "expected_stock_rows": 5500, "covered_stock_rows": 3800, "coverage": 0.690909, "ready": False},
        {"retained_rows": 7600, "expected_stock_rows": 5500, "covered_stock_rows": 5500, "coverage": 1.0, "ready": True},
    ]
    monkeypatch.setattr(data_center, "_stk_limit_stock_coverage", lambda _date: coverage.pop(0))
    monkeypatch.setattr(data_center, "_safe_sync_raw", lambda api, params: calls.append((api, params)) or {
        "ok": True, "rows_read": 5800 if params["offset"] == 0 else 1800, "rows_written": 1,
    })

    result = data_center.sync_complete_stk_limit_slice("20260724")

    assert result["ok"] is True
    assert [item[1]["offset"] for item in calls] == [0, 5800]
    assert result["coverage"]["ready"] is True


def test_stk_limit_repeated_full_page_is_classified_as_truncated(monkeypatch):
    coverage = [
        {"retained_rows": 6000, "expected_stock_rows": 5500, "covered_stock_rows": 3800, "coverage": 0.69, "ready": False},
        {"retained_rows": 6000, "expected_stock_rows": 5500, "covered_stock_rows": 3800, "coverage": 0.69, "ready": False},
        {"retained_rows": 6000, "expected_stock_rows": 5500, "covered_stock_rows": 3800, "coverage": 0.69, "ready": False},
    ]
    monkeypatch.setattr(data_center, "_stk_limit_stock_coverage", lambda _date: coverage.pop(0))
    monkeypatch.setattr(data_center, "_safe_sync_raw", lambda *_args, **_kwargs: {
        "ok": True, "rows_read": data_center.STK_LIMIT_PAGE_SIZE, "rows_written": data_center.STK_LIMIT_PAGE_SIZE,
    })
    monkeypatch.setattr(data_center, "sync_stk_limit_gap_batch", lambda _date: {
        "ok": False, "category": "truncated", "coverage": coverage[-1] if coverage else {
            "retained_rows": 6000, "expected_stock_rows": 5500, "covered_stock_rows": 3800, "coverage": 0.69, "ready": False,
        },
    })

    result = data_center.sync_complete_stk_limit_slice("20260724")

    assert result["ok"] is False
    assert result["category"] == "truncated"
    assert len(result["pages"]) == 2


def test_gap_fallback_stops_after_three_empty_probes(monkeypatch):
    calls = []
    monkeypatch.setattr(data_center, "_missing_stk_limit_stock_codes", lambda *_args, **_kwargs: [
        "603001.SH", "603002.SH", "603003.SH", "603004.SH",
    ])
    monkeypatch.setattr(data_center, "_safe_sync_raw", lambda _api, params: calls.append(params["ts_code"]) or {
        "ok": False, "rows_read": 0, "category": "upstream_not_ready",
    })
    monkeypatch.setattr(data_center, "_stk_limit_stock_coverage", lambda _date: {
        "expected_stock_rows": 5500, "covered_stock_rows": 3800, "retained_rows": 6000,
        "coverage": 0.69, "ready": False,
    })

    result = data_center.sync_stk_limit_gap_batch("20260724", limit=25)

    assert calls == ["603001.SH", "603002.SH", "603003.SH"]
    assert result["category"] == "upstream_not_ready"
    assert result["cooldown_seconds"] == 600


def test_empty_first_page_uses_calibrated_rule_fallback(monkeypatch):
    coverage = [
        {"retained_rows": 0, "expected_stock_rows": 5500, "covered_stock_rows": 0, "coverage": 0.0, "ready": False},
        {"retained_rows": 0, "expected_stock_rows": 5500, "covered_stock_rows": 0, "coverage": 0.0, "ready": False},
        {"retained_rows": 5500, "expected_stock_rows": 5500, "covered_stock_rows": 5500, "coverage": 1.0, "ready": True},
    ]
    monkeypatch.setattr(data_center, "_stk_limit_stock_coverage", lambda _date: coverage.pop(0))
    monkeypatch.setattr(data_center, "_safe_sync_raw", lambda *_args, **_kwargs: {
        "ok": False,
        "rows_read": 0,
        "category": "upstream_not_ready",
    })
    monkeypatch.setattr(
        "app.data_platform.services.derived_limit_price.repair_stk_limit_from_exchange_rules",
        lambda _date: {"ok": True, "requested": 5500, "rows_written": 5500},
    )

    result = data_center.sync_complete_stk_limit_slice("20260728")

    assert result["ok"] is True
    assert result["category"] == "complete"
    assert result["rule_fallback"]["rows_written"] == 5500
