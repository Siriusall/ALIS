from __future__ import annotations

from pathlib import Path


MODEL_CACHE_ROOT = Path(__file__).resolve().parents[2] / ".cache" / "models"


def resolve_model_artifact_path(
    path: str | Path | None,
    *,
    cache_root: Path | None = None,
) -> Path:
    """Resolve immutable model artifacts after a workspace directory move."""
    original = Path(str(path or ""))
    if str(path or "") and original.is_file():
        return original
    parts = original.parts
    try:
        cache_index = parts.index(".cache")
    except ValueError:
        return original
    suffix = parts[cache_index + 1:]
    if not suffix or suffix[0] != "models":
        return original
    candidate = (cache_root or MODEL_CACHE_ROOT).joinpath(*suffix[1:])
    return candidate if candidate.is_file() else original


def model_artifact_exists(path: str | Path | None) -> bool:
    return resolve_model_artifact_path(path).is_file()
