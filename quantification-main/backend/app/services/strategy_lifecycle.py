from __future__ import annotations

from datetime import datetime
import json
from math import prod
from typing import Any, Callable

from app.data_platform.services.oos_hard_data import audit_oos_hard_data
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


SHADOW_MINIMUM_SESSIONS = 20
SHADOW_MAXIMUM_SESSIONS = 60
COOLING_MINIMUM_SESSIONS = 20


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _date_key(value: Any) -> str:
    return str(value or "").replace("-", "")[:8]


def _merge_validation(model_version_id: int, **updates: Any) -> dict[str, Any]:
    version = get_model_version(int(model_version_id)) or {}
    current = version.get("validation") if isinstance(version.get("validation"), dict) else {}
    merged = {**current, **updates}
    update_model_version_validation(int(model_version_id), validation=merged)
    return merged


def formal_candidate_gate(
    candidate: dict[str, Any],
    champion: dict[str, Any] | None,
) -> dict[str, Any]:
    candidate_summary = candidate.get("summary") if isinstance(candidate.get("summary"), dict) else {}
    champion_summary = champion.get("summary") if champion and isinstance(champion.get("summary"), dict) else {}
    candidate_return = _number(candidate_summary.get("aggregate_cost_after_return"))
    champion_return = _number(champion_summary.get("aggregate_cost_after_return"), -1.0)
    candidate_drawdown = _number(candidate_summary.get("worst_window_drawdown"), -1.0)
    champion_drawdown = _number(champion_summary.get("worst_window_drawdown"), -1.0)
    candidate_fees = _number(candidate_summary.get("total_fees"))
    champion_fees = _number(champion_summary.get("total_fees"))
    gates = {
        "formal_oos_protocol_passed": str(candidate.get("status") or "") == "passed",
        "return_not_worse_than_champion": candidate_return >= champion_return,
        "drawdown_not_materially_worse_than_champion": candidate_drawdown >= champion_drawdown - 0.02,
        "fees_bounded_vs_champion": champion_fees <= 0 or candidate_fees <= max(champion_fees * 1.5, champion_fees + 500.0),
    }
    return {
        "passed": all(gates.values()),
        "gates": gates,
        "blocking_reasons": [key for key, passed in gates.items() if not passed],
        "comparison": {
            "candidate_return": candidate_return,
            "champion_return": champion_return,
            "candidate_drawdown": candidate_drawdown,
            "champion_drawdown": champion_drawdown,
            "candidate_fees": candidate_fees,
            "champion_fees": champion_fees,
        },
    }


def advance_formal_candidate_lifecycle(
    items: list[dict[str, Any]],
    *,
    shadow_start_date: str,
) -> list[dict[str, Any]]:
    champion = next((item for item in items if item.get("role") == "champion"), None)
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
        gate = formal_candidate_gate(item, champion)
        model_version_id = int(item["model_version_id"])
        if gate["passed"]:
            _merge_validation(
                model_version_id,
                status="shadow_observation",
                production_eligible=False,
                formal_comparison=gate,
                shadow_started_after=shadow_start_date,
                shadow_minimum_sessions=SHADOW_MINIMUM_SESSIONS,
                shadow_observation_sessions=0,
                next_gate="future_shadow_observation",
                blocking_reasons=[],
            )
            update_model_version_status(model_version_id, status="shadow_observation", is_active=False)
            status = "shadow_observation"
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


def shadow_promotion_gate(
    observations: list[dict[str, Any]],
    *,
    minimum_sessions: int = SHADOW_MINIMUM_SESSIONS,
) -> dict[str, Any]:
    sample = observations[-max(minimum_sessions, SHADOW_MAXIMUM_SESSIONS):]
    session_count = len(sample)
    deltas = [_number(item.get("excess_delta")) for item in sample]
    compounded_delta = prod(1.0 + value for value in deltas) - 1.0 if deltas else 0.0
    outperform_ratio = sum(value > 0 for value in deltas) / session_count if session_count else 0.0
    worst_drawdown_gap = min(
        (_number(item.get("candidate_drawdown")) - _number(item.get("champion_drawdown")) for item in sample),
        default=-1.0,
    )
    gates = {
        "minimum_future_sessions": session_count >= minimum_sessions,
        "point_in_time_all_passed": bool(sample) and all(bool(item.get("point_in_time_passed")) for item in sample),
        "behavior_all_passed": bool(sample) and all(bool(item.get("behavior_passed")) for item in sample),
        "shadow_compounded_delta_positive": compounded_delta > 0,
        "majority_shadow_sessions_outperform": outperform_ratio >= 0.55,
        "shadow_drawdown_not_materially_worse": worst_drawdown_gap >= -0.02,
    }
    return {
        "passed": all(gates.values()),
        "session_count": session_count,
        "minimum_sessions": minimum_sessions,
        "compounded_delta": round(compounded_delta, 8),
        "outperform_ratio": round(outperform_ratio, 8),
        "worst_drawdown_gap": round(worst_drawdown_gap, 8),
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
    deltas = [_number(item.get("excess_delta")) for item in recent]
    compounded_delta = prod(1.0 + value for value in deltas) - 1.0 if deltas else 0.0
    worst_drawdown_gap = min(
        (_number(item.get("candidate_drawdown")) - _number(item.get("champion_drawdown")) for item in recent),
        default=0.0,
    )
    reasons: list[str] = []
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


def run_strategy_shadow_cycle(
    *,
    replay_runner: Callable[..., dict[str, Any]] = run_strategy_replay,
    max_new_sessions_per_candidate: int = 5,
) -> dict[str, Any]:
    audit = audit_oos_hard_data(include_dates=False)
    if audit.get("status") != "ready":
        raise RuntimeError("OOS 永久硬数据未就绪，影子观察冻结")
    ds = BacktestDataSource()
    dates = ds.get_available_trade_dates(str(audit["window_start"]), str(audit["window_end"]))
    active = resolve_production_strategy_champion()
    candidates = [
        version for version in list_model_versions("alis_strategy_challenger", limit=100)
        if str((version.get("validation") or {}).get("status") or "")
        in {"shadow_observation", "promoted_cooling"}
    ]
    results: list[dict[str, Any]] = []
    for candidate in candidates:
        validation = candidate.get("validation") if isinstance(candidate.get("validation"), dict) else {}
        candidate_config = candidate.get("config") if isinstance(candidate.get("config"), dict) else {}
        candidate_strategy_id = int(candidate_config.get("strategy_id") or 0)
        observations = list_shadow_observations(int(candidate["id"]))
        last_date = str(observations[-1].get("trade_date") or "") if observations else str(validation.get("shadow_started_after") or "")
        pending_dates = [
            date for date in dates if _date_key(date) > _date_key(last_date)
        ][:max(1, int(max_new_sessions_per_candidate))]
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
            candidate_metrics = candidate_replay.get("metrics") or {}
            champion_metrics = champion_replay.get("metrics") or {}
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
                candidate_return=_number(candidate_metrics.get("total_return")),
                champion_return=_number(champion_metrics.get("total_return")),
                candidate_drawdown=_number(candidate_metrics.get("max_drawdown")),
                champion_drawdown=_number(champion_metrics.get("max_drawdown")),
                point_in_time_passed=candidate_pit and champion_pit,
                behavior_passed=behavior_passed,
                metrics={
                    "candidate": candidate_metrics,
                    "champion": champion_metrics,
                    "candidate_summary": candidate_replay.get("summary") or {},
                    "champion_summary": champion_replay.get("summary") or {},
                },
            )
        observations = list_shadow_observations(int(candidate["id"]))
        status = str(validation.get("status") or "")
        if status == "shadow_observation":
            gate = shadow_promotion_gate(observations)
            if gate["passed"]:
                _merge_validation(
                    int(candidate["id"]),
                    status="shadow_passed",
                    production_eligible=True,
                    shadow_gate=gate,
                    next_gate="automatic_promotion",
                )
                promotion = promote_strategy_candidate(int(candidate["id"]), evidence={"shadow_gate": gate})
                promoted_at = str(dates[-1])
                _merge_validation(
                    int(candidate["id"]),
                    status="promoted_cooling",
                    production_eligible=True,
                    shadow_gate=gate,
                    promoted_at=promoted_at,
                    promoted_champion_version_id=promotion["to_champion_version_id"],
                    rollback_target_version_id=promotion["from_champion_version_id"],
                    cooling_minimum_sessions=COOLING_MINIMUM_SESSIONS,
                    next_gate="cooling_observation",
                )
                update_model_version_status(int(candidate["id"]), status="promoted_cooling", is_active=False)
                _record_promotion_event(
                    "promoted",
                    from_id=promotion["from_champion_version_id"],
                    to_id=promotion["to_champion_version_id"],
                    candidate_id=int(candidate["id"]),
                    reason={"code": "formal_oos_and_shadow_passed"},
                    evidence={"shadow_gate": gate},
                )
                result_status = "promoted_cooling"
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
                result_status = "shadow_rejected"
            else:
                _merge_validation(
                    int(candidate["id"]),
                    shadow_observation_sessions=len(observations),
                    shadow_gate=gate,
                )
                result_status = "shadow_observation"
            results.append({"model_version_id": int(candidate["id"]), "status": result_status, "gate": gate})
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
                _record_promotion_event(
                    "rolled_back",
                    from_id=rollback["from_champion_version_id"],
                    to_id=rollback["to_champion_version_id"],
                    candidate_id=int(candidate["id"]),
                    reason={"codes": cooling["reasons"]},
                    evidence={"cooling_gate": cooling},
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
            results.append({"model_version_id": int(candidate["id"]), "status": result_status, "gate": cooling})
    return {
        "schema_version": "alis_strategy_shadow_cycle_v1",
        "status": "completed",
        "hard_data_end": str(audit["window_end"]),
        "candidate_count": len(candidates),
        "results": results,
    }
