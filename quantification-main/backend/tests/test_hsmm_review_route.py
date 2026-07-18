from __future__ import annotations

from fastapi import HTTPException

from app.api.routes import model_strategy


def test_hsmm_review_route_marks_version_approved(monkeypatch):
    stored_validation: dict = {"status": "passed", "oos_end": "20260501"}

    monkeypatch.setattr(
        model_strategy,
        "get_model_version",
        lambda model_version_id: {
            "id": model_version_id,
            "model_key": "market_regime",
            "version": "hsmm-shadow-test",
            "validation": dict(stored_validation),
        },
    )
    monkeypatch.setattr(
        model_strategy,
        "market_state_model_governance",
        lambda: {"hsmm": {"status": "shadow_live_observation_review_ready", "latest_shadow_version": "hsmm-shadow-test"}},
    )

    captured: dict = {}

    def _update(model_version_id: int, *, validation: dict):
        captured["id"] = model_version_id
        captured["validation"] = validation

    monkeypatch.setattr(model_strategy, "update_model_version_validation", _update)

    result = model_strategy.model_strategy_review_hsmm_shadow(
        7,
        model_strategy.HsmmReviewRequest(reviewer="alice", decision="approved", comment="shadow ok"),
    )

    assert result["ok"] is True
    assert captured["id"] == 7
    assert captured["validation"]["production_eligible"] is True
    assert captured["validation"]["review"]["status"] == "approved"


def test_hsmm_review_route_rejects_before_review_ready(monkeypatch):
    monkeypatch.setattr(
        model_strategy,
        "get_model_version",
        lambda model_version_id: {
            "id": model_version_id,
            "model_key": "market_regime",
            "version": "hsmm-shadow-test",
            "validation": {"status": "passed", "oos_end": "20260701"},
        },
    )
    monkeypatch.setattr(
        model_strategy,
        "market_state_model_governance",
        lambda: {"hsmm": {"status": "shadow_live_observation", "latest_shadow_version": "hsmm-shadow-test"}},
    )

    try:
        model_strategy.model_strategy_review_hsmm_shadow(
            7,
            model_strategy.HsmmReviewRequest(reviewer="alice", decision="approved", comment="too early"),
        )
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("expected HTTPException")
