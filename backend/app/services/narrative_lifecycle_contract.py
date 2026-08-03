"""Point-in-time, observable narrative lifecycle label contract.

The contract deliberately treats crowding as an orthogonal risk observation.
It is not a required lifecycle class because a theme can be mature without
having a reliable concentration/crowding measurement.
"""

from __future__ import annotations

from typing import Any


SCHEMA_VERSION = "alis_narrative_lifecycle_observable_v3"
STATES = ("形成", "扩散", "成熟/拥挤", "衰退")

# The third label is retained for compatibility with historical snapshots. Its
# v3 meaning is "成熟；拥挤风险另行记录", not "must be crowded".
STATE_SEMANTICS = {
    "形成": "新出现或重新出现，价格确认和广度尚未同时稳定",
    "扩散": "价格、广度和参与度同步扩张",
    "成熟/拥挤": "成熟阶段；crowding_risk 是独立风险字段，不是进入该状态的前提",
    "衰退": "价格和广度持续恶化，或主题确认明显失效",
}

ALLOWED_TRANSITIONS = {
    "形成": ("形成", "扩散", "衰退"),
    "扩散": ("形成", "扩散", "成熟/拥挤", "衰退"),
    "成熟/拥挤": ("扩散", "成熟/拥挤", "衰退"),
    "衰退": ("形成", "衰退"),
}

# Minimum observed sessions before a confirmed transition. A transition also
# needs two consecutive candidate observations; this avoids one-day flips.
MIN_STATE_SESSIONS = {"形成": 3, "扩散": 5, "成熟/拥挤": 5, "衰退": 3}
CONFIRMATION_SESSIONS = 2


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def crowding_risk(*, top_decile_concentration: float, company_count: int) -> dict[str, Any]:
    """Return an observed crowding risk without forcing a lifecycle label.

    Concentration is only considered observable when enough members and
    positive turnover exist. The thresholds are descriptive risk bands, not a
    class-balancing device and not a prerequisite for maturity.
    """

    observed = int(company_count or 0) >= 10 and float(top_decile_concentration or 0.0) > 0
    value = _clip(float(top_decile_concentration or 0.0))
    return {
        "value": round(value, 8),
        "observed": bool(observed),
        "band": "high" if observed and value >= 0.60 else "elevated" if observed and value >= 0.45 else "normal" if observed else "unobservable",
        "method": "top_decile_turnover_concentration_observable_v1",
    }

def candidate_state(
    *,
    base_score: float,
    momentum: float,
    breadth: float,
    persistence: float = 0.0,
    price_confirmation: float,
    breadth_slope: float = 0.0,
    score_slope: float = 0.0,
) -> str:
    """Map only current and trailing observable evidence to a candidate state.

    No future return, future label, or crowding threshold is used here. The
    mature state is intentionally reachable without crowding evidence.
    """

    decline = (
        (momentum <= -0.01 and breadth < 0.45)
        or (score_slope <= -0.08 and breadth_slope < -0.03)
        or (price_confirmation < 0.35 and breadth < 0.35)
    )
    if decline:
        return "衰退"

    expansion = (
        base_score >= 55
        and breadth >= 0.55
        and price_confirmation >= 0.50
        and (persistence >= 0.50 or breadth_slope >= 0.0)
    )
    mature = (
        base_score >= 60
        and breadth >= 0.58
        and price_confirmation >= 0.50
        and persistence >= 0.55
        and abs(breadth_slope) <= 0.08
    )
    if mature:
        return "成熟/拥挤"
    if expansion:
        return "扩散"
    return "形成"


def transition_state(
    previous: str | None,
    candidate: str,
    *,
    previous_age: int = 0,
    candidate_streak: int = 1,
) -> tuple[str, dict[str, Any]]:
    """Apply the lifecycle graph, minimum duration and confirmation rule."""

    candidate = candidate if candidate in STATES else "形成"
    if previous not in STATES:
        return candidate, {"changed": True, "held_for_min_duration": False, "confirmed": True}
    if candidate == previous:
        return previous, {"changed": False, "held_for_min_duration": False, "confirmed": True}
    allowed = candidate in ALLOWED_TRANSITIONS[previous]
    min_duration_ok = int(previous_age or 0) >= int(MIN_STATE_SESSIONS[previous])
    confirmed = int(candidate_streak or 0) >= CONFIRMATION_SESSIONS
    if not (allowed and min_duration_ok and confirmed):
        return previous, {
            "changed": False,
            "held_for_min_duration": not min_duration_ok,
            "confirmed": confirmed,
            "transition_allowed": allowed,
        }
    return candidate, {
        "changed": True,
        "held_for_min_duration": False,
        "confirmed": True,
        "transition_allowed": True,
    }


def contract_payload() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "states": list(STATES),
        "state_semantics": dict(STATE_SEMANTICS),
        "allowed_transitions": {key: list(value) for key, value in ALLOWED_TRANSITIONS.items()},
        "minimum_state_sessions": dict(MIN_STATE_SESSIONS),
        "confirmation_sessions": CONFIRMATION_SESSIONS,
        "crowding_policy": "orthogonal_observable_risk_not_required_for_maturity",
        "future_information": "forbidden_in_label_generation",
        "synthetic_samples": False,
        "oversampling": False,
    }
