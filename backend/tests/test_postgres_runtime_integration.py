"""Opt-in checks against the configured PostgreSQL runtime.

Run with RUN_POSTGRES_INTEGRATION=1 after configuring the project .env.  The
tests are read-only except for the schema reconciliation performed by init_db.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient


if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
    pytest.skip("set RUN_POSTGRES_INTEGRATION=1 to validate the configured PostgreSQL runtime", allow_module_level=True)


def test_primary_postgres_schema_is_ready() -> None:
    from app.db.store import _conn, database_health, init_db

    init_db()
    health = database_health()
    assert health == {"status": "ok", "engine": "postgresql"}
    with _conn() as conn:
        columns = {
            row["column_name"]
            for row in conn.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='positions'"""
            ).fetchall()
        }
    assert {"id", "symbol", "name", "bought_at", "quantity", "price"} <= columns


def test_data_platform_postgres_schema_is_ready() -> None:
    from app.data_platform.db import _conn, database_health, init_db

    init_db()
    health = database_health()
    assert health == {"status": "ok", "engine": "postgresql"}
    with _conn() as conn:
        tables = {
            row["table_name"]
            for row in conn.execute(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema='public'"""
            ).fetchall()
        }
    assert {"dc_stock_daily_snapshot", "dc_market_context_snapshot", "dc_runtime_logs"} <= tables


def test_business_routes_require_a_bearer_token(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "background_runtime_mode", "disabled")
    from app.main import app

    username = f"integration_{uuid.uuid4().hex[:12]}"
    with TestClient(app) as client:
        assert client.get("/api/account").status_code == 401
        registered = client.post(
            "/api/auth/register",
            json={"username": username, "password": "IntegrationPass_2026!"},
        )
        assert registered.status_code == 200
        token = registered.json()["access_token"]
        account = client.get("/api/account", headers={"Authorization": f"Bearer {token}"})
        assert account.status_code == 200
        assert account.json()["assets"] >= 0
