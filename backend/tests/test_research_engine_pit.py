from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3

from app.services import research_engine


def _connection(*, include_versions: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE dc_stock_master(
            ts_code TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            industry TEXT NOT NULL DEFAULT '',
            list_date TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE dc_stock_daily_snapshot(
            ts_code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            close REAL,
            pe_ttm REAL,
            pb REAL,
            total_mv REAL,
            available_time TEXT NOT NULL DEFAULT ''
        );
        """
    )
    if include_versions:
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


def _patch_connection(monkeypatch, connection: sqlite3.Connection) -> None:
    @contextmanager
    def fake_conn():
        yield connection

    monkeypatch.setattr(research_engine, "_conn", fake_conn)


def test_research_raw_reader_excludes_late_economic_clock_even_when_observed_early(monkeypatch):
    connection = _connection()
    _patch_connection(monkeypatch, connection)
    early = "2026-08-14T14:58:00+08:00"
    late_economic = "2026-08-14T16:00:00+08:00"
    payload_early = json.dumps({"period": "20260331", "n_income": 100})
    payload_late = json.dumps({"period": "20260331", "n_income": 999})
    connection.executemany(
        """INSERT INTO dc_tushare_raw_record_version
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (1, "income", "income:000001.SZ:20260331", "000001.SZ", "", "20260331", "", payload_early, "tushare", early, early, early, "tushare:direct_fetch", "early", early),
            # This record arrived before the cutoff but represents a value that
            # was economically available only after the close, so it is not
            # admissible PIT evidence.
            (2, "income", "income:000001.SZ:20260331", "000001.SZ", "", "20260331", "", payload_late, "tushare", late_economic, early, early, "tushare:direct_fetch", "late-economic", early),
        ],
    )

    rows = research_engine._payloads(
        "000001.SZ",
        "income",
        as_of_time="2026-08-14T15:00:00+08:00",
    )

    assert len(rows) == 1
    assert rows[0]["payload"]["n_income"] == 100
    assert rows[0]["economic_available_time"] == early


def test_research_snapshot_reader_excludes_late_economic_version(monkeypatch):
    connection = _connection()
    _patch_connection(monkeypatch, connection)
    early = "2026-08-14T14:58:00+08:00"
    late_economic = "2026-08-14T16:00:00+08:00"
    connection.executemany(
        """INSERT INTO dc_stock_daily_snapshot_version
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (1, "000001.SZ", "20260814", json.dumps({"close": 10}), "tushare", early, early, early, "snapshot:raw_dependency_observation_v1", "early", early),
            (2, "000001.SZ", "20260814", json.dumps({"close": 99}), "tushare", late_economic, early, early, "snapshot:raw_dependency_observation_v1", "late-economic", early),
        ],
    )

    row, status = research_engine._strict_snapshot_for_code(
        connection,
        "000001.SZ",
        as_of_time="2026-08-14T15:00:00+08:00",
        as_of_date="20260814",
    )

    assert status == "ready"
    assert row is not None
    assert row["close"] == 10


def test_formal_research_does_not_fall_back_to_mutable_snapshot_projection(monkeypatch):
    connection = _connection(include_versions=False)
    connection.execute(
        "INSERT INTO dc_stock_master(ts_code,name,industry) VALUES('000001.SZ','测试','制造业')"
    )
    connection.execute(
        """INSERT INTO dc_stock_daily_snapshot
           (ts_code,trade_date,close,pe_ttm,pb,total_mv,available_time)
           VALUES('000001.SZ','20260814',99,10,1,100000,'2026-08-14T14:59:00+08:00')"""
    )
    _patch_connection(monkeypatch, connection)

    result = research_engine.build_stock_research(
        "000001.SZ",
        persist=False,
        as_of_time="2026-08-14T15:00:00+08:00",
    )

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["reason"] == "immutable_snapshot_schema_unavailable"


def test_formal_valuation_does_not_fall_back_to_mutable_snapshot_projection(monkeypatch):
    connection = _connection(include_versions=False)
    connection.execute(
        "INSERT INTO dc_stock_master(ts_code,name,industry) VALUES('000001.SZ','测试','制造业')"
    )
    connection.execute(
        """INSERT INTO dc_stock_daily_snapshot
           (ts_code,trade_date,close,pe_ttm,pb,total_mv,available_time)
           VALUES('000001.SZ','20260814',99,10,1,100000,'2026-08-14T14:59:00+08:00')"""
    )
    _patch_connection(monkeypatch, connection)

    result = research_engine.build_valuation_state(
        "000001.SZ",
        persist=False,
        as_of_time="2026-08-14T15:00:00+08:00",
    )

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["reason"] == "immutable_snapshot_schema_unavailable"
