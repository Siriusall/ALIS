from app.services import model_candidate_research as research
from app.services.model_evaluation_contracts import SCHEMA_VERSION


def test_generic_candidate_attribution_is_retired():
    result = research.attribute_walk_forward_failure(
        {"model_key": "narrative_transition_multitask_v1"}
    )

    assert result["status"] == research.RETIRED_STATUS
    assert result["replacement_contract"] == SCHEMA_VERSION
    assert result["production_change_forbidden"] is True


def test_generic_candidate_tournament_cannot_dispatch():
    calls = []
    result = research.run_model_candidate_search(
        model_version_id=42,
        evaluator=lambda *_args: calls.append("evaluate") or {},
        formal_dispatcher=lambda *_args: calls.append("dispatch") or {},
    )

    assert result["status"] == research.RETIRED_STATUS
    assert result["model_version_id"] == 42
    assert calls == []
    assert research.generate_constrained_model_candidates("x", {}) == []
