from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from app.data_platform.services import observation_clock
from app.services import position_valuation


def _connection_context(connection):
    @contextmanager
    def _open():
        yield connection

    return _open


def test_strict_position_valuation_rejects_previous_trade_date_as_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A formal valuation must not turn the last available close into T's close."""

    class Connection:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("strict valuation must not read the mutable snapshot")

    def versions(_connection, **kwargs):
        assert kwargs["cutoff"] == "2026-08-15T15:00:00+08:00"
        assert kwargs["trade_date_end"] == "20260815"
        return [{
            "trade_date": "20260814",
            "payload_json": json.dumps({"close": 10.2, "adj_factor": 1.0}),
        }]

    monkeypatch.setattr(position_valuation, "data_conn", _connection_context(Connection()))
    monkeypatch.setattr(observation_clock, "load_snapshot_versions", versions)

    result = position_valuation.point_in_time_position_market_series(
        symbol="000001",
        as_of="20260815",
        fallback_price=9.8,
        observation_cutoff="2026-08-15T15:00:00+08:00",
    )

    assert result["price"] == 9.8
    assert result["price_source"] == "position_cost_fallback"
    assert result["trade_date"] == "20260815"
    assert result["observation_error"] == "strict_snapshot_trade_date_missing"


def test_strict_position_valuation_reads_target_day_immutable_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target-day immutable observation remains usable under its frozen cutoff."""

    class Connection:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("immutable version evidence should satisfy the read")

    def versions(_connection, **kwargs):
        assert kwargs["ts_codes"] == ["000001.SZ"]
        return [
            {
                "trade_date": "20260814",
                "payload_json": json.dumps({"close": 10.0, "adj_factor": 2.0}),
            },
            {
                "trade_date": "20260815",
                "payload_json": json.dumps({"close": 10.5, "adj_factor": 2.0}),
            },
        ]

    monkeypatch.setattr(position_valuation, "data_conn", _connection_context(Connection()))
    monkeypatch.setattr(observation_clock, "load_snapshot_versions", versions)

    result = position_valuation.point_in_time_position_market_series(
        symbol="000001",
        as_of="20260815",
        fallback_price=9.8,
        observation_cutoff="2026-08-15T15:00:00+08:00",
    )

    assert result["price"] == 10.5
    assert result["price_source"] == "stock_daily_point_in_time_close"
    assert result["trade_date"] == "20260815"
    assert result["observation_cutoff"] == "2026-08-15T15:00:00+08:00"
    assert result["returns"] == [pytest.approx(0.05)]


def test_strict_position_valuation_rejects_target_version_without_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An incomplete target-day version cannot fall through to yesterday's close."""

    class Connection:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("strict valuation must not read the mutable snapshot")

    monkeypatch.setattr(position_valuation, "data_conn", _connection_context(Connection()))
    monkeypatch.setattr(
        observation_clock,
        "load_snapshot_versions",
        lambda *_args, **_kwargs: [
            {
                "trade_date": "20260814",
                "payload_json": json.dumps({"close": 10.2, "adj_factor": 1.0}),
            },
            {
                "trade_date": "20260815",
                "payload_json": json.dumps({"close": None, "adj_factor": 1.0}),
            },
        ],
    )

    result = position_valuation.point_in_time_position_market_series(
        symbol="000001",
        as_of="20260815",
        fallback_price=9.8,
        observation_cutoff="2026-08-15T15:00:00+08:00",
    )

    assert result["price"] == 9.8
    assert result["price_source"] == "position_cost_fallback"
    assert result["observation_error"] == "strict_snapshot_target_close_missing"


def test_strict_position_valuation_cache_rejects_incomplete_target_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The formal multi-position entrypoint preserves the same target-day rule."""

    class Connection:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("strict valuation must not read the mutable snapshot")

    records = [
        {
            "ts_code": "000001.SZ",
            "trade_date": "20260814",
            "payload_json": json.dumps({"close": 10.2, "adj_factor": 1.0}),
        },
        {
            "ts_code": "000001.SZ",
            "trade_date": "20260815",
            "payload_json": json.dumps({"close": 0, "adj_factor": 1.0}),
        },
    ]
    monkeypatch.setattr(position_valuation, "data_conn", _connection_context(Connection()))
    monkeypatch.setattr(
        observation_clock,
        "load_snapshot_versions",
        lambda *_args, **_kwargs: records,
    )

    result = position_valuation.build_position_market_series_cache(
        [{"symbol": "000001", "quantity": 100, "price": 9.8}],
        as_of="20260815",
        observation_cutoff="2026-08-15T15:00:00+08:00",
    )

    assert result["000001"]["price"] == 9.8
    assert result["000001"]["price_source"] == "position_cost_fallback"
    assert result["000001"]["observation_error"] == "strict_snapshot_target_close_missing"


def test_live_position_cache_requires_the_requested_session_without_a_frozen_plan_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Account/report reads cannot use the prior close just because no plan is frozen."""

    class Connection:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("exact live valuation must not widen to mutable history")

    monkeypatch.setattr(position_valuation, "data_conn", _connection_context(Connection()))
    monkeypatch.setattr(
        position_valuation,
        "_live_observation_cutoff",
        lambda: "2026-08-15T18:00:00+08:00",
    )
    monkeypatch.setattr(
        observation_clock,
        "load_snapshot_versions",
        lambda *_args, **_kwargs: [{
            "ts_code": "000001.SZ",
            "trade_date": "20260814",
            "payload_json": json.dumps({"close": 10.2, "adj_factor": 1.0}),
        }],
    )

    result = position_valuation.build_position_market_series_cache(
        [{"symbol": "000001", "quantity": 100, "price": 9.8}],
        as_of="20260815",
    )

    assert result["000001"]["price_source"] == "position_cost_fallback"
    assert result["000001"]["observation_error"] == "strict_snapshot_trade_date_missing"
    assert position_valuation.missing_exact_position_valuations(
        [{"symbol": "000001", "quantity": 100}], result, as_of="20260815",
    ) == {"000001": "strict_snapshot_trade_date_missing"}


def test_position_valuation_rejects_naive_formal_cutoff() -> None:
    result = position_valuation.point_in_time_position_market_series(
        symbol="000001",
        as_of="20260815",
        fallback_price=9.8,
        observation_cutoff="2026-08-15T15:00:00",
    )

    assert result == {
        "price": 9.8,
        "returns": [],
        "price_source": "position_cost_fallback",
        "trade_date": "20260815",
        "observation_cutoff": "",
        "observation_error": "invalid_observation_cutoff",
    }
