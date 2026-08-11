from app.data_platform.services import oos_hard_data


def test_benchmark_gap_repair_uses_fallback_only_for_dates_still_missing(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.data_platform.services.eastmoney_index_daily_fallback.repair_hs300_index_daily_from_eastmoney",
        lambda trade_date: calls.append(trade_date) or {
            "ok": True, "status": "completed", "rows_written": 1,
        },
    )

    result = oos_hard_data._repair_benchmark_gaps(
        ["20260807", "20260810", "20260811"], {"20260807", "20260811"}
    )

    assert calls == ["20260810"]
    assert result == [{
        "trade_date": "20260810", "ok": True,
        "status": "completed", "rows_written": 1,
    }]
