from __future__ import annotations

from datetime import datetime, timedelta
import json
from math import isfinite
from typing import Any, Callable

from app.db.store import (
    _conn,
    get_model_version,
    list_model_versions,
    update_model_version_status,
    update_model_version_validation,
)
from app.services.backtest_data_source import BacktestDataSource
from app.services.production_strategy import (
    promote_strategy_candidate,
    resolve_production_strategy_champion,
    rollback_strategy_champion,
)
from app.services.strategy_replay import run_strategy_replay
from app.services.sealed_strategy_holdout import run_sealed_strategy_holdout
from app.services.strategy_statistics import paired_block_bootstrap_compounded_excess_ci


SHADOW_MINIMUM_SESSIONS = 20
SHADOW_MAXIMUM_SESSIONS = 60
COOLING_MINIMUM_SESSIONS = 20
SHADOW_RETURN_CONTRACT = "non_overlapping_daily_equity_v1"
SHADOW_MINIMUM_EFFECTIVE_REBALANCES = 4
SHADOW_REPLAY_LOOKBACK_CALENDAR_DAYS = 90


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _date_key(value: Any) -> str:
    return str(value or "").replace("-", "")[:8]


def _formal_reference_controller_key(
    item: dict[str, Any] | None,
    reference: dict[str, Any],
) -> str:
    item = item if isinstance(item, dict) else {}
    controller_id = int(
        item.get("controller_version_id")
        or reference.get("strategy_controller_version_id")
        or 0
    )
    controller_name = str(reference.get("strategy_controller_version") or "")
    numeric_name_id = int(controller_name) if controller_name.isdigit() else 0
    lookup_id = controller_id or numeric_name_id
    if lookup_id:
        version = get_model_version(lookup_id) or {}
        if str(version.get("model_key") or "") == "strategy_controller":
            resolved_name = str(version.get("version") or "")
            if resolved_name:
                return f"version:{resolved_name}"
        if controller_name and not controller_name.isdigit():
            return f"version:{controller_name}"
        return f"id:{lookup_id}"
    return f"version:{controller_name}" if controller_name else ""


def _formal_references_compatible(
    left_item: dict[str, Any] | None,
    left_reference: dict[str, Any] | None,
    right_item: dict[str, Any] | None,
    right_reference: dict[str, Any] | None,
) -> bool:
    if not isinstance(left_reference, dict) or not isinstance(right_reference, dict):
        return False
    controller_fields = {
        "strategy_controller_version_id",
        "strategy_controller_version",
    }
    left_core = {
        key: value for key, value in left_reference.items()
        if key not in controller_fields
    }
    right_core = {
        key: value for key, value in right_reference.items()
        if key not in controller_fields
    }
    if left_core != right_core:
        return False
    left_controller = _formal_reference_controller_key(left_item, left_reference)
    right_controller = _formal_reference_controller_key(right_item, right_reference)
    return bool(left_controller and left_controller == right_controller)


def _merge_validation(model_version_id: int, **updates: Any) -> dict[str, Any]:
    version = get_model_version(int(model_version_id)) or {}
    current = version.get("validation") if isinstance(version.get("validation"), dict) else {}
    merged = {**current, **updates}
    update_model_version_validation(int(model_version_id), validation=merged)
    return merged


def _shadow_competition_key(candidate: dict[str, Any]) -> tuple[float, float, float, float, int]:
    validation = candidate.get("validation") if isinstance(candidate.get("validation"), dict) else {}
    gate = validation.get("shadow_gate") if isinstance(validation.get("shadow_gate"), dict) else {}
    formal = ((candidate.get("metrics") or {}).get("formal_oos") or {})
    return (
        _number(gate.get("compounded_delta")),
        _number(gate.get("outperform_ratio")),
        _number(gate.get("worst_drawdown_gap")),
        _number(formal.get("aggregate_excess_return")),
        -int(candidate.get("id") or 0),
    )


def reactivate_best_shadow_reserve(
    *,
    failed_leader_id: int,
    shadow_started_after: str,
    versions: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Restart the best reserve after its competition leader is rolled back."""
    available = versions if versions is not None else list_model_versions("alis_strategy_challenger", limit=100)
    reserves = []
    for version in available:
        validation = version.get("validation") if isinstance(version.get("validation"), dict) else {}
        competition = validation.get("shadow_competition") if isinstance(validation.get("shadow_competition"), dict) else {}
        if (
            str(validation.get("status") or "") == "shadow_reserve"
            and int(competition.get("winner_model_version_id") or 0) == int(failed_leader_id)
        ):
            reserves.append(version)
    if not reserves:
        return None
    reserve = max(reserves, key=_shadow_competition_key)
    reserve_id = int(reserve["id"])
    validation = _merge_validation(
        reserve_id,
        status="shadow_observation",
        production_eligible=False,
        shadow_started_after=str(shadow_started_after),
        shadow_observation_sessions=0,
        next_gate="future_shadow_observation",
        blocking_reasons=[],
        reserve_reactivated_after_leader_id=int(failed_leader_id),
    )
    update_model_version_status(reserve_id, status="shadow_observation", is_active=False)
    return {
        "model_version_id": reserve_id,
        "status": "shadow_observation",
        "shadow_started_after": str(shadow_started_after),
        "validation": validation,
    }


def formal_candidate_gate(
    candidate: dict[str, Any],
    champion: dict[str, Any] | None,
    *,
    opportunity_cost_baselines: list[dict[str, Any]] | None = None,
    sealed_holdout: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate_summary = candidate.get("summary") if isinstance(candidate.get("summary"), dict) else {}
    champion_summary = champion.get("summary") if champion and isinstance(champion.get("summary"), dict) else {}
    candidate_reference = candidate.get("reference_contract") if isinstance(candidate.get("reference_contract"), dict) else None
    champion_reference = champion.get("reference_contract") if champion and isinstance(champion.get("reference_contract"), dict) else None
    reference_compatible = _formal_references_compatible(
        candidate,
        candidate_reference,
        champion,
        champion_reference,
    )

    def metric(summary: dict[str, Any], key: str) -> float | None:
        if key not in summary or summary.get(key) is None:
            return None
        value = _number(summary.get(key), float("nan"))
        return value if isfinite(value) else None

    candidate_return = metric(candidate_summary, "aggregate_cost_after_return")
    champion_return = metric(champion_summary, "aggregate_cost_after_return")
    candidate_drawdown = metric(candidate_summary, "worst_window_drawdown")
    champion_drawdown = metric(champion_summary, "worst_window_drawdown")
    candidate_fees = metric(candidate_summary, "total_fees")
    champion_fees = metric(champion_summary, "total_fees")
    candidate_annualized_excess = metric(candidate_summary, "annualized_relative_excess_return")
    minimum_annualized_excess = metric(candidate_summary, "minimum_annualized_relative_excess_return")
    reference_metrics_complete = all(value is not None for value in (
        champion_return, champion_drawdown, champion_fees,
    ))
    candidate_metrics_complete = all(value is not None for value in (
        candidate_return, candidate_drawdown, candidate_fees,
        candidate_annualized_excess, minimum_annualized_excess,
    ))
    reference_ready = reference_compatible and reference_metrics_complete
    baseline_gate_enabled = opportunity_cost_baselines is not None
    baseline_rows = opportunity_cost_baselines if isinstance(opportunity_cost_baselines, list) else []
    compatible_baselines = [
        item for item in baseline_rows
        if isinstance(item, dict)
        and _formal_references_compatible(
            candidate,
            candidate_reference,
            item,
            item.get("reference_contract"),
        )
        and metric(item.get("summary") if isinstance(item.get("summary"), dict) else {}, "aggregate_cost_after_return") is not None
    ]
    required_baseline_keys = {"multifactor_equal_weight", "simple_momentum"}
    compatible_baseline_keys = {str(item.get("baseline_key") or "") for item in compatible_baselines}
    opportunity_cost_reference_ready = (
        required_baseline_keys.issubset(compatible_baseline_keys)
        if baseline_gate_enabled else True
    )
    best_baseline = max(
        compatible_baselines,
        key=lambda item: metric(item.get("summary") or {}, "aggregate_cost_after_return") or float("-inf"),
        default=None,
    )
    best_baseline_return = metric(best_baseline.get("summary") or {}, "aggregate_cost_after_return") if best_baseline else None
    candidate_statistics = candidate.get("statistics") if isinstance(candidate.get("statistics"), dict) else {}
    multiple_testing = candidate_statistics.get("multiple_testing") if isinstance(candidate_statistics.get("multiple_testing"), dict) else {}
    statistical_evidence_complete = (
        multiple_testing.get("schema_version") == "alis_deflated_sharpe_ratio_v2"
        and multiple_testing.get("daily_return_contract_complete") is True
        and multiple_testing.get("unique_trade_dates") is True
        and multiple_testing.get("probability_sharpe_exceeds_selection_bias") is not None
    )
    multiple_testing_support = statistical_evidence_complete and bool(multiple_testing.get("passed"))
    holdout = sealed_holdout if isinstance(sealed_holdout, dict) else {}
    holdout_status = str(holdout.get("status") or "missing")
    holdout_passed = bool(holdout.get("passed")) and holdout_status == "passed"
    gates = {
        "same_run_window_cost_reference": reference_ready,
        "opportunity_cost_references_complete": opportunity_cost_reference_ready,
        "candidate_metrics_complete": candidate_metrics_complete,
        "formal_oos_protocol_passed": str(candidate.get("status") or "") == "passed",
        "multiple_testing_adjusted_support": multiple_testing_support,
        "sealed_historical_holdout_passed": holdout_passed,
        "economically_meaningful_annualized_excess": bool(
            candidate_annualized_excess is not None
            and minimum_annualized_excess is not None
            and candidate_annualized_excess >= minimum_annualized_excess
        ),
        "return_not_worse_than_champion": bool(reference_ready and candidate_return is not None and candidate_return >= champion_return),
        "return_above_best_low_complexity_baseline": bool(
            not baseline_gate_enabled
            or (opportunity_cost_reference_ready
            and candidate_return is not None
            and best_baseline_return is not None
            and candidate_return > best_baseline_return)
        ),
        "drawdown_not_materially_worse_than_champion": bool(
            reference_ready and candidate_drawdown is not None and candidate_drawdown >= champion_drawdown - 0.02
        ),
        "fees_bounded_vs_champion": bool(
            reference_ready
            and candidate_fees is not None
            and (champion_fees <= 0 or candidate_fees <= max(champion_fees * 1.5, champion_fees + 500.0))
        ),
    }
    if str(candidate.get("status") or "") == "failed":
        decision = "rejected"
    elif not reference_ready or not opportunity_cost_reference_ready:
        decision = "blocked_by_reference"
    elif str(candidate.get("status") or "") != "passed" or not candidate_metrics_complete:
        decision = "rejected"
    elif not multiple_testing_support:
        decision = "blocked_by_statistics"
    elif holdout_status in {"missing", "blocked_by_data"}:
        decision = "blocked_by_holdout_data"
    elif not holdout_passed:
        decision = "blocked_by_holdout"
    else:
        decision = "eligible" if all(gates.values()) else "rejected"
    return {
        "passed": decision == "eligible",
        "decision": decision,
        "gates": gates,
        "blocking_reasons": [key for key, passed in gates.items() if not passed],
        "reference_contract": candidate_reference if reference_compatible else None,
        "multiple_testing": multiple_testing,
        "sealed_holdout": holdout,
        "comparison": {
            "candidate_return": candidate_return,
            "champion_return": champion_return,
            "candidate_drawdown": candidate_drawdown,
            "champion_drawdown": champion_drawdown,
            "candidate_fees": candidate_fees,
            "champion_fees": champion_fees,
            "candidate_annualized_relative_excess_return": candidate_annualized_excess,
            "minimum_annualized_relative_excess_return": minimum_annualized_excess,
            "best_low_complexity_baseline_key": str((best_baseline or {}).get("baseline_key") or "") or None,
            "best_low_complexity_baseline_return": best_baseline_return,
            "required_low_complexity_baselines": sorted(required_baseline_keys),
            "available_low_complexity_baselines": sorted(compatible_baseline_keys),
        },
    }


def advance_formal_candidate_lifecycle(
    items: list[dict[str, Any]],
    *,
    shadow_start_date: str,
    holdout_runner: Callable[..., dict[str, Any]] = run_sealed_strategy_holdout,
) -> list[dict[str, Any]]:
    champion = next((item for item in items if item.get("role") == "champion"), None)
    opportunity_cost_baselines = [item for item in items if item.get("role") == "baseline"]
    outcomes: list[dict[str, Any]] = []
    for item in items:
        if item.get("role") != "challenger" or not int(item.get("model_version_id") or 0):
            continue
        if str(item.get("status") or "") == "quick_diagnostic_failed":
            outcomes.append({
                "model_version_id": int(item["model_version_id"]),
                "status": "quick_diagnostic_failed",
            })
            continue
        model_version_id = int(item["model_version_id"])
        pre_holdout_gate = formal_candidate_gate(
            item, champion,
            opportunity_cost_baselines=opportunity_cost_baselines,
            sealed_holdout={"status": "passed", "passed": True, "policy": "pre_holdout_gate_only"},
        )
        sealed_holdout: dict[str, Any] | None = None
        if pre_holdout_gate["decision"] == "eligible":
            sealed_holdout = holdout_runner(
                model_version_id,
                reference_contract=item.get("reference_contract") or {},
            )
            gate = formal_candidate_gate(
                item,
                champion,
                opportunity_cost_baselines=opportunity_cost_baselines,
                sealed_holdout=sealed_holdout,
            )
        else:
            gate = pre_holdout_gate
        if gate["decision"] == "eligible":
            existing_version = get_model_version(model_version_id) or {}
            existing_validation = (
                existing_version.get("validation")
                if isinstance(existing_version.get("validation"), dict)
                else {}
            )
            effective_shadow_start = max(
                _date_key(shadow_start_date),
                _date_key(existing_validation.get("parallel_shadow_started_after")),
            )
            _merge_validation(
                model_version_id,
                status="shadow_observation",
                production_eligible=False,
                formal_comparison=gate,
                shadow_started_after=effective_shadow_start,
                shadow_minimum_sessions=SHADOW_MINIMUM_SESSIONS,
                shadow_observation_sessions=0,
                next_gate="future_shadow_observation",
                blocking_reasons=[],
            )
            update_model_version_status(model_version_id, status="shadow_observation", is_active=False)
            status = "shadow_observation"
        elif gate["decision"] == "blocked_by_reference":
            _merge_validation(
                model_version_id,
                status="formal_oos_blocked_by_reference",
                production_eligible=False,
                formal_comparison=gate,
                next_gate="formal_oos_reference_rerun",
                blocking_reasons=gate["blocking_reasons"],
            )
            update_model_version_status(model_version_id, status="candidate", is_active=False)
            status = "blocked_by_reference"
        elif gate["decision"] == "blocked_by_statistics":
            _merge_validation(
                model_version_id,
                status="formal_oos_blocked_by_statistics",
                production_eligible=False,
                formal_comparison=gate,
                formal_statistics_blocked_after=shadow_start_date,
                next_gate="future_formal_oos_refresh",
                blocking_reasons=gate["blocking_reasons"],
            )
            update_model_version_status(model_version_id, status="candidate", is_active=False)
            status = "blocked_by_statistics"
        elif gate["decision"] == "blocked_by_holdout_data":
            _merge_validation(
                model_version_id,
                status="formal_oos_blocked_by_holdout_data",
                production_eligible=False,
                formal_comparison=gate,
                holdout_blocked_after=shadow_start_date,
                next_gate="sealed_holdout_data_repair",
                blocking_reasons=gate["blocking_reasons"],
            )
            update_model_version_status(model_version_id, status="candidate", is_active=False)
            status = "blocked_by_holdout_data"
        elif gate["decision"] == "blocked_by_holdout":
            _merge_validation(
                model_version_id,
                status="formal_oos_blocked_by_holdout",
                production_eligible=False,
                formal_comparison=gate,
                holdout_blocked_after=shadow_start_date,
                next_gate="future_independent_holdout_cohort",
                blocking_reasons=gate["blocking_reasons"],
            )
            update_model_version_status(model_version_id, status="candidate", is_active=False)
            status = "blocked_by_holdout"
        else:
            _merge_validation(
                model_version_id,
                status="formal_oos_rejected",
                production_eligible=False,
                formal_comparison=gate,
                next_gate="automatic_candidate_regeneration",
                blocking_reasons=gate["blocking_reasons"],
            )
            update_model_version_status(model_version_id, status="rejected", is_active=False)
            from app.services.experiment_memory import record_model_experiment_outcome

            version = get_model_version(model_version_id) or {}
            record_model_experiment_outcome(
                version,
                stage="formal_oos",
                rejected=True,
                evidence={"formal_comparison": gate, "blocking_reasons": gate["blocking_reasons"]},
            )
            status = "formal_oos_rejected"
        outcomes.append({"model_version_id": model_version_id, "status": status, **gate})
    return outcomes


def record_shadow_observation(
    *,
    model_version_id: int,
    champion_version_id: int,
    trade_date: str,
    candidate_return: float,
    champion_return: float,
    candidate_drawdown: float,
    champion_drawdown: float,
    point_in_time_passed: bool,
    behavior_passed: bool,
    metrics: dict[str, Any] | None = None,
) -> None:
    now = datetime.now().astimezone().isoformat()
    excess_delta = float(candidate_return) - float(champion_return)
    with _conn() as conn:
        existing = conn.execute(
            """SELECT id FROM strategy_shadow_observations
               WHERE model_version_id=? AND trade_date=?""",
            (int(model_version_id), str(trade_date)),
        ).fetchone()
        values = (
            int(champion_version_id), float(candidate_return), float(champion_return),
            excess_delta, float(candidate_drawdown), float(champion_drawdown),
            1 if point_in_time_passed else 0, 1 if behavior_passed else 0,
            json.dumps(metrics or {}, ensure_ascii=False, default=str), now,
        )
        if existing:
            conn.execute(
                """UPDATE strategy_shadow_observations SET
                   champion_version_id=?,candidate_return=?,champion_return=?,
                   excess_delta=?,candidate_drawdown=?,champion_drawdown=?,
                   point_in_time_passed=?,behavior_passed=?,metrics_json=?,created_at=?
                   WHERE id=?""",
                (*values, int(existing["id"])),
            )
        else:
            conn.execute(
                """INSERT INTO strategy_shadow_observations(
                   model_version_id,champion_version_id,trade_date,
                   candidate_return,champion_return,excess_delta,
                   candidate_drawdown,champion_drawdown,point_in_time_passed,
                   behavior_passed,metrics_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (int(model_version_id), *values[:1], str(trade_date), *values[1:]),
            )


def list_shadow_observations(model_version_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM strategy_shadow_observations
               WHERE model_version_id=? ORDER BY trade_date ASC LIMIT ?""",
            (int(model_version_id), max(1, min(int(limit), 500))),
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["metrics"] = json.loads(item.pop("metrics_json", "") or "{}")
        except Exception:
            item["metrics"] = {}
        item["point_in_time_passed"] = bool(item.get("point_in_time_passed"))
        item["behavior_passed"] = bool(item.get("behavior_passed"))
        items.append(item)
    return items


def _replay_daily_return(replay: dict[str, Any], *, trade_date: str) -> tuple[float, dict[str, Any]]:
    series = replay.get("equity_series") if isinstance(replay.get("equity_series"), list) else []
    if len(series) < 2:
        raise RuntimeError(f"影子重放 {trade_date} 缺少相邻两个权益点")
    previous = series[-2] if isinstance(series[-2], dict) else {}
    current = series[-1] if isinstance(series[-1], dict) else {}
    if _date_key(current.get("date")) != _date_key(trade_date):
        raise RuntimeError(f"影子重放 {trade_date} 缺少当日权益点")
    previous_equity = _number(previous.get("total_value"))
    current_equity = _number(current.get("total_value"))
    if previous_equity <= 0 or current_equity < 0:
        raise RuntimeError(f"影子重放 {trade_date} 权益点无效")
    daily_return = current_equity / previous_equity - 1.0
    if not isfinite(daily_return) or daily_return <= -1.0:
        raise RuntimeError(f"影子重放 {trade_date} 单日收益无效")
    return daily_return, {
        "previous_date": str(previous.get("date") or ""),
        "previous_equity": previous_equity,
        "trade_date": str(current.get("date") or ""),
        "current_equity": current_equity,
    }


def _forward_return_path(
    observations: list[dict[str, Any]],
    return_key: str,
) -> tuple[float, float]:
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for item in observations:
        equity *= 1.0 + _number(item.get(return_key))
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1.0)
    return equity - 1.0, max_drawdown


def _valid_shadow_return_contract(item: dict[str, Any]) -> bool:
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    candidate_return = _number(item.get("candidate_return"), float("nan"))
    champion_return = _number(item.get("champion_return"), float("nan"))
    return (
        metrics.get("return_contract") == SHADOW_RETURN_CONTRACT
        and isfinite(candidate_return)
        and isfinite(champion_return)
        and candidate_return > -1.0
        and champion_return > -1.0
    )


def _effective_rebalance_count(observations: list[dict[str, Any]]) -> int:
    return sum(
        int(((item.get("metrics") or {}).get("candidate_daily_trade_count") or 0)) > 0
        for item in observations
        if isinstance(item.get("metrics"), dict)
    )


def shadow_promotion_gate(
    observations: list[dict[str, Any]],
    *,
    minimum_sessions: int = SHADOW_MINIMUM_SESSIONS,
) -> dict[str, Any]:
    sample = observations[-max(minimum_sessions, SHADOW_MAXIMUM_SESSIONS):]
    session_count = len(sample)
    deltas = [_number(item.get("candidate_return")) - _number(item.get("champion_return")) for item in sample]
    candidate_return, candidate_drawdown = _forward_return_path(sample, "candidate_return")
    champion_return, champion_drawdown = _forward_return_path(sample, "champion_return")
    compounded_delta = candidate_return - champion_return
    effective_rebalances = _effective_rebalance_count(sample)
    uncertainty = paired_block_bootstrap_compounded_excess_ci(sample)
    ci_low, ci_high = uncertainty["compounded_excess_ci_95"]
    outperform_ratio = sum(value > 0 for value in deltas) / session_count if session_count else 0.0
    worst_drawdown_gap = candidate_drawdown - champion_drawdown
    gates = {
        "minimum_future_sessions": session_count >= minimum_sessions,
        "minimum_effective_rebalances": effective_rebalances >= SHADOW_MINIMUM_EFFECTIVE_REBALANCES,
        "non_overlapping_daily_return_contract": bool(sample) and all(_valid_shadow_return_contract(item) for item in sample),
        "point_in_time_all_passed": bool(sample) and all(bool(item.get("point_in_time_passed")) for item in sample),
        "behavior_all_passed": bool(sample) and all(bool(item.get("behavior_passed")) for item in sample),
        "shadow_compounded_delta_positive": compounded_delta > 0,
        "shadow_excess_confidence_lower_positive": ci_low is not None and ci_low > 0,
        "majority_shadow_sessions_outperform": outperform_ratio >= 0.55,
        "shadow_drawdown_not_materially_worse": worst_drawdown_gap >= -0.02,
    }
    return {
        "passed": all(gates.values()),
        "session_count": session_count,
        "minimum_sessions": minimum_sessions,
        "minimum_effective_rebalances": SHADOW_MINIMUM_EFFECTIVE_REBALANCES,
        "effective_rebalance_count": effective_rebalances,
        "candidate_compounded_return": round(candidate_return, 8),
        "champion_compounded_return": round(champion_return, 8),
        "candidate_forward_drawdown": round(candidate_drawdown, 8),
        "champion_forward_drawdown": round(champion_drawdown, 8),
        "compounded_delta": round(compounded_delta, 8),
        "outperform_ratio": round(outperform_ratio, 8),
        "worst_drawdown_gap": round(worst_drawdown_gap, 8),
        "uncertainty": uncertainty,
        "observation_decision": (
            "promote" if all(gates.values())
            else "continue_minimum_sessions" if session_count < minimum_sessions
            else "extend_for_effective_rebalances" if effective_rebalances < SHADOW_MINIMUM_EFFECTIVE_REBALANCES and session_count < SHADOW_MAXIMUM_SESSIONS
            else "extend_for_uncertainty" if (ci_low is None or ci_low <= 0) and session_count < SHADOW_MAXIMUM_SESSIONS
            else "reject"
        ),
        "gates": gates,
        "blocking_reasons": [key for key, passed in gates.items() if not passed],
    }


def cooling_rollback_gate(
    observations: list[dict[str, Any]],
    *,
    promoted_after: str,
) -> dict[str, Any]:
    sample = [item for item in observations if str(item.get("trade_date") or "") > str(promoted_after or "")]
    recent = sample[-COOLING_MINIMUM_SESSIONS:]
    candidate_return, candidate_drawdown = _forward_return_path(recent, "candidate_return")
    champion_return, champion_drawdown = _forward_return_path(recent, "champion_return")
    compounded_delta = candidate_return - champion_return
    worst_drawdown_gap = candidate_drawdown - champion_drawdown
    reasons: list[str] = []
    if recent and any(not _valid_shadow_return_contract(item) for item in recent):
        reasons.append("invalid_daily_return_contract")
    if any(not bool(item.get("point_in_time_passed")) or not bool(item.get("behavior_passed")) for item in recent):
        reasons.append("data_or_behavior_contract_breached")
    if len(recent) >= 5 and compounded_delta <= -0.05:
        reasons.append("cooling_excess_below_minus_5pct")
    if len(recent) >= 5 and worst_drawdown_gap <= -0.05:
        reasons.append("cooling_drawdown_worse_by_5pct")
    return {
        "rollback": bool(reasons),
        "stable": len(recent) >= COOLING_MINIMUM_SESSIONS and not reasons,
        "session_count": len(recent),
        "compounded_delta": round(compounded_delta, 8),
        "worst_drawdown_gap": round(worst_drawdown_gap, 8),
        "reasons": reasons,
    }


def _record_promotion_event(
    event_type: str,
    *,
    from_id: int | None,
    to_id: int | None,
    candidate_id: int | None,
    reason: dict[str, Any],
    evidence: dict[str, Any],
) -> int:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO strategy_promotion_events(
               event_type,from_champion_version_id,to_champion_version_id,
               candidate_model_version_id,reason_json,evidence_json,created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                str(event_type), from_id, to_id, candidate_id,
                json.dumps(reason, ensure_ascii=False, default=str),
                json.dumps(evidence, ensure_ascii=False, default=str),
                datetime.now().astimezone().isoformat(),
            ),
        )
        return conn.last_insert_id()


def list_strategy_promotion_events(limit: int = 50) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM strategy_promotion_events ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for source, target in (("reason_json", "reason"), ("evidence_json", "evidence")):
            try:
                item[target] = json.loads(item.pop(source, "") or "{}")
            except Exception:
                item[target] = {}
        items.append(item)
    return items


def _rolling_replay(
    *,
    strategy_id: int,
    model_version_id: int | None,
    dates: list[str],
    end_date: str,
    replay_runner: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    end_index = dates.index(end_date)
    start_index = max(0, end_index - 39)
    return replay_runner(
        strategy_id=int(strategy_id),
        model_version_id=model_version_id,
        start_date=dates[start_index],
        end_date=end_date,
    )


def _daily_trade_count(replay: dict[str, Any], *, trade_date: str) -> int:
    return sum(
        _date_key(item.get("date") or item.get("trade_date")) == _date_key(trade_date)
        for item in (replay.get("trades") or [])
        if isinstance(item, dict)
    )


def _strategy_observation_allowed(version: dict[str, Any]) -> bool:
    """Keep research gates separate from admission to isolated simulation."""
    if bool(version.get("is_active")):
        return False
    if str(version.get("status") or "candidate") not in {
        "candidate", "shadow_observation", "promoted_cooling",
    }:
        return False
    validation = version.get("validation") if isinstance(version.get("validation"), dict) else {}
    review = validation.get("review") if isinstance(validation.get("review"), dict) else {}
    blockers = {
        str(item) for item in (
            validation.get("blocking_reasons")
            or validation.get("blocked_reasons")
            or []
        )
    }
    return (
        str(validation.get("status") or "") not in {
            "rejected", "quick_diagnostic_failed", "shadow_rejected", "rolled_back",
        }
        and str(review.get("status") or "") != "rejected"
        and not blockers.intersection({"operator_rejected", "human_review_rejected", "superseded"})
    )


def run_strategy_shadow_cycle(
    *,
    replay_runner: Callable[..., dict[str, Any]] = run_strategy_replay,
    max_new_sessions_per_candidate: int = 5,
    target_trade_date: str = "",
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    candidate_ledger_persistor: Callable[[int, dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if candidate_ledger_persistor is None:
        from app.services.candidate_simulation_ledger import persist_candidate_replay

        candidate_ledger_persistor = persist_candidate_replay
    target_key = _date_key(target_trade_date)
    active = resolve_production_strategy_champion()
    all_versions = list_model_versions("alis_strategy_challenger", limit=100)
    candidates = [version for version in all_versions if _strategy_observation_allowed(version)]
    # Future shadow is an online observation, not a second formal-OOS run.
    # Candidates reach this stage only after formal OOS, statistics and sealed
    # holdout gates have been frozen in their validation evidence. Requiring
    # the rolling historical OOS window here creates a permanent deadlock when
    # an old slice was first received after its original T-day cutoff.
    #
    # Replay only needs a bounded pre-signal context for the candidate's own
    # daily return. Its point-in-time reads remain checked per observation.
    if not candidates:
        return {
            "ok": True,
            "schema_version": "alis_strategy_shadow_cycle_v3" if target_key else "alis_strategy_shadow_cycle_v2",
            "status": "completed",
            "target_trade_date": target_key,
            "execution_mode": "persistent_one_session" if target_key else "compatibility_batch",
            "candidate_count": 0,
            "results": [],
            "data_contract": "current_experiment_gate_and_candidate_frozen_formal_evidence_v1",
            "production_change_forbidden": True,
        }
    shadow_floors = [
        _date_key((version.get("validation") or {}).get("parallel_shadow_started_after")
                  or (version.get("validation") or {}).get("shadow_started_after"))
        for version in candidates
    ]
    shadow_floors = [value for value in shadow_floors if len(value) == 8 and value.isdigit()]
    # A persisted shadow_passed candidate resumes promotion without another
    # replay and therefore legitimately has no observation start marker.
    replay_end = target_key or max(shadow_floors or [_date_key(datetime.now())])
    replay_start = (datetime.strptime(min(shadow_floors or [replay_end]), "%Y%m%d")
                    - timedelta(days=SHADOW_REPLAY_LOOKBACK_CALENDAR_DAYS)).strftime("%Y%m%d")
    ds = BacktestDataSource()
    dates = ds.get_available_trade_dates(replay_start, replay_end)
    if target_key:
        dates = [date for date in dates if _date_key(date) <= target_key]
    results: list[dict[str, Any]] = []
    promotion_ready: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for candidate in candidates:
        validation = candidate.get("validation") if isinstance(candidate.get("validation"), dict) else {}
        lifecycle_status = str(validation.get("status") or "")
        if lifecycle_status == "shadow_passed":
            stored_gate = validation.get("shadow_gate") if isinstance(validation.get("shadow_gate"), dict) else {}
            if not bool(validation.get("production_eligible")) or not bool(stored_gate.get("passed")):
                raise RuntimeError(f"候选 {candidate['id']} 的 shadow_passed 状态缺少完整晋升证据")
            promotion_ready.append((candidate, stored_gate))
            results.append({
                "model_version_id": int(candidate["id"]),
                "status": "shadow_competition_ready",
                "gate": stored_gate,
                "recovered": True,
            })
            continue
        candidate_config = candidate.get("config") if isinstance(candidate.get("config"), dict) else {}
        candidate_strategy_id = int(candidate_config.get("strategy_id") or 0)
        observations = list_shadow_observations(int(candidate["id"]))
        immediate_observation = lifecycle_status not in {
            "shadow_observation", "shadow_passed", "promoted_cooling",
        }
        if bool(validation.get("parallel_shadow_enabled")) or immediate_observation:
            parallel_floor = _date_key(
                validation.get("parallel_shadow_started_after")
            )
            observations = [
                item for item in observations
                if _date_key(item.get("trade_date")) > parallel_floor
            ]
        if str(validation.get("status") or "") == "shadow_observation":
            shadow_floor = _date_key(validation.get("shadow_started_after"))
            observations = [item for item in observations if _date_key(item.get("trade_date")) > shadow_floor]
        last_date = (
            str(observations[-1].get("trade_date") or "")
            if observations
            else str(
                validation.get("parallel_shadow_started_after")
                or validation.get("shadow_started_after")
                or ""
            )
        )
        if target_key:
            observed_keys = {_date_key(item.get("trade_date")) for item in observations}
            pending_dates = [
                date for date in dates
                if _date_key(date) == target_key and target_key not in observed_keys
            ]
        else:
            pending_dates = [
                date for date in dates if _date_key(date) > _date_key(last_date)
            ][:max(1, min(int(max_new_sessions_per_candidate), 20))]
        reference_version_id = int(validation.get("rollback_target_version_id") or active["champion_id"])
        reference_version = get_model_version(reference_version_id)
        reference_config = reference_version.get("config") if reference_version and isinstance(reference_version.get("config"), dict) else {}
        reference_strategy_id = int(reference_config.get("strategy_id") or active["strategy_id"])
        for trade_date in pending_dates:
            candidate_replay = _rolling_replay(
                strategy_id=candidate_strategy_id,
                model_version_id=int(candidate["id"]),
                dates=dates,
                end_date=trade_date,
                replay_runner=replay_runner,
            )
            champion_replay = _rolling_replay(
                strategy_id=reference_strategy_id,
                model_version_id=None,
                dates=dates,
                end_date=trade_date,
                replay_runner=replay_runner,
            )
            candidate_ledger_audit = candidate_ledger_persistor(
                int(candidate["id"]), candidate_replay,
            )
            if not bool(candidate_ledger_audit.get("ok")):
                raise RuntimeError(
                    f"候选 {candidate['id']} 隔离模拟账本审计失败"
                )
            candidate_metrics = candidate_replay.get("metrics") or {}
            champion_metrics = champion_replay.get("metrics") or {}
            candidate_return, candidate_equity = _replay_daily_return(candidate_replay, trade_date=trade_date)
            champion_return, champion_equity = _replay_daily_return(champion_replay, trade_date=trade_date)
            candidate_pit = bool(((candidate_replay.get("data_quality") or {}).get("point_in_time") or {}).get("passed"))
            champion_pit = bool(((champion_replay.get("data_quality") or {}).get("point_in_time") or {}).get("passed"))
            behavior_passed = not any(
                str(item.get("code") or "") in {"cash_insufficient", "oversell", "empty_snapshot"}
                for item in (candidate_replay.get("blocked_events") or [])
            )
            record_shadow_observation(
                model_version_id=int(candidate["id"]),
                champion_version_id=reference_version_id,
                trade_date=trade_date,
                candidate_return=candidate_return,
                champion_return=champion_return,
                candidate_drawdown=_number(candidate_metrics.get("max_drawdown")),
                champion_drawdown=_number(champion_metrics.get("max_drawdown")),
                point_in_time_passed=candidate_pit and champion_pit,
                behavior_passed=behavior_passed,
                metrics={
                    "return_contract": SHADOW_RETURN_CONTRACT,
                    "candidate_daily_trade_count": _daily_trade_count(candidate_replay, trade_date=trade_date),
                    "champion_daily_trade_count": _daily_trade_count(champion_replay, trade_date=trade_date),
                    "candidate_equity": candidate_equity,
                    "champion_equity": champion_equity,
                    "candidate": candidate_metrics,
                    "champion": champion_metrics,
                    "candidate_summary": candidate_replay.get("summary") or {},
                    "champion_summary": champion_replay.get("summary") or {},
                    "candidate_ledger": candidate_ledger_audit,
                },
            )
            if progress_callback is not None:
                progress_callback({
                    "last_candidate_id": int(candidate["id"]),
                    "last_trade_date": str(trade_date),
                    "target_trade_date": target_key,
                    "observation_persisted": True,
                })
        observations = list_shadow_observations(int(candidate["id"]))
        status = lifecycle_status
        if status == "shadow_observation":
            shadow_floor = _date_key(validation.get("shadow_started_after"))
            observations = [item for item in observations if _date_key(item.get("trade_date")) > shadow_floor]
        if status == "shadow_observation":
            gate = shadow_promotion_gate(observations)
            if gate["passed"]:
                promotion_ready.append((candidate, gate))
                result_status = "shadow_competition_ready"
            elif len(observations) >= SHADOW_MAXIMUM_SESSIONS:
                _merge_validation(
                    int(candidate["id"]),
                    status="shadow_rejected",
                    production_eligible=False,
                    shadow_gate=gate,
                    blocking_reasons=gate["blocking_reasons"],
                    next_gate="automatic_candidate_regeneration",
                )
                update_model_version_status(int(candidate["id"]), status="rejected", is_active=False)
                from app.services.experiment_memory import record_model_experiment_outcome

                record_model_experiment_outcome(
                    candidate,
                    stage="shadow",
                    rejected=True,
                    evidence={"shadow_gate": gate, "blocking_reasons": gate["blocking_reasons"]},
                )
                result_status = "shadow_rejected"
            else:
                _merge_validation(
                    int(candidate["id"]),
                    shadow_observation_sessions=len(observations),
                    shadow_gate=gate,
                )
                result_status = "shadow_observation"
            results.append({"model_version_id": int(candidate["id"]), "status": result_status, "gate": gate})
        elif bool(validation.get("parallel_shadow_enabled")) or immediate_observation:
            _merge_validation(
                int(candidate["id"]),
                parallel_shadow_enabled=True,
                parallel_shadow_observation_sessions=len(observations),
                parallel_shadow_latest_trade_date=(
                    str(observations[-1].get("trade_date") or "")
                    if observations else ""
                ),
            )
            results.append({
                "model_version_id": int(candidate["id"]),
                "status": "parallel_shadow_observation",
                "observation_sessions": len(observations),
            })
        else:
            cooling = cooling_rollback_gate(observations, promoted_after=str(validation.get("promoted_at") or ""))
            if cooling["rollback"]:
                rollback = rollback_strategy_champion(
                    int(validation["rollback_target_version_id"]),
                    evidence={"cooling_gate": cooling, "candidate_model_version_id": int(candidate["id"])},
                )
                _merge_validation(
                    int(candidate["id"]),
                    status="rolled_back",
                    production_eligible=False,
                    cooling_gate=cooling,
                    next_gate="automatic_candidate_regeneration",
                )
                update_model_version_status(int(candidate["id"]), status="rolled_back", is_active=False)
                from app.services.experiment_memory import record_model_experiment_outcome

                record_model_experiment_outcome(
                    candidate,
                    stage="cooling",
                    rejected=True,
                    evidence={"cooling_gate": cooling, "blocking_reasons": cooling["reasons"]},
                )
                _record_promotion_event(
                    "rolled_back",
                    from_id=rollback["from_champion_version_id"],
                    to_id=rollback["to_champion_version_id"],
                    candidate_id=int(candidate["id"]),
                    reason={"codes": cooling["reasons"]},
                    evidence={"cooling_gate": cooling},
                )
                reactivated_reserve = reactivate_best_shadow_reserve(
                    failed_leader_id=int(candidate["id"]),
                    shadow_started_after=str(dates[-1]),
                    versions=all_versions,
                )
                result_status = "rolled_back"
            elif cooling["stable"]:
                _merge_validation(
                    int(candidate["id"]),
                    status="promoted_stable",
                    production_eligible=True,
                    cooling_gate=cooling,
                    next_gate="continuous_monitoring",
                )
                update_model_version_status(int(candidate["id"]), status="promoted", is_active=False)
                result_status = "promoted_stable"
            else:
                _merge_validation(int(candidate["id"]), cooling_gate=cooling)
                result_status = "promoted_cooling"
            result = {"model_version_id": int(candidate["id"]), "status": result_status, "gate": cooling}
            if cooling["rollback"]:
                result["reactivated_reserve"] = reactivated_reserve
            results.append(result)
    if promotion_ready:
        winner, winner_gate = max(
            promotion_ready,
            key=lambda item: _shadow_competition_key({
                **item[0],
                "validation": {**(item[0].get("validation") or {}), "shadow_gate": item[1]},
            }),
        )
        _merge_validation(
            int(winner["id"]),
            status="shadow_passed",
            production_eligible=True,
            shadow_gate=winner_gate,
            next_gate="automatic_promotion",
        )
        promotion = promote_strategy_candidate(int(winner["id"]), evidence={
            "shadow_gate": winner_gate,
            "competition_candidate_ids": [int(item[0]["id"]) for item in promotion_ready],
            "selection_rule": "shadow_daily_equity_outperform_drawdown_formal_excess_v2",
        })
        # Cooling starts after the last observation actually used for the
        # winning candidate, not after the end of the whole available window.
        winner_observations = list_shadow_observations(int(winner["id"]))
        promoted_at = str(winner_observations[-1].get("trade_date") or dates[-1]) if winner_observations else str(dates[-1])
        _merge_validation(
            int(winner["id"]),
            status="promoted_cooling",
            production_eligible=True,
            shadow_gate=winner_gate,
            promoted_at=promoted_at,
            promoted_champion_version_id=promotion["to_champion_version_id"],
            rollback_target_version_id=promotion["from_champion_version_id"],
            cooling_minimum_sessions=COOLING_MINIMUM_SESSIONS,
            next_gate="cooling_observation",
            shadow_competition={
                "winner_model_version_id": int(winner["id"]),
                "candidate_ids": [int(item[0]["id"]) for item in promotion_ready],
                "selection_rule": "shadow_daily_equity_outperform_drawdown_formal_excess_v2",
            },
        )
        update_model_version_status(int(winner["id"]), status="promoted_cooling", is_active=False)
        _record_promotion_event(
            "promoted",
            from_id=promotion["from_champion_version_id"],
            to_id=promotion["to_champion_version_id"],
            candidate_id=int(winner["id"]),
            reason={"code": "formal_oos_shadow_and_competition_passed"},
            evidence={"shadow_gate": winner_gate, "competition_candidate_ids": [int(item[0]["id"]) for item in promotion_ready]},
        )
        for candidate, gate in promotion_ready:
            candidate_id = int(candidate["id"])
            result = next((item for item in results if int(item.get("model_version_id") or 0) == candidate_id), None)
            if candidate_id == int(winner["id"]):
                if result is not None:
                    result["status"] = "promoted_cooling"
                    result["competition_winner"] = True
                continue
            _merge_validation(
                candidate_id,
                status="shadow_reserve",
                production_eligible=False,
                shadow_gate=gate,
                shadow_started_after=promoted_at,
                next_gate="leader_cooling_outcome",
                shadow_competition={
                    "winner_model_version_id": int(winner["id"]),
                    "candidate_ids": [int(item[0]["id"]) for item in promotion_ready],
                    "selection_rule": "shadow_daily_equity_outperform_drawdown_formal_excess_v2",
                },
            )
            update_model_version_status(candidate_id, status="candidate", is_active=False)
            if result is not None:
                result["status"] = "shadow_reserve"
                result["competition_winner"] = False
    return {
        "schema_version": "alis_strategy_shadow_cycle_v3" if target_key else "alis_strategy_shadow_cycle_v2",
        "status": "completed",
        "target_trade_date": target_key,
        "execution_mode": "persistent_one_session" if target_key else "compatibility_batch",
        "candidate_count": len(candidates),
        "results": results,
        "data_contract": "current_experiment_gate_and_candidate_frozen_formal_evidence_v1",
        "replay_window": {"start": replay_start, "end": replay_end},
        "production_change_forbidden": True,
    }
