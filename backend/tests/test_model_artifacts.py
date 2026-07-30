from app.services.model_artifacts import resolve_model_artifact_path


def test_model_artifact_resolves_by_stable_cache_suffix_after_workspace_move(tmp_path):
    cache_root = tmp_path / "current" / ".cache" / "models"
    artifact = cache_root / "family" / "version-1" / "artifact.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    stale = "/removed/worktree/backend/.cache/models/family/version-1/artifact.json"

    resolved = resolve_model_artifact_path(stale, cache_root=cache_root)

    assert resolved == artifact


def test_model_artifact_does_not_guess_outside_model_cache(tmp_path):
    missing = tmp_path / "removed" / "artifact.json"

    resolved = resolve_model_artifact_path(missing, cache_root=tmp_path / "models")

    assert resolved == missing
