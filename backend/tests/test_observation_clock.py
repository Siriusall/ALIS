from __future__ import annotations

import json
import sqlite3

from app.data_platform.services import observation_clock
from app.data_platform.services.observation_clock import (
    load_current_raw_record_versions,
    load_raw_record_versions,
    load_snapshot_market_aggregates,
    load_snapshot_versions,
    normalize_explicit_timestamp,
    observation_metadata,
    upsert_raw_record,
)


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE dc_tushare_raw_record_version(
            id INTEGER PRIMARY KEY,
            api_name TEXT NOT NULL,
            record_key TEXT NOT NULL,
            ts_code TEXT NOT NULL DEFAULT '',
            trade_date TEXT NOT NULL DEFAULT '',
            period TEXT NOT NULL DEFAULT '',
            ann_date TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            source_code TEXT NOT NULL DEFAULT '',
            economic_available_time TEXT NOT NULL DEFAULT '',
            observed_at TEXT NOT NULL DEFAULT '',
            first_observed_at TEXT NOT NULL DEFAULT '',
            observation_provenance TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE dc_stock_daily_snapshot_version(
            id INTEGER PRIMARY KEY,
            ts_code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            source_code TEXT NOT NULL DEFAULT '',
            economic_available_time TEXT NOT NULL DEFAULT '',
            observed_at TEXT NOT NULL DEFAULT '',
            first_observed_at TEXT NOT NULL DEFAULT '',
            observation_provenance TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        );
        """
    )
    return connection


def _connection_with_accepted_projections() -> sqlite3.Connection:
    connection = _connection()
    connection.executescript(
        """
        CREATE TABLE dc_tushare_raw_records(
            api_name TEXT NOT NULL,
            record_key TEXT NOT NULL,
            content_hash TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(api_name, record_key)
        );
        CREATE TABLE dc_stock_daily_snapshot(
            ts_code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            content_hash TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(ts_code, trade_date)
        );
        """
    )
    return connection


def test_immutable_loaders_reject_blank_legacy_and_naive_observation_evidence():
    connection = _connection()
    valid_time = "2026-08-14T14:30:00+08:00"
    later_time = "2026-08-14T14:45:00+08:00"
    payload = json.dumps({"close": 10})
    raw_rows = [
        (1, "daily", "daily:000001.SZ:20260814", "000001.SZ", "20260814", "", "", payload, "tushare", valid_time, valid_time, valid_time, "tushare:direct_fetch", "valid", valid_time),
        # A newer blank row must be rejected before the logical version rank.
        (2, "daily", "daily:000001.SZ:20260814", "000001.SZ", "20260814", "", "", payload, "tushare", "", later_time, later_time, "   ", "blank", later_time),
        (3, "daily", "daily:000002.SZ:20260814", "000002.SZ", "20260814", "", "", payload, "tushare", "", later_time, later_time, "legacy_unknown", "legacy", later_time),
        (4, "daily", "daily:000003.SZ:20260814", "000003.SZ", "20260814", "", "", payload, "tushare", "", "2026-08-14T14:45:00", "2026-08-14T14:45:00", "tushare:direct_fetch", "naive", "2026-08-14T14:45:00"),
    ]
    connection.executemany(
        """INSERT INTO dc_tushare_raw_record_version
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        raw_rows,
    )
    snapshot_rows = [
        (1, "000001.SZ", "20260814", payload, "tushare", valid_time, valid_time, valid_time, "tushare:normalized", "valid", valid_time),
        (2, "000001.SZ", "20260814", payload, "tushare", "", later_time, later_time, "", "blank", later_time),
        (3, "000002.SZ", "20260814", payload, "tushare", "", later_time, later_time, "legacy_unknown", "legacy", later_time),
        (4, "000003.SZ", "20260814", payload, "tushare", "", "2026-08-14T14:45:00", "2026-08-14T14:45:00", "tushare:normalized", "naive", "2026-08-14T14:45:00"),
    ]
    connection.executemany(
        """INSERT INTO dc_stock_daily_snapshot_version
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        snapshot_rows,
    )

    raw = load_raw_record_versions(connection, "daily", cutoff="2026-08-14T15:00:00+08:00")
    snapshots = load_snapshot_versions(connection, cutoff="2026-08-14T15:00:00+08:00")

    assert [(row["ts_code"], row["content_hash"]) for row in raw or []] == [("000001.SZ", "valid")]
    assert [(row["ts_code"], row["content_hash"]) for row in snapshots or []] == [("000001.SZ", "valid")]


def test_snapshot_loader_per_code_limit_keeps_each_security_window():
    connection = _connection_with_accepted_projections()
    rows = []
    accepted = []
    record_id = 1
    for code in ("000001.SZ", "000002.SZ"):
        for offset in range(4):
            trade_date = f"202608{14 - offset:02d}"
            observed = f"2026-08-{14 - offset:02d}T15:01:00+08:00"
            content_hash = f"{code}-{trade_date}"
            rows.append((
                record_id, code, trade_date, json.dumps({"close": record_id}),
                "tushare", observed, observed, observed,
                "snapshot:test", content_hash, observed,
            ))
            accepted.append((code, trade_date, content_hash))
            record_id += 1
    connection.executemany(
        """INSERT INTO dc_stock_daily_snapshot_version
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    connection.executemany(
        """INSERT INTO dc_stock_daily_snapshot(ts_code,trade_date,content_hash)
           VALUES(?,?,?)""",
        accepted,
    )

    visible = load_snapshot_versions(
        connection,
        cutoff="2026-08-14T16:00:00+08:00",
        ts_codes=["000001.SZ", "000002.SZ"],
        per_ts_code_limit=2,
    )

    assert [
        (row["ts_code"], row["trade_date"])
        for row in visible or []
    ] == [
        ("000001.SZ", "20260813"), ("000002.SZ", "20260813"),
        ("000001.SZ", "20260814"), ("000002.SZ", "20260814"),
    ]


def test_observation_metadata_does_not_reinterpret_naive_source_clock_as_actual_fetch_time(monkeypatch):
    monkeypatch.setattr(
        "app.data_platform.services.observation_clock.now_observed_at",
        lambda: "2026-08-14T16:00:00+08:00",
    )

    metadata = observation_metadata(
        "daily",
        {"trade_date": "20260814", "close": 10},
        source_code="tushare",
        observed_at="2026-08-14T14:30:00",
    )

    assert metadata["observed_at"] == "2026-08-14T16:00:00+08:00"
    assert metadata["first_observed_at"] == "2026-08-14T16:00:00+08:00"


def test_immutable_loaders_fail_closed_for_naive_cutoff():
    connection = _connection()
    assert normalize_explicit_timestamp("2026-08-14T15:00:00") == ""
    assert load_raw_record_versions(
        connection, "daily", cutoff="2026-08-14T15:00:00"
    ) == []
    assert load_snapshot_versions(
        connection, cutoff="2026-08-14T15:00:00"
    ) == []


def test_economic_source_clock_is_not_guessed_when_explicit_value_is_naive():
    metadata = observation_metadata(
        "major_news",
        {"pub_time": "2026-08-14T14:30:00"},
        source_code="news",
        observed_at="2026-08-14T16:00:00+08:00",
    )
    assert metadata["economic_available_time"] == ""


def test_own_trade_date_cutoff_keeps_visible_version_and_excludes_late_repair():
    connection = _connection()
    before_close = "2026-08-14T14:59:00+08:00"
    after_close = "2026-08-14T16:01:00+08:00"
    early = json.dumps({"close": 10})
    late = json.dumps({"close": 11})
    connection.executemany(
        """INSERT INTO dc_tushare_raw_record_version
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (1, "daily", "daily:000001.SZ:20260814", "000001.SZ", "20260814", "", "", early, "tushare", "2026-08-14T15:00:00+08:00", before_close, before_close, "tushare:direct_fetch", "early", before_close),
            (2, "daily", "daily:000001.SZ:20260814", "000001.SZ", "20260814", "", "", late, "tushare", "2026-08-14T15:00:00+08:00", after_close, after_close, "tushare:direct_fetch", "late", after_close),
            # Bad persisted clock text must be excluded rather than aborting
            # the complete point-in-time query.
            (3, "daily", "daily:000002.SZ:20260814", "000002.SZ", "20260814", "", "", late, "tushare", "", "2026-99-99T16:01:00+08:00", "2026-99-99T16:01:00+08:00", "tushare:direct_fetch", "bad", "2026-99-99T16:01:00+08:00"),
        ],
    )
    connection.executemany(
        """INSERT INTO dc_stock_daily_snapshot_version
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (1, "000001.SZ", "20260814", early, "tushare", "2026-08-14T15:00:00+08:00", before_close, before_close, "snapshot:raw_dependency_observation_v1", "early", before_close),
            (2, "000001.SZ", "20260814", late, "tushare", "2026-08-14T15:00:00+08:00", after_close, after_close, "snapshot:raw_dependency_observation_v1", "late", after_close),
            (3, "000002.SZ", "20260814", late, "tushare", "", "2026-99-99T16:01:00+08:00", "2026-99-99T16:01:00+08:00", "snapshot:raw_dependency_observation_v1", "bad", "2026-99-99T16:01:00+08:00"),
        ],
    )

    raw = load_raw_record_versions(connection, "daily", cutoff_by_trade_date=True)
    snapshots = load_snapshot_versions(connection, cutoff_by_trade_date=True)

    assert [(row["ts_code"], row["content_hash"]) for row in raw or []] == [("000001.SZ", "early")]
    assert [(row["ts_code"], row["content_hash"]) for row in snapshots or []] == [("000001.SZ", "early")]


def test_market_aggregate_uses_only_immutable_versions_visible_at_session_close():
    connection = _connection()
    before_close = "2026-08-14T14:59:00+08:00"
    after_close = "2026-08-14T16:01:00+08:00"
    early_one = json.dumps({"change_pct": 1.0, "turnover": 100.0})
    early_two = json.dumps({"change_pct": -2.0, "turnover": 300.0})
    late_repair = json.dumps({"change_pct": -9.9, "turnover": 9999.0})
    connection.executemany(
        """INSERT INTO dc_stock_daily_snapshot_version
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (1, "000001.SZ", "20260814", early_one, "tushare", "2026-08-14T15:00:00+08:00", before_close, before_close, "snapshot:raw_dependency_observation_v1", "early-one", before_close),
            (2, "000002.SZ", "20260814", early_two, "tushare", "2026-08-14T15:00:00+08:00", before_close, before_close, "snapshot:raw_dependency_observation_v1", "early-two", before_close),
            (3, "000001.SZ", "20260814", late_repair, "tushare", "2026-08-14T15:00:00+08:00", after_close, after_close, "snapshot:raw_dependency_observation_v1", "late", after_close),
        ],
    )

    result = load_snapshot_market_aggregates(connection, trade_dates=["2026-08-14"])

    assert result == {
        "20260814": {
            "trade_date": "20260814",
            "rows_total": 2,
            "market_rows": 2,
            "breadth": 0.5,
            "average_return": -0.005,
            "amount": 400.0,
            "limit_up_ratio": 0.0,
            "limit_down_ratio": 0.0,
            "top_5pct_turnover_concentration": 0.75,
        },
    }


def test_sqlite_timestamp_parameters_are_compared_as_instants():
    connection = _connection()
    early = "2026-08-14T14:30:00+08:00"
    late = "2026-08-14T16:30:00+08:00"
    payload = json.dumps({"close": 10})
    connection.executemany(
        """INSERT INTO dc_tushare_raw_record_version
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (1, "daily", "daily:early", "000001.SZ", "20260814", "", "", payload, "tushare", early, early, early, "tushare:direct_fetch", "early", early),
            (2, "daily", "daily:late", "000002.SZ", "20260814", "", "", payload, "tushare", late, late, late, "tushare:direct_fetch", "late", late),
        ],
    )

    visible = load_raw_record_versions(
        connection, "daily", cutoff="2026-08-14T15:00:00+08:00",
    )
    future = load_raw_record_versions(
        connection, "daily", observed_start="2026-08-14T15:00:00+08:00",
    )

    assert [(row["ts_code"], row["content_hash"]) for row in visible or []] == [("000001.SZ", "early")]
    assert [(row["ts_code"], row["content_hash"]) for row in future or []] == [("000002.SZ", "late")]


def test_raw_loader_keeps_first_accepted_provider_when_later_version_arrives():
    connection = _connection_with_accepted_projections()
    early = "2026-08-14T15:01:00+08:00"
    late = "2026-08-14T15:02:00+08:00"
    connection.executemany(
        """INSERT INTO dc_tushare_raw_record_version
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (1, "daily", "daily:000001.SZ:20260814", "000001.SZ", "20260814", "", "",
             json.dumps({"close": 10}), "tencent_market_snapshot", early, early, early,
             "tencent_market_snapshot:validated", "fallback", early),
            (2, "daily", "daily:000001.SZ:20260814", "000001.SZ", "20260814", "", "",
             json.dumps({"close": 11}), "tushare", late, late, late,
             "tushare:direct_fetch", "primary", late),
        ],
    )
    connection.execute(
        "INSERT INTO dc_tushare_raw_records(api_name,record_key,content_hash) VALUES(?,?,?)",
        ("daily", "daily:000001.SZ:20260814", "fallback"),
    )

    visible = load_raw_record_versions(
        connection, "daily", cutoff="2026-08-14T15:05:00+08:00",
    )

    assert [(row["source_code"], row["content_hash"]) for row in visible or []] == [
        ("tencent_market_snapshot", "fallback"),
    ]


def test_raw_loader_period_limit_is_per_code_and_keeps_accepted_pit_versions():
    """Bound wide financial reads without weakening the immutable contract."""

    connection = _connection_with_accepted_projections()
    cutoff = "2026-08-14T15:10:00+08:00"

    def version(
        record_id: int,
        code: str,
        period: str,
        content_hash: str,
        observed: str,
        *,
        record_key: str | None = None,
    ) -> tuple:
        key = record_key or f"income:{code}:{period}"
        payload = json.dumps({"ts_code": code, "period": period, "n_income": record_id})
        return (
            record_id, "income", key, code, "", period, "", payload,
            "tushare", observed, observed, observed,
            "tushare:direct_fetch", content_hash, observed,
        )

    rows = [
        version(1, "000001.SZ", "20230101", "a-2023", "2026-08-14T15:01:00+08:00"),
        version(2, "000001.SZ", "20240101", "a-2024", "2026-08-14T15:02:00+08:00"),
        version(3, "000001.SZ", "20250101", "a-2025-old", "2026-08-14T15:03:00+08:00", record_key="income:000001.SZ:20250101"),
        version(4, "000001.SZ", "20250101", "a-2025", "2026-08-14T15:04:00+08:00", record_key="income:000001.SZ:20250101"),
        version(5, "000001.SZ", "20260101", "a-2026", "2026-08-14T16:00:00+08:00"),
        version(6, "000002.SZ", "20240101", "b-2024", "2026-08-14T15:01:00+08:00"),
        version(7, "000002.SZ", "20250101", "b-2025", "2026-08-14T15:02:00+08:00"),
        version(8, "000002.SZ", "20230101", "b-2023", "2026-08-14T15:03:00+08:00"),
    ]
    connection.executemany(
        """INSERT INTO dc_tushare_raw_record_version
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    connection.executemany(
        """INSERT INTO dc_tushare_raw_records(api_name,record_key,content_hash)
           VALUES(?,?,?)""",
        [
            ("income", row[2], row[13])
            for row in rows
            if row[13] != "a-2025-old"
        ],
    )

    visible = load_raw_record_versions(
        connection, "income", cutoff=cutoff, period_limit=2,
    )

    assert [
        (row["ts_code"], row["period"], row["content_hash"])
        for row in visible or []
    ] == [
        ("000001.SZ", "20250101", "a-2025"),
        ("000001.SZ", "20240101", "a-2024"),
        ("000002.SZ", "20250101", "b-2025"),
        ("000002.SZ", "20240101", "b-2024"),
    ]


def test_raw_loader_blocks_when_accepted_version_is_after_cutoff():
    connection = _connection_with_accepted_projections()
    late = "2026-08-14T15:02:00+08:00"
    connection.execute(
        """INSERT INTO dc_tushare_raw_record_version
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (1, "daily", "daily:000001.SZ:20260814", "000001.SZ", "20260814", "", "",
         json.dumps({"close": 11}), "tushare", late, late, late,
         "tushare:direct_fetch", "primary", late),
    )
    connection.execute(
        "INSERT INTO dc_tushare_raw_records(api_name,record_key,content_hash) VALUES(?,?,?)",
        ("daily", "daily:000001.SZ:20260814", "primary"),
    )

    assert load_raw_record_versions(
        connection, "daily", cutoff="2026-08-14T15:01:00+08:00",
    ) == []


def test_current_projection_loader_requires_its_immutable_version():
    connection = _connection_with_accepted_projections()
    connection.execute("ALTER TABLE dc_tushare_raw_records ADD COLUMN trade_date TEXT NOT NULL DEFAULT ''")
    observed = "2026-08-14T15:02:00+08:00"
    connection.execute(
        """INSERT INTO dc_tushare_raw_record_version
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (1, "daily", "daily:000001.SZ:20260814", "000001.SZ", "20260814", "", "",
         json.dumps({"ts_code": "000001.SZ", "close": 10}), "tencent_market_snapshot",
         observed, observed, observed, "tencent_market_snapshot:validated", "accepted", observed),
    )
    connection.execute(
        """INSERT INTO dc_tushare_raw_records(api_name,record_key,content_hash,trade_date)
           VALUES(?,?,?,?)""",
        ("daily", "daily:000001.SZ:20260814", "accepted", "20260814"),
    )

    rows = load_current_raw_record_versions(connection, "daily", "20260814")

    assert [(row["ts_code"], row["content_hash"]) for row in rows or []] == [
        ("000001.SZ", "accepted"),
    ]


def test_postgres_current_projection_materializes_both_exact_date_sides(monkeypatch):
    captured = {}

    class Result:
        def fetchall(self):
            return []

    class Connection:
        _is_postgres = True

        def execute(self, sql, params=()):
            captured["sql"] = sql
            captured["params"] = params
            return Result()

    projection_columns = {"api_name", "record_key", "trade_date", "content_hash"}
    version_columns = {
        "api_name", "record_key", "ts_code", "trade_date", "period", "ann_date",
        "payload_json", "source_code", "economic_available_time", "observed_at",
        "first_observed_at", "observation_provenance", "content_hash", "created_at", "id",
    }
    monkeypatch.setattr(
        observation_clock,
        "_table_columns",
        lambda _connection, table: projection_columns
        if table == "dc_tushare_raw_records"
        else version_columns,
    )

    assert load_current_raw_record_versions(Connection(), "adj_factor", "20260825") == []
    assert "versions AS MATERIALIZED" in captured["sql"]
    assert captured["params"] == ("adj_factor", "20260825", "adj_factor", "20260825")


def test_raw_loader_does_not_move_an_accepted_hash_between_trade_dates():
    """The accepted projection's session is part of immutable PIT identity."""

    connection = _connection_with_accepted_projections()
    connection.execute(
        "ALTER TABLE dc_tushare_raw_records ADD COLUMN trade_date TEXT NOT NULL DEFAULT ''"
    )
    observed = "2026-08-14T15:02:00+08:00"
    connection.execute(
        """INSERT INTO dc_tushare_raw_record_version
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            1, "daily", "daily:000001.SZ:20260813", "000001.SZ", "20260813", "", "",
            json.dumps({"ts_code": "000001.SZ", "close": 10}), "tencent_market_snapshot",
            observed, observed, observed, "tencent_market_snapshot:validated", "accepted", observed,
        ),
    )
    connection.execute(
        """INSERT INTO dc_tushare_raw_records(api_name,record_key,content_hash,trade_date)
           VALUES(?,?,?,?)""",
        ("daily", "daily:000001.SZ:20260813", "accepted", "20260814"),
    )

    assert load_raw_record_versions(
        connection, "daily", cutoff="2026-08-14T15:05:00+08:00",
    ) == []


def test_snapshot_loader_keeps_accepted_projection_and_blocks_late_acceptance():
    connection = _connection_with_accepted_projections()
    early = "2026-08-14T15:01:00+08:00"
    late = "2026-08-14T15:02:00+08:00"
    connection.executemany(
        """INSERT INTO dc_stock_daily_snapshot_version
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (1, "000001.SZ", "20260814", json.dumps({"close": 10}),
             "tencent_market_snapshot", early, early, early,
             "snapshot:raw_dependency_observation_v1", "fallback", early),
            (2, "000001.SZ", "20260814", json.dumps({"close": 11}),
             "tushare", late, late, late,
             "snapshot:raw_dependency_observation_v1", "primary", late),
        ],
    )
    connection.execute(
        "INSERT INTO dc_stock_daily_snapshot(ts_code,trade_date,content_hash) VALUES(?,?,?)",
        ("000001.SZ", "20260814", "fallback"),
    )
    visible = load_snapshot_versions(
        connection, cutoff="2026-08-14T15:05:00+08:00",
    )
    assert [(row["source_code"], row["content_hash"]) for row in visible or []] == [
        ("tencent_market_snapshot", "fallback"),
    ]

    connection.execute(
        "UPDATE dc_stock_daily_snapshot SET content_hash=? WHERE ts_code=? AND trade_date=?",
        ("primary", "000001.SZ", "20260814"),
    )
    assert load_snapshot_versions(
        connection, cutoff="2026-08-14T15:01:00+08:00",
    ) == []


def test_market_aggregate_uses_same_projection_selected_snapshot_version():
    connection = _connection_with_accepted_projections()
    early = "2026-08-14T14:59:00+08:00"
    late = "2026-08-14T14:59:30+08:00"
    connection.executemany(
        """INSERT INTO dc_stock_daily_snapshot_version
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                1, "000001.SZ", "20260814",
                json.dumps({"change_pct": 1.0, "turnover": 100.0}),
                "tencent_market_snapshot", early, early, early,
                "snapshot:raw_dependency_observation_v1", "fallback", early,
            ),
            (
                2, "000001.SZ", "20260814",
                json.dumps({"change_pct": -9.9, "turnover": 9999.0}),
                "tushare", late, late, late,
                "snapshot:raw_dependency_observation_v1", "primary", late,
            ),
        ],
    )
    connection.execute(
        "INSERT INTO dc_stock_daily_snapshot(ts_code,trade_date,content_hash) VALUES(?,?,?)",
        ("000001.SZ", "20260814", "fallback"),
    )

    result = load_snapshot_market_aggregates(
        connection, trade_dates=["2026-08-14"],
    )

    assert result == {
        "20260814": {
            "trade_date": "20260814",
            "rows_total": 1,
            "market_rows": 1,
            "breadth": 1.0,
            "average_return": 0.01,
            "amount": 100.0,
            "limit_up_ratio": 0.0,
            "limit_down_ratio": 0.0,
            "top_5pct_turnover_concentration": 1.0,
        },
    }


def test_upsert_raw_record_does_not_replace_projection_when_gap_repair_races():
    connection = _connection()
    connection.executescript(
        """
        CREATE TABLE dc_tushare_raw_records(
            api_name TEXT NOT NULL,
            record_key TEXT NOT NULL,
            ts_code TEXT NOT NULL DEFAULT '',
            trade_date TEXT NOT NULL DEFAULT '',
            period TEXT NOT NULL DEFAULT '',
            ann_date TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            source_code TEXT NOT NULL DEFAULT '',
            available_time TEXT NOT NULL DEFAULT '',
            economic_available_time TEXT NOT NULL DEFAULT '',
            observed_at TEXT NOT NULL DEFAULT '',
            first_observed_at TEXT NOT NULL DEFAULT '',
            observation_provenance TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(api_name, record_key)
        );
        CREATE UNIQUE INDEX raw_record_version_hash
            ON dc_tushare_raw_record_version(api_name, record_key, content_hash);
        """
    )
    accepted = {
        "ts_code": "000001.SZ", "trade_date": "20260814", "close": 10,
    }
    repair = {
        "ts_code": "000001.SZ", "trade_date": "20260814", "close": 11,
    }
    first = upsert_raw_record(
        connection,
        api_name="daily",
        record_key="daily:000001.SZ:20260814",
        ts_code="000001.SZ",
        trade_date="20260814",
        payload=accepted,
        source_code="tencent_market_snapshot",
        observed_at="2026-08-14T15:01:00+08:00",
        economic_time="2026-08-14T15:00:00+08:00",
        provenance="tencent_market_snapshot:validated",
    )
    second = upsert_raw_record(
        connection,
        api_name="daily",
        record_key="daily:000001.SZ:20260814",
        ts_code="000001.SZ",
        trade_date="20260814",
        payload=repair,
        source_code="tushare",
        observed_at="2026-08-14T15:02:00+08:00",
        economic_time="2026-08-14T15:00:00+08:00",
        provenance="tushare:direct_fetch",
    )

    assert first == 1
    assert second == 0
    current = connection.execute(
        "SELECT source_code,payload_json FROM dc_tushare_raw_records"
    ).fetchone()
    assert current["source_code"] == "tencent_market_snapshot"
    assert json.loads(current["payload_json"])["close"] == 10
    versions = connection.execute(
        "SELECT source_code FROM dc_tushare_raw_record_version ORDER BY id"
    ).fetchall()
    assert [row["source_code"] for row in versions] == [
        "tencent_market_snapshot", "tushare",
    ]
