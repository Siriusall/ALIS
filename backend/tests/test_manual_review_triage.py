from __future__ import annotations

from app.services import manual_review_triage as triage
from app.services.manual_review_queue import _model_release_ready_for_manual_review


def test_model_release_waits_for_strict_strategy_ab_before_manual_review():
    latest = {
        "validation": {
            "production_eligibility": {"gates": {"controlled_strategy_ab": False}}
        }
    }
    assert _model_release_ready_for_manual_review(latest) is False


def test_model_release_enters_manual_review_only_after_strict_strategy_ab():
    latest = {
        "validation": {
            "production_eligibility": {
                "gates": {
                    "controlled_strategy_ab": True,
                    "live_observation_20_sessions": True,
                }
            }
        }
    }
    assert _model_release_ready_for_manual_review(latest) is True


def test_model_release_waits_for_future_observation_after_strategy_ab():
    latest = {
        "validation": {
            "production_eligibility": {
                "gates": {
                    "controlled_strategy_ab": True,
                    "live_observation_20_sessions": False,
                }
            }
        }
    }
    assert _model_release_ready_for_manual_review(latest) is False


def test_llm_configuration_failure_becomes_retryable_after_key_is_added(monkeypatch):
    monkeypatch.setattr(triage, "get_env_value", lambda name: "configured" if name == "DEEPSEEK_API_KEY" else "")
    state = {
        "status": "escalated", "summary": "LLM预审失败",
        "escalation_reason": "deepseek_api_key_not_configured", "model": "",
    }

    assert triage._retryable_llm_configuration_failure(state) is True


def test_immature_decision_waits_without_calling_llm(monkeypatch):
    saved: list[dict] = []
    monkeypatch.setattr(triage, "build_manual_review_queue", lambda **_kwargs: {
        "items": [{"key": "decision:12", "kind": "decision_review"}]
    })
    monkeypatch.setattr(triage, "list_manual_review_triage", lambda: {})
    monkeypatch.setattr(triage, "decision_outcomes", lambda _decision_id: [])
    monkeypatch.setattr(triage, "save_manual_review_triage", lambda **kwargs: saved.append(kwargs))
    monkeypatch.setattr(
        triage,
        "generate_review_draft",
        lambda _decision_id: (_ for _ in ()).throw(AssertionError("LLM must not run before outcomes mature")),
    )

    result = triage.run_manual_review_triage_once()

    assert result["awaiting_outcome"] == 1
    assert result["processed"] == 0
    assert saved[0]["status"] == "awaiting_outcome"
    assert saved[0]["escalation_reason"] == "尚未达到首个5交易日观察点，不需要人工判断"
