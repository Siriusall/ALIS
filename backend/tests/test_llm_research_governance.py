from app.services import llm_research_governance as governance
from app.services.llm_research_governance import _fingerprint, _semantic_signature


def test_research_governance_fingerprint_is_stable_for_equivalent_hypotheses():
    assert _fingerprint({"target": "strategy", "title": " A ", "proposed_experiment": "Test"}) == _fingerprint({"target": "strategy", "title": "a", "proposed_experiment": "test"})


def test_semantic_signature_collapses_same_route_issue_family() -> None:
    first = _semantic_signature(
        {"target": "data", "title": "数据质量问题导致负超额收益"}
    )
    second = _semantic_signature(
        {"target": "data", "title": "数据异常令模型判断发生偏差"}
    )
    assert first == second == "data|data_quality"


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


def test_governance_rejects_cross_proposal_semantic_duplicate_in_same_period(monkeypatch):
    monkeypatch.setattr(governance, "_ensure_tables", lambda: None)
    monkeypatch.setattr(governance, "find_rejected_hypothesis", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        governance,
        "llm_research_proposal_quality_report",
        lambda: {"status": "observing"},
    )
    rows = [
        {
            "id": 1,
            "source_period": "2026-07",
            "evidence_json": "{}",
            "payload_json": '{"hypotheses":[{"target":"data","title":"数据质量问题导致负收益","rationale":"x","proposed_experiment":"检查缺失率","evidence_refs":["data_quality"],"confidence":0.8}]}'
        },
        {
            "id": 2,
            "source_period": "2026-07",
            "evidence_json": "{}",
            "payload_json": '{"hypotheses":[{"target":"data","title":"数据异常导致信号偏差","rationale":"y","proposed_experiment":"复核异常值","evidence_refs":["数据质量"],"confidence":0.8}]}'
        },
    ]

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def execute(self, sql, params=()):
            class Result:
                def fetchall(self):
                    return rows if "SELECT * FROM llm_research_proposals" in sql else []
            return Result()

    monkeypatch.setattr(governance, "_conn", lambda: Conn())
    result = governance.assess_llm_research_proposals()

    assert result["assessments"][0]["status"] == "accepted"
    assert result["assessments"][1]["status"] == "rejected"
    assert "semantic_duplicate_hypothesis" in result["assessments"][1]["rejected"][0]["reason"]
