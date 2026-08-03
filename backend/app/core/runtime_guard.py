"""Runtime resource boundaries for production and isolated test processes."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse


PRODUCTION_DATABASE_NAMES = {"alis_sim", "alis_data"}
TEST_DATABASE_SUFFIXES = ("_test", "_tests")


def database_name(url: str | None) -> str:
    parsed = urlparse(str(url or ""))
    return str(parsed.path or "").lstrip("/").split("?", 1)[0].lower()


def is_test_environment() -> bool:
    return (
        str(os.getenv("APP_ENV", "")).strip().lower() in {"test", "testing"}
        or str(os.getenv("ALIS_TEST_MODE", "")).strip().lower() in {"1", "true", "yes", "on"}
    )


def is_production_environment() -> bool:
    return str(os.getenv("APP_ENV", "")).strip().lower() in {"prod", "production"}


def assert_database_boundary(url: str, *, role: str) -> None:
    """Reject cross-environment database use before opening a connection."""
    name = database_name(url)
    if not name:
        raise RuntimeError(f"{role} database URL must include a database name")

    is_test_db = name.endswith(TEST_DATABASE_SUFFIXES) or name.startswith("test_")
    if is_test_environment():
        if str(os.getenv("ALIS_TEST_MODE", "")).strip().lower() not in {"1", "true", "yes", "on"}:
            raise RuntimeError("test environment requires ALIS_TEST_MODE=1")
        if name in PRODUCTION_DATABASE_NAMES or not is_test_db:
            raise RuntimeError(
                f"test process refused non-test {role} database: {name}"
            )
    elif is_test_db:
        raise RuntimeError(
            f"non-test process refused test {role} database: {name}"
        )


def assert_data_platform_path_boundary(path: Path, *, default_path: Path) -> None:
    """Keep SQLite overrides limited to isolated test processes."""
    if path == default_path:
        return
    if is_test_environment():
        return
    raise RuntimeError(
        "non-test data platform cannot override DATA_PLATFORM_DB_PATH"
    )


def cache_root(default_root: Path) -> Path:
    """Resolve the process cache root without changing the production default."""
    configured = str(os.getenv("ALIS_CACHE_ROOT", "")).strip()
    return Path(configured).expanduser() if configured else default_root
