"""Filesystem paths that must follow the active runtime environment."""

from __future__ import annotations

from pathlib import Path

from app.core.runtime_guard import cache_root


def backend_cache_root() -> Path:
    return cache_root(Path(__file__).resolve().parents[2] / ".cache")
