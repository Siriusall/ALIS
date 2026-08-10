"""Pytest bootstrap: make test resources isolated before importing app modules."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[2]

# Keep collection independent of the directory from which pytest is invoked.
sys.path.insert(0, str(ROOT / "backend"))


def _database_url_from_project_env(key: str, fallback: str) -> str:
    value = str(os.getenv(key, "")).strip()
    if value:
        return value
    values = dotenv_values(ROOT / ".env")
    return str(values.get(key) or fallback).strip()


def _replace_database_name(url: str, database: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"postgresql", "postgres", "postgresql+psycopg"}:
        raise RuntimeError("pytest requires PostgreSQL URLs for isolated test databases")
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database}", parsed.query, parsed.fragment))


# This module is imported by pytest before test modules. Do not let the
# project's production .env reach application imports during collection.
os.environ["APP_ENV"] = "test"
os.environ["ALIS_TEST_MODE"] = "1"
os.environ["ALIS_BACKGROUND_RUNTIME"] = "disabled"
os.environ["DATABASE_URL"] = _replace_database_name(
    _database_url_from_project_env("DATABASE_URL", "postgresql://sirius@127.0.0.1:5432/alis_sim"),
    "alis_sim_test",
)
os.environ["DATA_PLATFORM_DATABASE_URL"] = _replace_database_name(
    _database_url_from_project_env("DATA_PLATFORM_DATABASE_URL", "postgresql://sirius@127.0.0.1:5432/alis_data"),
    "alis_data_test",
)
os.environ["REDIS_URL"] = "redis://127.0.0.1:6379/15"
os.environ["ALIS_REDIS_NAMESPACE"] = "alis:test:pytest:"
os.environ["ALIS_TASK_STREAM"] = "alis:test:data-platform:tasks"
os.environ["ALIS_TASK_GROUP"] = "alis:test:data-platform-workers"
os.environ["ALIS_TASK_DEAD_LETTER"] = "alis:test:data-platform:tasks:dead-letter"
os.environ["ALIS_CACHE_ROOT"] = tempfile.mkdtemp(prefix="alis-pytest-", dir="/tmp")
os.environ["ALIS_COLD_ARCHIVE_ROOT"] = str(Path(os.environ["ALIS_CACHE_ROOT"]) / "cold-archive")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: requires an explicitly enabled external integration")


@pytest.fixture(scope="session", autouse=True)
def isolated_database_schema():
    """Create only the two isolated test schemas used by the suite."""
    from app.db.store import init_db
    from app.data_platform.db import init_db as init_data_platform_db
    from app.services.corporate_actions import _ensure_table as ensure_corporate_action_table
    from app.services.simulation_ledger_audit import _ensure_table as ensure_ledger_audit_table

    init_db()
    init_data_platform_db()
    ensure_corporate_action_table()
    ensure_ledger_audit_table()
    yield
