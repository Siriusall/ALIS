from __future__ import annotations

from contextlib import contextmanager

import pytest
from psycopg.errors import ReadOnlySqlTransaction

from app.data_platform import db as data_db
from app.db import store
from app.data_platform.services import decision_data_contract as contract
from app.core.autonomy_step_control import (
    autonomy_step_scope,
    cancel_autonomy_scope,
)


class _Result:
    def __init__(self, *, one=None, many=None):
        self._one = one
        self._many = many or []

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many


class _RawConnection:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.cancelled = 0

    def execute(self, sql, params=()):  # noqa: ANN001
        self.calls.append((str(sql), tuple(params or ())))
        return _Result(one={"value": 1})

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True

    def cancel(self):
        self.cancelled += 1


class _Cursor:
    def __init__(self, raw: _RawConnection):
        self.raw = raw

    def execute(self, sql, params=()):  # noqa: ANN001
        self.raw.calls.append((str(sql), tuple(params or ())))
        return self

    def fetchone(self):
        return {"value": 1}

    def fetchall(self):
        return []


def _patch_postgres_connection(monkeypatch, module, raw: _RawConnection):  # noqa: ANN001
    opened: dict[str, object] = {}

    def connect(_factory, _url, **kwargs):  # noqa: ANN001
        opened.update(kwargs)
        return raw

    monkeypatch.setattr(module, "_is_postgres", lambda: True)
    monkeypatch.setattr(module, "connect_postgres", connect)
    monkeypatch.setattr(module, "assert_database_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.core.autonomy_step_control.register_postgres_connection",
        lambda _raw: "registration",
    )
    monkeypatch.setattr(
        "app.core.autonomy_step_control.unregister_postgres_connection",
        lambda _registration: None,
    )
    return opened


def _has_session_setting(raw: _RawConnection, name: str, value: str) -> bool:
    return any(
        name in sql and params == (value,)
        for sql, params in raw.calls
    )


def _raw_connection(conn):  # noqa: ANN001
    """Return the driver connection for either DB wrapper implementation."""

    raw = getattr(conn, "raw", None)
    return raw if raw is not None else conn._conn


def test_data_platform_read_connection_is_autocommit_and_server_read_only(monkeypatch):
    monkeypatch.delenv("DATABASE_IDLE_IN_TRANSACTION_TIMEOUT_MS", raising=False)
    raw = _RawConnection()
    opened = _patch_postgres_connection(monkeypatch, data_db, raw)

    with data_db._read_conn() as conn:
        assert conn._autocommit_read_only is True
        conn.execute("SELECT 1").fetchone()

    assert opened["autocommit"] is True
    assert raw.commits == 0
    assert raw.rollbacks == 0
    assert raw.closed is True
    assert _has_session_setting(raw, "idle_in_transaction_session_timeout", "15000")
    assert any("default_transaction_read_only" in sql for sql, _params in raw.calls)


def test_data_platform_default_connection_remains_a_write_transaction(monkeypatch):
    monkeypatch.delenv("DATABASE_IDLE_IN_TRANSACTION_TIMEOUT_MS", raising=False)
    raw = _RawConnection()
    opened = _patch_postgres_connection(monkeypatch, data_db, raw)

    with data_db._conn() as conn:
        assert conn._autocommit_read_only is False
        conn.execute("SELECT 1").fetchone()

    assert opened["autocommit"] is False
    assert raw.commits == 1
    assert raw.rollbacks == 0
    assert not any("default_transaction_read_only" in sql for sql, _params in raw.calls)


def test_business_read_connection_is_autocommit_and_default_write_path_commits(monkeypatch):
    monkeypatch.delenv("DATABASE_IDLE_IN_TRANSACTION_TIMEOUT_MS", raising=False)

    read_raw = _RawConnection()
    opened = _patch_postgres_connection(monkeypatch, store, read_raw)
    with store._read_conn() as conn:
        assert conn._autocommit_read_only is True
        conn.execute("SELECT 1").fetchone()
    assert opened["autocommit"] is True
    assert read_raw.commits == 0
    assert _has_session_setting(read_raw, "idle_in_transaction_session_timeout", "15000")
    assert any("default_transaction_read_only" in sql for sql, _params in read_raw.calls)

    write_raw = _RawConnection()
    opened = _patch_postgres_connection(monkeypatch, store, write_raw)
    with store._conn() as conn:
        assert conn._autocommit_read_only is False
        conn.execute("SELECT 1").fetchone()
    assert opened["autocommit"] is False
    assert write_raw.commits == 1


def test_owned_decision_contract_skips_savepoints_on_autocommit_read_connection(monkeypatch):
    class _ContractConnection:
        _autocommit_read_only = True

        def __init__(self):
            self.sql: list[str] = []

        def execute(self, sql, _params=()):  # noqa: ANN001
            text = str(sql)
            self.sql.append(text)
            if "SAVEPOINT" in text:
                raise AssertionError("autocommit decision contract must not use savepoints")
            if "FROM dc_stock_master" in text and "COUNT(*) AS count" in text:
                return _Result(one={"count": 0})
            if "GROUP BY raw.api_name,raw.source_code" in text:
                return _Result(many=[])
            if "GROUP BY raw.api_name" in text:
                return _Result(many=[])
            if "jsonb_each_text" in text:
                return _Result(many=[])
            if "WITH raw_values AS" in text:
                return _Result(one={})
            raise AssertionError(text)

    connection = _ContractConnection()

    @contextmanager
    def open_read_connection():
        yield connection

    monkeypatch.setattr(contract, "_read_conn", open_read_connection)

    result = contract.decision_data_contract(
        "20260724",
        as_of_time="2026-07-24T18:00:00+08:00",
    )

    assert result["ok"] is False
    assert not any("SAVEPOINT" in sql for sql in connection.sql)


@pytest.mark.parametrize(
    "open_connection",
    [data_db._read_conn, store._read_conn],
    ids=["data_platform", "business"],
)
def test_postgres_read_connection_enforces_read_only_for_each_autocommit_statement(open_connection):
    """Verify the real psycopg/PostgreSQL session semantics, not just kwargs."""

    with open_connection() as conn:
        assert _raw_connection(conn).autocommit is True
        settings = dict(conn.execute(
            """SELECT current_setting('default_transaction_read_only') AS default_read_only,
                      current_setting('transaction_read_only') AS transaction_read_only,
                      EXTRACT(EPOCH FROM current_setting(
                          'idle_in_transaction_session_timeout'
                      )::interval)::INTEGER AS idle_seconds"""
        ).fetchone())
        assert str(settings["default_read_only"]).lower() == "on"
        assert str(settings["transaction_read_only"]).lower() == "on"
        assert int(settings["idle_seconds"]) == 15

        # A temporary table is still a write.  Rejecting it proves the server
        # applies read-only semantics to the statement transaction itself.
        with pytest.raises(ReadOnlySqlTransaction):
            conn.execute("CREATE TEMP TABLE alis_read_only_contract_probe(id INTEGER)")

        # The rejected statement must not poison the next autocommit query.
        after_error = dict(conn.execute(
            "SELECT current_setting('transaction_read_only') AS transaction_read_only"
        ).fetchone())
        assert str(after_error["transaction_read_only"]).lower() == "on"


@pytest.mark.parametrize(
    "open_connection",
    [data_db._conn, store._conn],
    ids=["data_platform", "business"],
)
def test_postgres_default_connection_remains_a_writable_transaction(open_connection):
    with open_connection() as conn:
        assert _raw_connection(conn).autocommit is False
        value = dict(conn.execute(
            "SELECT current_setting('transaction_read_only') AS transaction_read_only"
        ).fetchone())
        assert str(value["transaction_read_only"]).lower() == "off"


def test_read_connection_keeps_autonomy_cancellation_registration(monkeypatch):
    raw = _RawConnection()
    monkeypatch.setattr(data_db, "_is_postgres", lambda: True)
    monkeypatch.setattr(data_db, "connect_postgres", lambda *_args, **_kwargs: raw)
    monkeypatch.setattr(data_db, "assert_database_boundary", lambda *_args, **_kwargs: None)

    with autonomy_step_scope() as scope_id:
        with data_db._read_conn():
            assert cancel_autonomy_scope(scope_id) == 1
            assert raw.cancelled == 1


def test_data_platform_read_connection_keeps_sqlite_runtime_disabled(monkeypatch):
    monkeypatch.setattr(data_db, "_is_postgres", lambda: False)

    with pytest.raises(RuntimeError, match="SQLite 已禁用"):
        with data_db._read_conn():
            pass
