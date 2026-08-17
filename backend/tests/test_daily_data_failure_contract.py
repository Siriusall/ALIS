from __future__ import annotations

import pytest

from app.services import daily_data


def test_required_daily_bars_fail_closed_without_postgres_coverage(monkeypatch):
    monkeypatch.setattr(daily_data, "_load_daily_from_postgres", lambda *_args: {})

    with pytest.raises(daily_data.DailyCacheNotReadyError):
        daily_data.get_daily_bars(
            ["000001"],
            "20260801",
            "20260814",
            require_cache=True,
            lookback_days_for_error=10,
        )


def test_lookback_never_rewrites_requested_end_date_to_previous_cache_day(monkeypatch):
    requested: list[str] = []

    def load(_symbols, _start, end):
        requested.append(end)
        return {"000001": [daily_data.DailyBar("2026-08-14", 1, 1, 1, 1, 1, 1)]}

    monkeypatch.setattr(daily_data, "_load_daily_from_postgres", load)

    result = daily_data.get_daily_bars_lookback(
        ["000001"], "20260814", lookback_days=5, require_cache=True,
    )

    assert result["000001"]
    assert requested == ["20260814"]


def test_required_daily_bars_reject_prior_session_inside_requested_range(monkeypatch):
    monkeypatch.setattr(
        daily_data,
        "_load_daily_from_postgres",
        lambda *_args: {"000001": [daily_data.DailyBar("2026-08-14", 1, 1, 1, 1, 1, 1)]},
    )

    with pytest.raises(daily_data.DailyCacheNotReadyError) as exc:
        daily_data.get_daily_bars(["000001"], "20260801", "20260815", require_cache=True)

    assert exc.value.end_date == "20260815"
