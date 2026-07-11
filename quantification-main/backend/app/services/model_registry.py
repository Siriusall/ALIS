from __future__ import annotations

from pathlib import Path
from typing import Any

from app.db.store import (
    get_active_model_version,
    get_latest_model_train_run,
    get_model_train_run,
    get_model_version,
    list_model_versions,
)


MODEL_KEY = "main"


def models_root() -> Path:
    root = Path(__file__).resolve().parents[2] / ".cache" / "models"
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_model_status() -> dict[str, Any]:
    active = get_active_model_version(MODEL_KEY)
    latest_run = get_latest_model_train_run()
    versions = list_model_versions(MODEL_KEY, limit=10)
    return {
        "model_key": MODEL_KEY,
        "active_version": active,
        "latest_train_run": latest_run,
        "versions": versions,
        "has_active_model": active is not None,
    }


def get_train_run_status(run_id: int) -> dict[str, Any] | None:
    return get_model_train_run(run_id)


def get_model_version_or_active(model_version_id: int | None = None) -> dict[str, Any] | None:
    if model_version_id:
        return get_model_version(int(model_version_id))
    return get_active_model_version(MODEL_KEY)
