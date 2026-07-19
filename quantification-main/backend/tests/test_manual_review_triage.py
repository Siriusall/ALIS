from __future__ import annotations

from app.services import manual_review_triage as triage


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
