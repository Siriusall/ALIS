from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.runtime_guard import (
    assert_data_platform_path_boundary,
    assert_database_boundary,
    database_name,
)
from app.data_platform.redis_store import _assert_redis_boundary


def test_pytest_bootstrap_uses_isolated_resources() -> None:
    assert os.environ["APP_ENV"] == "test"
    assert os.environ["ALIS_TEST_MODE"] == "1"
    assert database_name(os.environ["DATABASE_URL"]) == "alis_sim_test"
    assert database_name(os.environ["DATA_PLATFORM_DATABASE_URL"]) == "alis_data_test"
    assert os.environ["REDIS_URL"].rsplit("/", 1)[-1] == "15"
    assert os.environ["ALIS_REDIS_NAMESPACE"].startswith("alis:test:")
    assert Path(os.environ["ALIS_CACHE_ROOT"]).is_dir()


def test_test_process_rejects_production_database(monkeypatch) -> None:
    monkeypatch.setenv("ALIS_TEST_MODE", "1")
    monkeypatch.setenv("APP_ENV", "test")
    production_url = os.environ["DATABASE_URL"].replace("alis_sim_test", "alis_sim")
    try:
        assert_database_boundary(production_url, role="business")
    except RuntimeError as exc:
        assert "refused" in str(exc)
    else:
        raise AssertionError("test process accepted production database")


def test_non_test_process_rejects_test_database_and_overrides(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.delenv("ALIS_TEST_MODE", raising=False)
    test_url = os.environ["DATABASE_URL"]
    with pytest.raises(RuntimeError, match="non-test process refused test"):
        assert_database_boundary(test_url, role="business")
    with pytest.raises(RuntimeError, match="non-production Redis"):
        _assert_redis_boundary("redis://127.0.0.1:6379/15")
    with pytest.raises(RuntimeError, match="cannot override"):
        assert_data_platform_path_boundary(tmp_path, default_path=tmp_path / "default")
