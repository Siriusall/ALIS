from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pandas as pd
import pytest

from app.data_platform.services import data_center, daily_update
from app.data_platform.services import market_data_discovery


class _SQLiteConnection:
    _is_mysql = False

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def execute(self, sql, params=()):
        return self.connection.execute(sql, params)


def _connection_context(connection: sqlite3.Connection):
    @contextmanager
    def _open():
        yield _SQLiteConnection(connection)
        connection.commit()

    return _open


def _calendar_connection(*rows):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE dc_trade_calendar(
            exchange TEXT NOT NULL,
            cal_date TEXT NOT NULL,
            is_open INTEGER NOT NULL,
            pretrade_date TEXT,
            source_code TEXT,
            available_time TEXT,
            updated_at TEXT,
            PRIMARY KEY(exchange, cal_date)
        )
        """
    )
    connection.executemany(
        "INSERT INTO dc_trade_calendar(exchange,cal_date,is_open,pretrade_date,source_code) VALUES(?,?,?,?,?)",
        rows,
    )
    return connection


def _patch_fallback_dependencies(monkeypatch, connection, discovery):
    monkeypatch.setattr(data_center, "_conn", _connection_context(connection))
    monkeypatch.setattr(data_center, "_now", lambda: "2026-08-18T16:10:00+08:00")
    monkeypatch.setattr(market_data_discovery, "discover_first_published", lambda *_args, **_kwargs: discovery)


def test_trade_calendar_fallback_adds_exact_published_session(monkeypatch):
    connection = _calendar_connection(("SSE", "20260817", 1, "20260814", "tushare"))
    _patch_fallback_dependencies(monkeypatch, connection, {
        "ok": True,
        "winner": "tencent_market_snapshot",
        "status": "available",
    })

    result = data_center._fallback_trade_calendar_from_market_observation(
        start_date="20260714",
        end_date="20260825",
        exchange="SSE",
        upstream_error=RuntimeError("您的token不对，请确认。"),
    )

    assert result["ok"] is True
    assert result["status"] == "fallback_market_observation"
    assert result["source_code"] == "market_observation:tencent_market_snapshot"
    row = connection.execute(
        "SELECT is_open,pretrade_date,source_code FROM dc_trade_calendar WHERE cal_date='20260818'"
    ).fetchone()
    assert dict(row) == {
        "is_open": 1,
        "pretrade_date": "20260817",
        "source_code": "market_observation:tencent_market_snapshot",
    }


def test_trade_calendar_fallback_reuses_existing_open_row_without_overwrite(monkeypatch):
    connection = _calendar_connection(
        ("SSE", "20260817", 1, "20260814", "tushare"),
        ("SSE", "20260818", 1, "20260817", "manual_audit"),
    )
    _patch_fallback_dependencies(monkeypatch, connection, {
        "ok": True,
        "winner": "tencent_market_snapshot",
        "status": "available",
    })

    result = data_center._fallback_trade_calendar_from_market_observation(
        start_date="20260714", end_date="20260825", exchange="SSE",
        upstream_error=RuntimeError("token invalid"),
    )

    assert result["ok"] is True
    assert result["status"] == "existing_calendar_row_after_tushare_failure"
    assert result["rows_written"] == 0
    row = connection.execute(
        "SELECT source_code FROM dc_trade_calendar WHERE cal_date='20260818'"
    ).fetchone()
    assert row[0] == "manual_audit"


def test_trade_calendar_fallback_fails_closed_on_holiday_conflict(monkeypatch):
    connection = _calendar_connection(
        ("SSE", "20260817", 1, "20260814", "tushare"),
        ("SSE", "20260818", 0, "20260817", "exchange_calendar"),
    )
    _patch_fallback_dependencies(monkeypatch, connection, {
        "ok": True,
        "winner": "tencent_market_snapshot",
        "status": "available",
    })

    result = data_center._fallback_trade_calendar_from_market_observation(
        start_date="20260714", end_date="20260825", exchange="SSE",
        upstream_error=RuntimeError("token invalid"),
    )

    assert result["ok"] is False
    assert result["status"] == "calendar_provider_conflict"
    assert result["existing_is_open"] == 0


def test_trade_calendar_fallback_reports_probe_failure(monkeypatch):
    connection = _calendar_connection(("SSE", "20260817", 1, "20260814", "tushare"))
    _patch_fallback_dependencies(monkeypatch, connection, {
        "ok": False,
        "status": "provider_failed",
        "failure_category": "transient",
    })

    result = data_center._fallback_trade_calendar_from_market_observation(
        start_date="20260714", end_date="20260825", exchange="SSE",
        upstream_error=RuntimeError("token invalid"),
    )

    assert result["ok"] is False
    assert result["status"] == "fallback_market_not_published"


def test_historical_calendar_sync_does_not_enable_current_day_fallback_by_default(monkeypatch):
    connection = _calendar_connection(("SSE", "20260817", 1, "20260814", "tushare"))
    monkeypatch.setattr(data_center, "ensure_data_center_seeded", lambda: None)
    monkeypatch.setattr(data_center, "_conn", _connection_context(connection))
    monkeypatch.setattr(data_center, "start_ingestion_run", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(data_center, "finish_ingestion_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(data_center, "_record_probe", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(data_center, "get_tushare_client", lambda: (_ for _ in ()).throw(RuntimeError("token invalid")))
    monkeypatch.setattr(data_center, "_now", lambda: "2026-08-18T16:10:00+08:00")
    monkeypatch.setattr(
        market_data_discovery,
        "discover_first_published",
        lambda *_args, **_kwargs: pytest.fail("historical sync must not probe today's market fallback"),
    )

    with pytest.raises(RuntimeError, match="token invalid"):
        data_center.sync_trade_calendar("20260714", "20260825")


def test_trade_calendar_sync_records_concrete_proxy_provider(monkeypatch):
    captured: dict[str, object] = {}

    class _Pro:
        @staticmethod
        def trade_cal(**_kwargs):
            return pd.DataFrame([{
                "exchange": "SSE",
                "cal_date": "20260825",
                "is_open": 1,
                "pretrade_date": "20260824",
            }])

    class _Client:
        pro = _Pro()

    monkeypatch.setattr(data_center, "ensure_data_center_seeded", lambda: None)
    monkeypatch.setattr(data_center, "primary_tushare_provider_code", lambda: "tushare_proxy")
    monkeypatch.setattr(data_center, "get_tushare_client", lambda: _Client())
    monkeypatch.setattr(data_center, "start_ingestion_run", lambda name, meta, **kwargs: captured.update(name=name, meta=meta, kwargs=kwargs) or 7)
    monkeypatch.setattr(data_center, "finish_ingestion_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(data_center, "_record_probe", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(data_center, "_upsert_trade_calendar", lambda _frame, **kwargs: captured.update(upsert=kwargs) or 1)

    result = data_center.sync_trade_calendar("20260825", "20260825")

    assert result["ok"] is True
    assert captured["name"] == "tushare_proxy.trade_cal"
    assert captured["kwargs"] == {"source_code": "tushare_proxy"}
    assert captured["upsert"] == {"source_code": "tushare_proxy"}


def test_daily_update_calendar_auth_failure_is_bounded(monkeypatch):
    class _Result:
        def fetchone(self):
            return {"cnt": 100}

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, _params=()):
            if "SELECT COUNT(*) cnt FROM dc_stock_master" in sql:
                return _Result()
            return _Result()

    state = {}
    monkeypatch.setattr(daily_update, "_conn", lambda: _Connection())
    monkeypatch.setattr(daily_update, "_state", state)
    monkeypatch.setattr(daily_update, "ensure_data_center_seeded", lambda: None)
    monkeypatch.setattr(
        daily_update,
        "sync_trade_calendar",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("您的token不对，请确认。")),
    )

    daily_update._worker(123)

    assert state["phase"] == "failed"
    assert state["failure_category"] == "authentication"
    assert state["retry_after_seconds"] == 86400


def test_daily_update_statement_timeout_is_bounded(monkeypatch):
    class _Result:
        def fetchone(self):
            return {"cnt": 100}

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _sql, _params=()):
            return _Result()

    state = {}
    monkeypatch.setattr(daily_update, "_conn", lambda: _Connection())
    monkeypatch.setattr(daily_update, "_state", state)
    monkeypatch.setattr(daily_update, "ensure_data_center_seeded", lambda: None)
    monkeypatch.setattr(daily_update, "sync_trade_calendar", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(
        daily_update,
        "_latest_trade_date",
        lambda: (_ for _ in ()).throw(RuntimeError("canceling statement due to statement timeout")),
    )

    daily_update._worker(124)

    assert state["phase"] == "failed"
    assert state["failure_category"] == "transient"
    assert state["retry_after_seconds"] == 600
