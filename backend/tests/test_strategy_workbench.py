from __future__ import annotations

from app.services import strategy_workbench


def test_strategy_workbench_exposes_rule_fallback_and_failed_experiments(monkeypatch):
    monkeypatch.setattr(strategy_workbench, "get_model_status", lambda: {
        "has_active_model": False,
        "active_version": None,
        "active_artifact": {"ok": False, "status": "no_active_model"},
        "invalid_active_version": {"id": 1, "status": "artifact_missing"},
        "latest_train_run": {"id": 3, "status": "failed"},
    })
    monkeypatch.setattr(strategy_workbench, "list_model_versions", lambda model_key, limit=10: [
        {"id": 2, "version": "v2", "status": "candidate", "is_active": False, "validation": {"production_eligible": False, "production_eligibility": {"blocked_reasons": ["minimum_history_sessions"]}}},
    ])
    monkeypatch.setattr(strategy_workbench, "list_model_train_runs", lambda limit=10: [
        {"id": 3, "status": "failed", "phase": "failed", "progress": 1.0, "message": "训练失败", "error": "sample too small"},
    ])
    monkeypatch.setattr(strategy_workbench, "market_state_model_governance", lambda: {"hsmm": {"status": "shadow_live_observation"}})
    monkeypatch.setattr(strategy_workbench, "backend_acceptance_matrix", lambda: (_ for _ in ()).throw(AssertionError("heavy acceptance should be linked by default")))
    monkeypatch.setattr(strategy_workbench, "build_governance_overview", lambda: (_ for _ in ()).throw(AssertionError("heavy operations overview should be linked by default")))

    result = strategy_workbench.build_strategy_workbench()

    assert result["schema_version"] == "strategy_workbench_v1"
    assert result["status"]["model"] == "transparent_rule_fallback"
    assert result["current_analysis"]["scoring_source"] == "transparent_rule_fallback"
    assert result["model"]["failed_experiments"][0]["error"] == "sample too small"
    assert result["model"]["challengers"][0]["blocked_reasons"] == ["minimum_history_sessions"]
    assert result["pending_user_decisions"][0]["key"] == "main_model_fallback"
    assert result["backend_acceptance"]["status"] == "linked"
    assert result["operations"]["status"] == "linked"
