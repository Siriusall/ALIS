import pytest
from app.db import store
from app.core.db_connection_gate import (
    DEFAULT_AUTH_DB_CONNECTIONS,
    DEFAULT_BUSINESS_DB_CONNECTIONS,
    DatabaseConnectionCapacity,
    DatabaseConnectionGate,
)
from app.api.routes import auth


def test_auth_connection_gate_exists():
    assert hasattr(store, "_AUTH_DB_CONNECTION_GATE")
    assert store._AUTH_DB_CONNECTION_GATE.limit == DEFAULT_AUTH_DB_CONNECTIONS


def test_auth_read_conn_is_autocommit_read_only():
    assert hasattr(store, "_auth_read_conn")


def test_auth_route_uses_auth_read_conn(monkeypatch):
    calls = []

    class DummyCursor:
        def fetchone(self):
            return {"id": 1, "username": "admin", "password_hash": "hash", "created_at": "2026-09-14"}

    class DummyConn:
        _is_postgres = True
        _autocommit_read_only = True

        def execute(self, sql, params=()):
            calls.append((sql, params))
            return DummyCursor()

    from contextlib import contextmanager

    @contextmanager
    def mock_auth_read_conn():
        yield DummyConn()

    monkeypatch.setattr(store, "_auth_read_conn", mock_auth_read_conn)
    user = store.get_user_by_id(1)
    assert user is not None
    assert user["id"] == 1
    assert any("FROM users WHERE id = ?" in sql for sql, _ in calls)


def test_auth_isolated_gate_does_not_consume_business_gate():
    """Exhausting the business connection gate must not block auth read connections."""
    business_gate = store._BUSINESS_DB_CONNECTION_GATE
    # Simulate business gate exhaustion
    slots_acquired = []
    try:
        for _ in range(business_gate.limit):
            ctx = business_gate.slot(0.1)
            ctx.__enter__()
            slots_acquired.append(ctx)

        # Business slot should now be exhausted
        with pytest.raises(DatabaseConnectionCapacity):
            with business_gate.slot(0.1):
                pass

        # Auth gate slot should still be obtainable
        with store._AUTH_DB_CONNECTION_GATE.slot(0.1):
            pass
    finally:
        for ctx in slots_acquired:
            ctx.__exit__(None, None, None)
