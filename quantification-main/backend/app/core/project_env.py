from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT: Path | None = None
_ENV_LOADED = False


def get_project_root() -> Path:
    """Return repository root (parent of backend/)."""

    global _PROJECT_ROOT
    if _PROJECT_ROOT is not None:
        return _PROJECT_ROOT

    # backend/app/core/project_env.py -> backend/app/core -> backend/app -> backend -> root
    _PROJECT_ROOT = Path(__file__).resolve().parents[3]
    return _PROJECT_ROOT


def get_env_file_paths() -> list[Path]:
    """Preferred .env locations: project root first, then backend/.env."""

    root = get_project_root()
    backend_dir = root / "backend"
    paths: list[Path] = []
    for candidate in (root / ".env", backend_dir / ".env"):
        if candidate.exists():
            paths.append(candidate)
    return paths


def get_primary_env_path() -> Path:
    """Return the first existing .env path, else default to project root."""

    paths = get_env_file_paths()
    if paths:
        return paths[0]
    return get_project_root() / ".env"


def load_project_env(*, override: bool = False) -> list[Path]:
    """Load .env from project root and backend/.env once per process."""

    global _ENV_LOADED
    loaded: list[Path] = []
    for env_path in get_env_file_paths():
        load_dotenv(env_path, override=override)
        loaded.append(env_path)

    if not loaded and not _ENV_LOADED:
        # Keep python-dotenv default behavior for cwd when no file exists yet.
        load_dotenv(override=override)

    _ENV_LOADED = True
    return loaded


def get_env_value(name: str, default: str = "") -> str:
    """Read an environment variable after loading project .env files."""

    load_project_env()
    return os.getenv(name, default).strip()
