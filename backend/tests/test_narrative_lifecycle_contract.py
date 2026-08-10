from app.services.narrative_lifecycle_contract import (
    SCHEMA_VERSION,
    candidate_state,
    crowding_risk,
    transition_state,
)


def test_maturity_does_not_require_crowding_measurement():
    assert candidate_state(
        base_score=64,
        momentum=0.01,
        breadth=0.64,
        persistence=0.70,
        price_confirmation=0.62,
        breadth_slope=0.01,
    ) == "成熟/拥挤"


def test_one_day_candidate_flip_is_held_until_min_duration_and_confirmation():
    state, meta = transition_state(
        "形成", "扩散", previous_age=3, candidate_streak=1
    )
    assert state == "形成"
    assert meta["confirmed"] is False
    state, meta = transition_state(
        "形成", "扩散", previous_age=3, candidate_streak=2
    )
    assert state == "扩散"
    assert meta["changed"] is True


def test_crowding_is_orthogonal_and_can_be_unobservable():
    result = crowding_risk(top_decile_concentration=0.0, company_count=4)
    assert result["observed"] is False
    assert result["band"] == "unobservable"
    assert SCHEMA_VERSION.endswith("v3")
