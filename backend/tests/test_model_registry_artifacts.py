from __future__ import annotations

from pathlib import Path

from app.services import model_registry


def test_model_status_demotes_active_version_with_missing_artifact(monkeypatch):
    active = {
        "id": 7,
        "model_key": "main",
        "version": "v-missing",
        "status": "active",
        "artifact_path": str(Path("/tmp/alis-missing-model-artifact.json")),
        "is_active": True,
    }
    calls: list[tuple[int, str, bool]] = []
    active_calls = {"count": 0}

    def fake_active(model_key="main"):
        active_calls["count"] += 1
        return active if active_calls["count"] == 1 else None

    monkeypatch.setattr(model_registry, "get_active_model_version", fake_active)
    monkeypatch.setattr(model_registry, "get_latest_model_train_run", lambda: None)
    monkeypatch.setattr(model_registry, "list_model_versions", lambda model_key, limit=10: [])
    monkeypatch.setattr(
        model_registry,
        "update_model_version_status",
        lambda model_version_id, *, status=None, is_active=None: calls.append((model_version_id, status, is_active)),
    )

    status = model_registry.get_model_status()

    assert calls == [(7, "artifact_missing", False)]
    assert status["active_version"] is None
    assert status["has_active_model"] is False
    assert status["invalid_active_version"]["status"] == "artifact_missing"
    assert status["invalid_active_version"]["is_active"] is False


def test_model_artifact_status_accepts_existing_file(tmp_path):
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}", encoding="utf-8")

    status = model_registry.model_artifact_status({"artifact_path": str(artifact)})

    assert status["ok"] is True
    assert status["status"] == "ready"
