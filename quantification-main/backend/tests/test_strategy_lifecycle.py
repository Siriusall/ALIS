from app.services import strategy_lifecycle as lifecycle


def _shadow_rows(count: int, *, delta: float = 0.002) -> list[dict]:
    return [
        {
            "trade_date": f"2026-08-{index + 1:02d}",
            "candidate_return": 0.01,
            "champion_return": 0.01 - delta,
            "excess_delta": delta,
            "candidate_drawdown": -0.05,
            "champion_drawdown": -0.06,
            "point_in_time_passed": True,
            "behavior_passed": True,
        }
        for index in range(count)
    ]


def test_formal_gate_requires_protocol_and_non_worse_champion():
    gate = lifecycle.formal_candidate_gate(
        {
            "status": "passed",
            "summary": {
                "aggregate_cost_after_return": 0.12,
                "worst_window_drawdown": -0.11,
                "total_fees": 800,
            },
        },
        {
            "summary": {
                "aggregate_cost_after_return": 0.08,
                "worst_window_drawdown": -0.10,
                "total_fees": 700,
            },
        },
    )
    assert gate["passed"] is True
    failed = lifecycle.formal_candidate_gate(
        {"status": "failed", "summary": {"aggregate_cost_after_return": 0.20}},
        {"summary": {"aggregate_cost_after_return": 0.08}},
    )
    assert failed["passed"] is False
    assert "formal_oos_protocol_passed" in failed["blocking_reasons"]


def test_shadow_gate_cannot_promote_before_twenty_future_sessions():
    early = lifecycle.shadow_promotion_gate(_shadow_rows(19))
    assert early["passed"] is False
    assert "minimum_future_sessions" in early["blocking_reasons"]
    ready = lifecycle.shadow_promotion_gate(_shadow_rows(20))
    assert ready["passed"] is True
    assert ready["session_count"] == 20


def test_shadow_gate_blocks_point_in_time_or_behavior_failure():
    rows = _shadow_rows(20)
    rows[3]["point_in_time_passed"] = False
    rows[5]["behavior_passed"] = False
    gate = lifecycle.shadow_promotion_gate(rows)
    assert gate["passed"] is False
    assert "point_in_time_all_passed" in gate["blocking_reasons"]
    assert "behavior_all_passed" in gate["blocking_reasons"]


def test_cooling_gate_requests_rollback_on_material_underperformance():
    rows = _shadow_rows(5, delta=-0.011)
    gate = lifecycle.cooling_rollback_gate(rows, promoted_after="2026-07-31")
    assert gate["rollback"] is True
    assert "cooling_excess_below_minus_5pct" in gate["reasons"]


def test_formal_lifecycle_opens_shadow_only_for_eligible_candidate(monkeypatch):
    validations: list[tuple[int, dict]] = []
    statuses: list[tuple[int, str]] = []
    monkeypatch.setattr(lifecycle, "_merge_validation", lambda model_id, **updates: validations.append((model_id, updates)) or updates)
    monkeypatch.setattr(
        lifecycle,
        "update_model_version_status",
        lambda model_id, status, is_active: statuses.append((model_id, status)),
    )
    outcomes = lifecycle.advance_formal_candidate_lifecycle(
        [
            {
                "role": "champion",
                "strategy_id": 1,
                "summary": {
                    "aggregate_cost_after_return": 0.05,
                    "worst_window_drawdown": -0.12,
                    "total_fees": 1000,
                },
            },
            {
                "role": "challenger",
                "strategy_id": 2,
                "model_version_id": 9,
                "status": "passed",
                "summary": {
                    "aggregate_cost_after_return": 0.09,
                    "worst_window_drawdown": -0.10,
                    "total_fees": 900,
                },
            },
        ],
        shadow_start_date="20260722",
    )
    assert outcomes[0]["status"] == "shadow_observation"
    assert validations[0][1]["production_eligible"] is False
    assert validations[0][1]["shadow_started_after"] == "20260722"
    assert statuses == [(9, "shadow_observation")]
