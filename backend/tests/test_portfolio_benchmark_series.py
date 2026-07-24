from app.services.profit_loop import _attach_benchmark_series, _available_by_market_close


def test_benchmark_series_is_normalized_on_common_first_date():
    series = [
        {"date": "20260717", "close": 100.0, "change_pct": 0.0},
        {"date": "20260720", "close": 102.0, "change_pct": 2.0},
        {"date": "20260721", "close": 101.0, "change_pct": -0.9804},
    ]
    enriched, latest = _attach_benchmark_series(
        series,
        {"20260717": 4000.0, "20260720": 4040.0, "20260721": 4080.0},
    )

    assert enriched[0]["benchmark_close"] == 100.0
    assert enriched[1]["benchmark_close"] == 101.0
    assert enriched[1]["excess_close"] == 1.0
    assert latest["portfolio_return_pct"] == 1.0
    assert latest["benchmark_return_pct"] == 2.0
    assert latest["excess_return_pct"] == -1.0
    assert latest["daily_excess_return_pct"] < -1.9


def test_benchmark_series_never_backfills_missing_dates():
    series = [
        {"date": "20260717", "close": 100.0, "change_pct": 0.0},
        {"date": "20260720", "close": 101.0, "change_pct": 1.0},
    ]
    enriched, latest = _attach_benchmark_series(series, {"20260720": 4100.0})

    assert enriched[0]["benchmark_close"] is None
    assert enriched[1]["benchmark_close"] == 100.0
    assert latest["excess_return_pct"] == 1.0


def test_benchmark_available_time_must_be_no_later_than_market_close():
    assert _available_by_market_close("2026-07-23T15:00:00+08:00", "20260723")
    assert not _available_by_market_close("2026-07-23T15:00:01+08:00", "20260723")
    assert not _available_by_market_close("", "20260723")
