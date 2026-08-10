from app.services.model_registry import compare_champion_challenger
from app.services.plan_model_auto_optimizer import _candidate_evaluation_passed


def test_retired_generic_champion_comparison_cannot_change_production():
    result = compare_champion_challenger(
        {"id": 1, "version": "champion"},
        {"id": 2, "version": "challenger"},
    )
    assert result["decision"] == "no_action"
    assert result["production_change_forbidden"] is True


def test_old_model_evaluation_contract_is_never_accepted():
    passed, reason = _candidate_evaluation_passed({})
    assert passed is False
    assert reason == "legacy_model_evaluation_contract"
