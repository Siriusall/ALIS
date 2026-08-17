from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3

from app.services import industry_event_engine as event_engine
from app.services import research_engine


def _version_connection(*, include_versions: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
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
            """
        )
    return connection


def _patch_connection(monkeypatch, connection: sqlite3.Connection) -> None:
    @contextmanager
    def fake_conn():
        yield connection

    monkeypatch.setattr(event_engine, "_conn", fake_conn)


def _raw_version(*, record_id: int, api_name: str, payload: dict, observed_at: str) -> tuple:
    return (
        record_id,
        api_name,
        f"{api_name}:000001.SZ:20260331",
        "000001.SZ",
        "",
        "20260331",
        "2026-08-14",
        json.dumps(payload),
        "tushare",
        observed_at,
        observed_at,
        observed_at,
        "tushare:direct_fetch",
        f"hash-{record_id}",
        observed_at,
    )


def test_formal_events_read_immutable_raw_versions_not_mutable_announcements(monkeypatch):
    connection = _version_connection()
    observed = "2026-08-14T14:58:00+08:00"
    connection.execute(
        """INSERT INTO dc_tushare_raw_record_version VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        _raw_version(
            record_id=1,
            api_name="forecast",
            payload={"ann_date": "20260814", "p_change_min": 25, "p_change_max": 35},
            observed_at=observed,
        ),
    )
    _patch_connection(monkeypatch, connection)

    result = event_engine.list_events(
        ts_code="000001.SZ",
        as_of_time="2026-08-14T15:00:00+08:00",
    )

    assert result["ok"] is True
    assert result["status"] == "ready"
    assert result["count"] == 1
    assert result["items"][0]["event_type"] == "earnings_forecast"
    assert result["items"][0]["available_time"] == observed
    assert result["items"][0]["evidence"]["content_hash"] == "hash-1"


def test_formal_events_block_when_immutable_version_schema_is_missing(monkeypatch):
    connection = _version_connection(include_versions=False)
    _patch_connection(monkeypatch, connection)

    result = event_engine.list_events(
        ts_code="000001.SZ",
        as_of_time="2026-08-14T15:00:00+08:00",
    )

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["reason"] == "immutable_event_schema_unavailable"
    assert result["items"] == []


def test_specialized_research_propagates_event_pit_block(monkeypatch):
    monkeypatch.setattr(
        research_engine,
        "build_expectation_state",
        lambda *_args, **_kwargs: {"ok": True, "dimension": {"components": {}}},
    )
    monkeypatch.setattr(
        research_engine,
        "build_valuation_state",
        lambda *_args, **_kwargs: {"ok": True, "dimension": {"components": {}}},
    )
    monkeypatch.setattr(
        event_engine,
        "list_industries",
        lambda **_kwargs: {"ok": True, "status": "ready", "items": []},
    )
    monkeypatch.setattr(
        event_engine,
        "list_events",
        lambda **_kwargs: {
            "ok": False,
            "status": "blocked",
            "reason": "immutable_event_schema_unavailable",
            "items": [],
        },
    )

    _inputs, state = research_engine._specialized_research_inputs(
        "000001.SZ",
        "制造业",
        as_of_time="2026-08-14T15:00:00+08:00",
    )

    assert state["status"] == "blocked"
    assert state["reason"] == "specialized_pit_dependency_unavailable"
    assert state["dependencies"] == [{
        "dependency": "events",
        "reason": "immutable_event_schema_unavailable",
        "status": "blocked",
    }]


def test_stock_master_clock_is_required_for_formal_research(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE dc_stock_master(
            ts_code TEXT PRIMARY KEY, name TEXT, industry TEXT, list_date TEXT,
            source_code TEXT, available_time TEXT, updated_at TEXT
        )"""
    )
    connection.execute(
        """INSERT INTO dc_stock_master VALUES(?,?,?,?,?,?,?)""",
        (
            "000001.SZ", "测试", "制造业", "20100101", "tushare",
            "2026-08-14T16:00:00+08:00", "2026-08-14T16:00:00+08:00",
        ),
    )
    @contextmanager
    def fake_conn():
        yield connection

    monkeypatch.setattr(research_engine, "_conn", fake_conn)
    row, status = research_engine._strict_master_row(
        connection,
        "000001.SZ",
        as_of_time="2026-08-14T15:00:00+08:00",
    )
    assert row is None
    assert status == "stock_master_not_visible_at_cutoff"
