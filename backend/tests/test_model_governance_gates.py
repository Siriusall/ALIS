from app.services.model_registry import compare_champion_challenger
from app.services.plan_model_auto_optimizer import _candidate_cost_after_passed


def test_model_challenger_must_not_be_worse_than_champion(monkeypatch):
    monkeypatch.setattr("app.services.review_learning.model_feedback_gate", lambda *_args, **_kwargs: {"passed": True})
    result = compare_champion_challenger(
        {"id": 1, "version": "champion", "metrics": {"lift": .20, "walk_forward_lift": .15, "hit_rate": .60, "walk_forward_hit_rate": .60}, "config": {}},
        {"id": 2, "version": "challenger", "metrics": {"lift": .01, "walk_forward_lift": .01, "hit_rate": .60, "walk_forward_hit_rate": .60}, "validation": {"status": "passed"}, "config": {}},
    )
    assert result["decision"] == "keep_champion"
    assert "lift_not_lower_than_champion" in result["blocked_reasons"]


def test_zero_walk_forward_and_worst_window_are_not_filled_from_positive_lift():
    passed, reason = _candidate_cost_after_passed({"net_lift": .05, "walk_forward_lift": 0.0, "worst_window_net_lift": 0.0})
    assert passed is False
    assert "walk_forward" in reason
