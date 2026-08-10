from app.services.theme_engine import (
    NARRATIVE_LIFECYCLE_ALLOWED_TRANSITIONS,
    NARRATIVE_LIFECYCLE_STATES,
    _lifecycle,
)


def test_crowded_lifecycle_uses_pre_penalty_evidence_and_real_concentration():
    assert _lifecycle(
        score=44.0,
        base_score=60.0,
        momentum=0.02,
        breadth=0.62,
        crowding=0.80,
        price_confirmation=0.60,
    ) == "成熟/拥挤"


def test_four_state_lifecycle_has_explicit_safe_transition_graph():
    assert NARRATIVE_LIFECYCLE_STATES == ("形成", "扩散", "成熟/拥挤", "衰退")
    assert "成熟/拥挤" not in NARRATIVE_LIFECYCLE_ALLOWED_TRANSITIONS["形成"]
    assert "衰退" in NARRATIVE_LIFECYCLE_ALLOWED_TRANSITIONS["扩散"]
    assert NARRATIVE_LIFECYCLE_ALLOWED_TRANSITIONS["衰退"] == ("形成", "衰退")
