from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api.routes import model_strategy
from app.services.model_training import _model_selection_rank, _production_eligibility


def test_training_candidate_is_not_production_eligible_without_six_year_window(tmp_path):
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}", encoding="utf-8")

    result = _production_eligibility(
        dates=[f"2026-01-{day:02d}" for day in range(1, 21)],
        metrics={"lift": 0.1, "walk_forward_lift": 0.1, "walk_forward_folds": 3},
        artifact_path=artifact,
    )

    assert result["eligible"] is False
    assert "minimum_history_sessions" in result["blocked_reasons"]
    assert result["gates"]["artifact_exists"] is True


def test_training_candidate_is_history_eligible_with_1512_streamed_calendar_sessions(tmp_path):
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}", encoding="utf-8")
    result = _production_eligibility(
        dates=[f"session-{index}" for index in range(1512)],
        metrics={"lift": 0.1, "walk_forward_lift": 0.1, "walk_forward_folds": 3},
        artifact_path=artifact,
    )

    assert result["eligible"] is True
    assert result["history_sessions"] == 1512


def test_model_selection_prefers_positive_holdout_and_walk_forward_over_secondary_score():
    negative_holdout = {
        "lift": -0.002,
        "walk_forward_lift": 0.02,
        "walk_forward_folds": 3,
    }
    production_ready_metrics = {
        "lift": 0.001,
        "walk_forward_lift": 0.001,
        "walk_forward_folds": 3,
    }

    assert _model_selection_rank(production_ready_metrics, 0.002) > _model_selection_rank(negative_holdout, 0.01)


def test_promote_rejects_candidate_without_production_eligibility(monkeypatch, tmp_path):
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        model_strategy,
        "get_model_version",
        lambda model_version_id: {
            "id": model_version_id,
            "artifact_path": str(artifact),
            "validation": {"status": "passed", "production_eligible": False},
            "metrics": {"lift": 0.1, "walk_forward_lift": 0.1},
        },
    )

    with pytest.raises(HTTPException) as exc:
        model_strategy.model_strategy_promote(9)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "model_not_production_eligible"
