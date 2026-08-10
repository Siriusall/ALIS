import hashlib
import json
import pickle

import pytest

from app.services import model_signal_fusion as fusion


class FoldModel:
    def predict(self, values):
        return [0.5 for _ in values]


def _binding(tmp_path, *, model_bytes=None):
    model_path = tmp_path / "model.pkl"
    if model_bytes is None:
        with model_path.open("wb") as handle:
            pickle.dump(FoldModel(), handle)
    else:
        model_path.write_bytes(model_bytes)
    digest = hashlib.sha256(model_path.read_bytes()).hexdigest()
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(json.dumps({
        "binding_contract": "point_in_time_fold_model_v1",
        "model_key": "medium_horizon_lambdarank_v1",
        "fold": 1,
        "model_path": str(model_path),
        "model_sha256": digest,
        "training_dataset": {
            "validation_end": "20200131",
            "test_start": "20200201",
            "test_end": "20200630",
        },
    }), encoding="utf-8")
    return {
        "id": 9,
        "point_in_time_fold_artifact": {
            "binding_contract": "point_in_time_fold_model_v1",
            "fold": 1,
            "artifact_path": str(artifact_path),
            "model_sha256": digest,
            "available_at": "2020-01-31T15:00:00+08:00",
            "validation_end": "20200131",
            "test_start": "20200201",
            "test_end": "20200630",
        },
    }, model_path


def test_resolve_model_versions_uses_verified_point_in_time_fold(monkeypatch, tmp_path):
    binding, _ = _binding(tmp_path)
    monkeypatch.setattr(fusion, "get_model_version", lambda _version_id: {
        "id": 9,
        "model_key": "medium_horizon_lambdarank_v1",
        "version": "candidate",
        "artifact_path": "/must/not/be/used.json",
        "created_at": "2026-07-28T20:00:00+08:00",
    })

    versions = fusion._resolve_model_versions({
        "medium_horizon_lambdarank_v1": binding,
    })

    resolved = versions["medium_horizon_lambdarank_v1"]
    assert resolved["artifact_path"] == binding["point_in_time_fold_artifact"]["artifact_path"]
    assert resolved["created_at"] == "2020-01-31T15:00:00+08:00"


def test_resolve_model_versions_rejects_tampered_fold_model(monkeypatch, tmp_path):
    binding, model_path = _binding(tmp_path)
    model_path.write_bytes(b"tampered")
    monkeypatch.setattr(fusion, "get_model_version", lambda _version_id: {
        "id": 9,
        "model_key": "medium_horizon_lambdarank_v1",
    })

    with pytest.raises(ValueError, match="身份或哈希不匹配"):
        fusion._resolve_model_versions({
            "medium_horizon_lambdarank_v1": binding,
        })


def test_resolve_model_versions_rejects_fold_window_mismatch(monkeypatch, tmp_path):
    binding, _ = _binding(tmp_path)
    binding["point_in_time_fold_artifact"]["test_start"] = "20200301"
    monkeypatch.setattr(fusion, "get_model_version", lambda _version_id: {
        "id": 9,
        "model_key": "medium_horizon_lambdarank_v1",
    })

    with pytest.raises(ValueError, match="时点合同不匹配"):
        fusion._resolve_model_versions({
            "medium_horizon_lambdarank_v1": binding,
        })
