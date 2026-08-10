import numpy as np

from app.services.event_conditional_failure import (
    EVENT_CONDITIONAL_FAILURE_AUDIT_CONTRACT,
    aggregate_event_conditional_failure_audit,
    fold_event_conditional_failure_audit,
)


def test_conditional_failure_audit_preserves_native_baseline_by_group():
    rows = 24
    returns = np.asarray([0.02, -0.01] * 12, dtype=float)
    reaction = np.asarray([0.01, -0.01] * 12, dtype=float)
    direction = np.asarray([1.0, -1.0] * 12, dtype=float)
    outputs = {
        "positive_impact": np.asarray([0.9, 0.1] * 12),
        "priced_in": np.asarray([0.1] * rows),
        "continuation": np.asarray([0.9] * rows),
        "reversal": np.asarray([0.1] * rows),
        "direction_realization": np.asarray([0.9] * rows),
        "q10": returns - 0.005,
        "q50": returns,
        "q90": returns + 0.005,
    }
    result = fold_event_conditional_failure_audit(
        outputs=outputs,
        actual_abnormal_return=returns,
        train_abnormal_return=np.asarray([0.015, -0.015] * 30),
        event_reaction=reaction,
        train_event_reaction=np.asarray([0.01, -0.01] * 30),
        event_direction_signal=direction,
        train_event_direction_signal=np.asarray([1.0, -1.0] * 30),
        event_sources=["announcement"] * rows,
        train_event_sources=["announcement"] * 60,
        event_families=["earnings"] * rows,
        train_event_families=["earnings"] * 60,
        market_states=["S1"] * rows,
        fold=1,
        transaction_cost_rate=0.004,
        target_contract="conditional_market_adjusted_event_impact_v3",
    )
    assert result["contract"] == EVENT_CONDITIONAL_FAILURE_AUDIT_CONTRACT
    assert result["cells"]["event_family"]["earnings"]["supported"] is True
    assert result["cells"]["market_state"]["S1"]["rows"] == rows

    aggregate = aggregate_event_conditional_failure_audit([result])
    summary = aggregate["group_summary"]["event_family"]["earnings"]
    assert summary["supported_folds"] == [1]
    assert summary["probability_heads"]["actionability"]["observed_rows"] == rows


def test_conditional_failure_audit_does_not_promote_small_groups():
    rows = 10
    outputs = {name: np.full(rows, 0.5) for name in (
        "positive_impact", "priced_in", "continuation", "reversal",
        "direction_realization", "q10", "q50", "q90",
    )}
    result = fold_event_conditional_failure_audit(
        outputs=outputs,
        actual_abnormal_return=np.zeros(rows),
        train_abnormal_return=np.zeros(30),
        event_reaction=np.ones(rows),
        train_event_reaction=np.ones(30),
        event_direction_signal=np.ones(rows),
        train_event_direction_signal=np.ones(30),
        event_sources=["announcement"] * rows,
        train_event_sources=["announcement"] * 30,
        event_families=["governance_risk"] * rows,
        train_event_families=["governance_risk"] * 30,
        market_states=["S3"] * rows,
        fold=1,
        transaction_cost_rate=0.004,
        target_contract="conditional_market_adjusted_event_impact_v3",
    )
    cell = result["cells"]["event_family"]["governance_risk"]
    assert cell == {
        "rows": rows,
        "supported": False,
        "reason": "minimum_group_support_not_met",
    }
