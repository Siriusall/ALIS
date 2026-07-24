from app.services import llm_research_governance as governance
from app.services.llm_research_governance import _fingerprint


def test_research_governance_fingerprint_is_stable_for_equivalent_hypotheses():
    assert _fingerprint({"target": "strategy", "title": " A ", "proposed_experiment": "Test"}) == _fingerprint({"target": "strategy", "title": "a", "proposed_experiment": "test"})


def test_research_governance_rejects_hypothesis_with_formal_failure_memory(monkeypatch):
    monkeypatch.setattr(governance, "_ensure_tables", lambda: None)
    monkeypatch.setattr(governance, "find_rejected_hypothesis", lambda *_args, **_kwargs: {"outcome_stage": "formal_oos"})

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def execute(self, sql, params=()):
            class Result:
                def fetchall(self):
                    return [{"id": 8, "payload_json": '{"hypotheses":[{"target":"strategy","title":"Top8","rationale":"x","proposed_experiment":"top_n=8","evidence_refs":["oos"],"confidence":0.8}]}'}] if "SELECT * FROM llm_research_proposals" in sql else []
            return Result()
    monkeypatch.setattr(governance, "_conn", lambda: Conn())

    result = governance.assess_llm_research_proposals()

    assert result["assessments"][0]["status"] == "rejected"
    assert "formally_rejected_hypothesis" in result["assessments"][0]["rejected"][0]["reason"]
