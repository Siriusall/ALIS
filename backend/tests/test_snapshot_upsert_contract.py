from __future__ import annotations

import json

import pytest

from app.data_platform.services.data_center import _strict_index_bar, _upsert_snapshot_row


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, *, is_mysql: bool):
        self._is_postgres = True
        self._is_mysql = is_mysql
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.schema_checks = 0

    def execute(self, sql, params=()):
        if "information_schema.columns" in sql:
            self.schema_checks += 1
            return _Result([{"column_name": column} for column in (
                "economic_available_time",
                "observed_at",
                "first_observed_at",
                "observation_provenance",
                "content_hash",
            )])
        self.calls.append((sql, tuple(params)))
        return _Result([])


@pytest.mark.parametrize("is_mysql", [False, True])
def test_clocked_snapshot_upsert_has_one_parameter_per_column(is_mysql: bool):
    connection = _Connection(is_mysql=is_mysql)
    values = tuple(range(32))

    _upsert_snapshot_row(connection, values)

    assert len(connection.calls) == 1
    sql, params = connection.calls[0]
    assert sql.count("?") == len(params) == 32
    if not is_mysql:
        assert "content_hash IS DISTINCT FROM excluded.content_hash" in sql


def test_clocked_snapshot_upsert_reuses_materialization_schema_check():
    connection = _Connection(is_mysql=False)

    _upsert_snapshot_row(connection, tuple(range(32)), has_clock=True)
    _upsert_snapshot_row(connection, tuple(range(32)), has_clock=True)

    assert connection.schema_checks == 0
    assert len(connection.calls) == 2


def test_strict_benchmark_accepts_audited_fallback_previous_close(monkeypatch):
    payload = {
        "open": 10,
        "high": 11,
        "low": 9,
        "close": 10.5,
        "previous_tushare_close": 9.8,
        "vol": 100,
    }
    row = {
        "trade_date": "20260814",
        "source_code": "sina_index_daily_fallback",
        "economic_available_time": "2026-08-14T15:00:00+08:00",
        "observed_at": "2026-08-14T16:06:50+08:00",
        "first_observed_at": "2026-08-14T16:06:50+08:00",
        "observation_provenance": "sina_index_daily_fallback:validated_exact_session",
        "content_hash": "hash",
        "created_at": "2026-08-14T16:06:50+08:00",
        "payload_json": json.dumps(payload),
    }
    monkeypatch.setattr(
        "app.data_platform.services.data_center.load_raw_record_versions",
        lambda *_args, **_kwargs: [row],
    )

    result = _strict_index_bar(object(), "000300.SH", "20260814")

    assert result["pre_close"] == 9.8
