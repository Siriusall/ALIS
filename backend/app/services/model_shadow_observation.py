from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Callable

import numpy as np

from app.data_platform.db import _conn as data_conn
from app.db.store import (
    get_model_version,
    list_model_versions,
    update_model_version_validation,
)
from app.services.backtest_data_source import BacktestDataSource
from app.services.data_health import assess_latest_decision_data_health
from app.services.plan_model_control import (
    PLAN_MODELS,
    active_production_plan_model_bindings,
    controlled_strategy_ab_passed_current_contract,
)
from app.services.production_strategy import resolve_production_strategy_champion


SHADOW_SCHEMA_VERSION = "alis_model_future_shadow_v1"
MINIMUM_RETURN_SESSIONS = 20
MAXIMUM_RETURN_SESSIONS = 60
MINIMUM_EFFECT_DAYS = 4


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _ymd(value: Any) -> str:
    return str(value or "").replace("-", "")[:8]


def _daily_returns(replay: dict[str, Any]) -> dict[str, float]:
    points = [
        (str(item.get("date") or ""), float(item.get("total_value") or 0.0))
        for item in replay.get("equity_series") or []
        if str(item.get("date") or "") and float(item.get("total_value") or 0.0) > 0
    ]
    return {
        current_date: current_value / prior_value - 1.0
        for (prior_date, prior_value), (current_date, current_value)
        in zip(points, points[1:])
        if prior_date and prior_value > 0
    }


def _paired_bootstrap_lower(
    deltas: list[float],
    *,
    trials: int = 1000,
) -> float | None:
    if len(deltas) < MINIMUM_RETURN_SESSIONS:
        return None
    values = np.asarray(deltas, dtype=float)
    rng = np.random.default_rng(20260730)
    block = min(5, len(values))
    means: list[float] = []
    for _ in range(trials):
        sampled: list[float] = []
        while len(sampled) < len(values):
            start = int(rng.integers(0, max(1, len(values) - block + 1)))
            sampled.extend(values[start:start + block].tolist())
        means.append(float(np.mean(sampled[:len(values)])))
    return float(np.quantile(means, 0.025))


def _next_trade_date(trade_date: str) -> str:
    with data_conn() as conn:
        row = conn.execute(
            """SELECT MIN(cal_date) AS trade_date
               FROM dc_trade_calendar
               WHERE is_open=1 AND cal_date>?""",
            (_ymd(trade_date),),
        ).fetchone()
    return _ymd(dict(row).get("trade_date")) if row else ""


def _latest_ready_trade_date() -> tuple[str, dict[str, Any]]:
    health = assess_latest_decision_data_health()
    if not bool(health.get("experiment_ready")):
        return "", health
    latest = health.get("latest_snapshot") if isinstance(
        health.get("latest_snapshot"), dict
    ) else {}
    readiness = health.get("readiness") if isinstance(
        health.get("readiness"), dict
    ) else {}
    trade_date = _ymd(
        latest.get("trade_date")
        or readiness.get("latest_decision_ready_trade_date")
        or readiness.get("target_trade_date")
    )
    return trade_date, health


def _binding(version: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(version["id"]),
        "model_key": str(version.get("model_key") or ""),
        "version": str(version.get("version") or ""),
        "status": str(version.get("status") or ""),
        "production_eligible": False,
    }


def _binding_ids(bindings: dict[str, Any]) -> dict[str, int | None]:
    return {
        key: (
            int(value.get("id") or 0) or None
            if isinstance(value, dict)
            else None
        )
        for key, value in bindings.items()
    }


def _research_shadow_only(version: dict[str, Any]) -> bool:
    context = (version.get("metrics") or {}).get("targeted_training_context")
    return bool(
        str(version.get("model_key") or "")
        == "narrative_transition_multitask_v1"
        and isinstance(context, dict)
        and str(context.get("evaluation_scope") or "")
        == "research_shadow_only"
        and bool(context.get("formal_promotion_forbidden"))
    )


def _reference_bindings(model_key: str) -> dict[str, Any]:
    bindings = active_production_plan_model_bindings()
    bindings[model_key] = None
    return bindings


def _contract(
    version: dict[str, Any],
    *,
    reference_bindings: dict[str, Any],
) -> dict[str, Any]:
    champion = resolve_production_strategy_champion()
    strategy = champion.get("strategy") if isinstance(
        champion.get("strategy"), dict
    ) else {}
    contract = {
        "schema_version": SHADOW_SCHEMA_VERSION,
        "champion_id": int(champion["champion_id"]),
        "strategy_id": int(champion["strategy_id"]),
        "strategy_name": str(strategy.get("name") or ""),
        "strategy_content": strategy.get("content") or {},
        "strategy_controller_version_id": int(
            champion["strategy_controller_version_id"]
        ),
        "factor_bindings": dict(champion.get("factor_bindings") or {}),
        "reference_model_bindings": reference_bindings,
        "candidate_model_binding": _binding(version),
        "target_model_reference": "disabled_ablation",
        "market_state_binding": champion.get("market_state_binding") or {},
        "portfolio_optimizer": champion.get("portfolio_optimizer") or {},
        "execution_contract": champion.get("execution_contract") or {},
    }
    contract["contract_hash"] = hashlib.sha256(
        json.dumps(
            contract,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode()
    ).hexdigest()
    return contract


def _persist_shadow(
    version: dict[str, Any],
    shadow: dict[str, Any],
    *,
    status: str,
    live_gate: bool,
    blocked_reasons: list[str],
) -> dict[str, Any]:
    latest = get_model_version(int(version["id"])) or version
    validation = dict(latest.get("validation") or {})
    eligibility = dict(validation.get("production_eligibility") or {})
    gates = dict(eligibility.get("gates") or {})
    research_only = _research_shadow_only(latest)
    gates["live_observation_20_sessions"] = bool(live_gate and not research_only)
    gates["simulation_only_live_observation_waived"] = False
    if research_only:
        gates["fresh_unconsumed_narrative_formal_oos_cohort"] = False
        gates["research_shadow_not_production_evidence"] = False
        blocked_reasons = list(dict.fromkeys([
            *blocked_reasons,
            "waiting_for_complete_new_126_session_oos_cohort",
            "research_shadow_not_production_evidence",
        ]))
    eligibility.update({
        "eligible": False,
        "gates": gates,
        "blocked_reasons": list(dict.fromkeys(blocked_reasons)),
    })
    validation.update({
        "status": status,
        "production_eligible": False,
        "production_eligibility": eligibility,
        "live_observation_sessions": int(
            shadow.get("observed_return_sessions") or 0
        ),
        "model_shadow": shadow,
    })
    update_model_version_validation(int(version["id"]), validation=validation)
    return shadow


def enroll_model_shadow_candidate(
    model_version_id: int,
    *,
    force_reset: bool = False,
) -> dict[str, Any]:
    version = get_model_version(int(model_version_id))
    if not version:
        return {"ok": False, "status": "blocked_by_missing_model_version"}
    model_key = str(version.get("model_key") or "")
    if model_key not in PLAN_MODELS:
        return {"ok": False, "status": "blocked_by_model_family"}
    research_only = _research_shadow_only(version)
    impact = (version.get("metrics") or {}).get("strategy_impact_ab")
    if (
        not research_only
        and not controlled_strategy_ab_passed_current_contract(model_key, impact)
    ):
        return {
            "ok": False,
            "status": "blocked_by_controlled_strategy_ab",
            "model_version_id": int(model_version_id),
        }
    validation = dict(version.get("validation") or {})
    existing = validation.get("model_shadow") if isinstance(
        validation.get("model_shadow"), dict
    ) else {}
    reference_bindings = _reference_bindings(model_key)
    contract = _contract(version, reference_bindings=reference_bindings)
    if (
        existing
        and not force_reset
        and str(existing.get("contract_hash") or "") == contract["contract_hash"]
        and str(existing.get("status") or "") not in {"invalidated"}
    ):
        return {"ok": True, "status": "already_enrolled", "shadow": existing}

    latest_ready, health = _latest_ready_trade_date()
    signal_start = _next_trade_date(latest_ready) if latest_ready else ""
    shadow = {
        "ok": True,
        "schema_version": SHADOW_SCHEMA_VERSION,
        "status": (
            "awaiting_first_future_signal"
            if signal_start
            else "blocked_by_decision_data"
        ),
        "model_version_id": int(version["id"]),
        "model_key": model_key,
        "version": str(version.get("version") or ""),
        "enrolled_at": _now(),
        "enrolled_after_trade_date": latest_ready or None,
        "signal_start_trade_date": signal_start or None,
        "required_return_sessions": MINIMUM_RETURN_SESSIONS,
        "maximum_return_sessions": MAXIMUM_RETURN_SESSIONS,
        "minimum_effect_days": MINIMUM_EFFECT_DAYS,
        "observed_return_sessions": 0,
        "last_observed_trade_date": None,
        "observations": [],
        "contract": contract,
        "contract_hash": contract["contract_hash"],
        "reference_binding_ids": _binding_ids(reference_bindings),
        "candidate_binding_id": int(version["id"]),
        "data_gate": {
            "status": health.get("status"),
            "experiment_ready": bool(health.get("experiment_ready")),
            "latest_ready_trade_date": latest_ready or None,
        },
        "reset_reason": "reference_model_bundle_changed" if force_reset else None,
        "production_change_forbidden": True,
        "scope": (
            "research_prediction_shadow"
            if research_only
            else "candidate_future_shadow"
        ),
        "formal_promotion_forbidden": research_only,
        "formal_promotion_block_reason": (
            "waiting_for_complete_new_126_session_oos_cohort"
            if research_only
            else None
        ),
    }
    _persist_shadow(
        version,
        shadow,
        status=(
            "research_shadow_observing"
            if research_only
            else "pending_live_observation"
        ),
        live_gate=False,
        blocked_reasons=["live_observation_20_sessions"],
    )
    return {"ok": True, "status": shadow["status"], "shadow": shadow}


def _model_effect_days(
    replay: dict[str, Any],
    *,
    model_key: str,
) -> tuple[int, int]:
    consumed = 0
    effective = 0
    for day in replay.get("model_fusion_history") or []:
        models = day.get("production_models") if isinstance(day, dict) else []
        item = next(
            (
                value for value in models or []
                if isinstance(value, dict)
                and str(value.get("model_key") or "") == model_key
            ),
            None,
        )
        if not item or not bool(item.get("consumed")):
            continue
        consumed += 1
        if bool(item.get("effective")):
            effective += 1
    return consumed, effective


def _run_pair(
    shadow: dict[str, Any],
    *,
    through_trade_date: str,
    progress_callback: Callable[[dict[str, Any]], None] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from app.services.strategy_replay import run_strategy_replay

    contract = shadow["contract"]
    common = {
        "strategy_id": int(contract["strategy_id"]),
        "start_date": str(shadow["signal_start_trade_date"]),
        "end_date": str(through_trade_date),
        "factor_bindings": dict(contract.get("factor_bindings") or {}),
        "market_state_binding": dict(contract.get("market_state_binding") or {}),
        "strategy_controller_version_id": int(
            contract["strategy_controller_version_id"]
        ),
        "strategy_content": contract.get("strategy_content") or {},
        "strategy_name": str(contract.get("strategy_name") or ""),
        "research_package_hash": str(contract["contract_hash"]),
    }

    def report(side: str, offset: float, scale: float) -> Callable[[dict[str, Any]], None]:
        def callback(value: dict[str, Any]) -> None:
            if not progress_callback:
                return
            progress_callback({
                **value,
                "side": side,
                "progress": round(
                    offset + scale * float(value.get("progress") or 0.0),
                    6,
                ),
            })

        return callback

    reference = run_strategy_replay(
        **common,
        model_bindings=dict(contract.get("reference_model_bindings") or {}),
        data_source=BacktestDataSource(),
        progress_callback=report("reference", 0.05, 0.40),
    )
    candidate_bindings = dict(contract.get("reference_model_bindings") or {})
    model_key = str(shadow["model_key"])
    candidate_bindings[model_key] = dict(
        contract.get("candidate_model_binding") or {}
    )
    candidate = run_strategy_replay(
        **common,
        model_bindings=candidate_bindings,
        data_source=BacktestDataSource(),
        progress_callback=report("candidate", 0.50, 0.45),
    )
    return reference, candidate


def advance_model_shadow_candidate(
    model_version_id: int,
    *,
    through_trade_date: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    version = get_model_version(int(model_version_id))
    if not version:
        return {"ok": False, "status": "blocked_by_missing_model_version"}
    validation = dict(version.get("validation") or {})
    research_only = _research_shadow_only(version)
    shadow = validation.get("model_shadow") if isinstance(
        validation.get("model_shadow"), dict
    ) else {}
    if not shadow:
        enrolled = enroll_model_shadow_candidate(int(model_version_id))
        shadow = enrolled.get("shadow") if isinstance(
            enrolled.get("shadow"), dict
        ) else {}
    if not shadow or str(shadow.get("status") or "") in {"passed", "failed"}:
        return {
            "ok": bool(shadow),
            "status": str(shadow.get("status") or "not_enrolled"),
            "shadow": shadow,
        }

    current_references = _reference_bindings(str(version.get("model_key") or ""))
    if _binding_ids(current_references) != dict(
        shadow.get("reference_binding_ids") or {}
    ):
        return enroll_model_shadow_candidate(
            int(model_version_id),
            force_reset=True,
        )

    latest_ready, health = _latest_ready_trade_date()
    through = _ymd(through_trade_date or latest_ready)
    if (
        not through
        or not bool(health.get("experiment_ready"))
        or through > latest_ready
    ):
        shadow["data_gate"] = {
            "status": health.get("status"),
            "experiment_ready": bool(health.get("experiment_ready")),
            "latest_ready_trade_date": latest_ready or None,
        }
        _persist_shadow(
            version,
            shadow,
            status="pending_live_observation",
            live_gate=False,
            blocked_reasons=["live_observation_20_sessions"],
        )
        return {"ok": True, "status": "blocked_by_decision_data", "shadow": shadow}

    start = _ymd(shadow.get("signal_start_trade_date"))
    dates = BacktestDataSource().get_available_trade_dates(start, through)
    observed_sessions = max(0, len(dates) - 1)
    if observed_sessions <= 0:
        return {
            "ok": True,
            "status": "awaiting_realized_future_return",
            "observed_return_sessions": 0,
            "shadow": shadow,
        }
    if (
        _ymd(shadow.get("last_observed_trade_date")) == through
        and int(shadow.get("observed_return_sessions") or 0) == observed_sessions
    ):
        return {"ok": True, "status": "already_observed", "shadow": shadow}

    reference, candidate = _run_pair(
        shadow,
        through_trade_date=through,
        progress_callback=progress_callback,
    )
    reference_returns = _daily_returns(reference)
    candidate_returns = _daily_returns(candidate)
    common_dates = sorted(set(reference_returns) & set(candidate_returns))
    deltas = [
        candidate_returns[date] - reference_returns[date]
        for date in common_dates
    ]
    reference_metrics = dict(reference.get("metrics") or {})
    candidate_metrics = dict(candidate.get("metrics") or {})
    consumed_days, effect_days = _model_effect_days(
        candidate,
        model_key=str(shadow["model_key"]),
    )
    bootstrap_lower = _paired_bootstrap_lower(deltas)
    return_delta = (
        float(candidate_metrics.get("total_return") or 0.0)
        - float(reference_metrics.get("total_return") or 0.0)
    )
    enough_sessions = observed_sessions >= MINIMUM_RETURN_SESSIONS
    gates = {
        "minimum_future_return_sessions": enough_sessions,
        "all_replays_completed": (
            str(reference.get("status") or "") == "completed"
            and str(candidate.get("status") or "") == "completed"
        ),
        "paired_daily_returns_complete": len(common_dates) >= observed_sessions,
        "candidate_model_consumed": consumed_days >= observed_sessions,
        "minimum_decision_effect_days": effect_days >= MINIMUM_EFFECT_DAYS,
        "cumulative_return_delta_positive": return_delta > 0,
        "mean_daily_return_delta_positive": (
            bool(deltas) and float(np.mean(deltas)) > 0
        ),
        "paired_daily_bootstrap_lower_positive": (
            bootstrap_lower is not None and bootstrap_lower > 0
        ),
        "drawdown_not_materially_worse": (
            float(candidate_metrics.get("max_drawdown") or 0.0)
            >= float(reference_metrics.get("max_drawdown") or 0.0) - 0.02
        ),
        "turnover_not_explosive": (
            float(candidate_metrics.get("turnover_rate") or 0.0)
            <= max(
                0.05,
                float(reference_metrics.get("turnover_rate") or 0.0) * 1.5,
            )
        ),
    }
    decision_gates = {
        key: value
        for key, value in gates.items()
        if key != "minimum_future_return_sessions"
    }
    if enough_sessions and all(decision_gates.values()):
        status = "passed"
    elif observed_sessions >= MAXIMUM_RETURN_SESSIONS:
        status = "failed"
    elif enough_sessions:
        status = "extended_for_inconclusive_evidence"
    else:
        status = "observing"

    observations = [
        item for item in shadow.get("observations") or []
        if _ymd(item.get("through_trade_date")) != through
    ]
    observations.append({
        "through_trade_date": through,
        "observed_return_sessions": observed_sessions,
        "paired_daily_observations": len(common_dates),
        "candidate_model_consumed_days": consumed_days,
        "candidate_model_effect_days": effect_days,
        "reference_total_return": round(
            float(reference_metrics.get("total_return") or 0.0),
            8,
        ),
        "candidate_total_return": round(
            float(candidate_metrics.get("total_return") or 0.0),
            8,
        ),
        "return_delta": round(return_delta, 8),
        "mean_daily_return_delta": (
            round(float(np.mean(deltas)), 8) if deltas else None
        ),
        "paired_bootstrap_95_lower": (
            round(float(bootstrap_lower), 8)
            if bootstrap_lower is not None
            else None
        ),
        "gates": gates,
        "status": status,
        "recorded_at": _now(),
    })
    shadow.update({
        "status": status,
        "observed_return_sessions": observed_sessions,
        "last_observed_trade_date": through,
        "observations": observations[-MAXIMUM_RETURN_SESSIONS:],
        "latest_evidence": observations[-1],
        "data_gate": {
            "status": health.get("status"),
            "experiment_ready": True,
            "latest_ready_trade_date": latest_ready,
        },
        "updated_at": _now(),
    })
    blocked = [] if status == "passed" else (
        [key for key, passed in gates.items() if not passed]
        if status == "failed"
        else ["live_observation_20_sessions"]
    )
    _persist_shadow(
        version,
        shadow,
        status=(
            "research_shadow_complete"
            if research_only and status in {"passed", "failed"}
            else "research_shadow_observing"
            if research_only
            else "shadow_passed"
            if status == "passed"
            else "shadow_failed"
            if status == "failed"
            else "pending_live_observation"
        ),
        live_gate=status == "passed" and not research_only,
        blocked_reasons=blocked,
    )
    if progress_callback:
        progress_callback({
            "phase": "model_shadow_observation",
            "progress": 0.98,
            "message": (
                f"模型未来 Shadow 已累计 {observed_sessions}/"
                f"{MINIMUM_RETURN_SESSIONS} 个真实收益日"
            ),
            "observed_return_sessions": observed_sessions,
            "through_trade_date": through,
        })
    return {
        "ok": True,
        "schema_version": SHADOW_SCHEMA_VERSION,
        "status": status,
        "model_version_id": int(model_version_id),
        "observed_return_sessions": observed_sessions,
        "through_trade_date": through,
        "gates": gates,
        "shadow": shadow,
        "production_change_forbidden": True,
    }


def ensure_model_shadow_tasks() -> dict[str, Any]:
    latest_ready, health = _latest_ready_trade_date()
    enrolled: list[dict[str, Any]] = []
    queued: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if not latest_ready or not bool(health.get("experiment_ready")):
        return {
            "ok": True,
            "status": "blocked_by_decision_data",
            "latest_ready_trade_date": latest_ready or None,
            "enrolled": enrolled,
            "queued": queued,
            "skipped": skipped,
        }
    from app.services.evolution_orchestrator import create_evolution_task

    for model_key in PLAN_MODELS:
        versions = list_model_versions(model_key, limit=10)
        version = next(
            (
                item for item in versions
                if str(item.get("status") or "") == "candidate"
                and (
                    _research_shadow_only(item)
                    or str(
                        (
                            (item.get("metrics") or {}).get("strategy_impact_ab")
                            or {}
                        ).get("status")
                        or ""
                    )
                    == "passed"
                )
                and str(
                    (
                        (item.get("validation") or {}).get("review")
                        or {}
                    ).get("status")
                    or ""
                )
                != "rejected"
            ),
            {},
        )
        impact = (version.get("metrics") or {}).get("strategy_impact_ab")
        if (
            not version
            or str(version.get("status") or "") != "candidate"
            or (
                not _research_shadow_only(version)
                and not controlled_strategy_ab_passed_current_contract(
                    model_key,
                    impact,
                )
            )
        ):
            skipped.append({"model_key": model_key, "reason": "no_ab_passed_candidate"})
            continue
        validation = dict(version.get("validation") or {})
        shadow = validation.get("model_shadow") if isinstance(
            validation.get("model_shadow"), dict
        ) else {}
        current_references = _reference_bindings(model_key)
        if (
            shadow
            and _binding_ids(current_references)
            != dict(shadow.get("reference_binding_ids") or {})
        ):
            result = enroll_model_shadow_candidate(
                int(version["id"]),
                force_reset=True,
            )
            shadow = result.get("shadow") or {}
            enrolled.append({
                "model_key": model_key,
                "model_version_id": int(version["id"]),
                "status": result.get("status"),
                "reason": "reference_model_bundle_changed",
            })
        elif not shadow:
            result = enroll_model_shadow_candidate(int(version["id"]))
            shadow = result.get("shadow") or {}
            enrolled.append({
                "model_key": model_key,
                "model_version_id": int(version["id"]),
                "status": result.get("status"),
                "reason": (
                    "research_shadow_candidate_enrolled"
                    if _research_shadow_only(version)
                    else "ab_passed_candidate_enrolled"
                ),
            })
        if str(shadow.get("status") or "") in {"passed", "failed"}:
            skipped.append({"model_key": model_key, "reason": shadow["status"]})
            continue
        signal_start = _ymd(shadow.get("signal_start_trade_date"))
        if not signal_start or latest_ready <= signal_start:
            skipped.append({
                "model_key": model_key,
                "reason": "awaiting_first_realized_future_return",
                "signal_start_trade_date": signal_start or None,
            })
            continue
        if _ymd(shadow.get("last_observed_trade_date")) == latest_ready:
            skipped.append({"model_key": model_key, "reason": "latest_date_already_observed"})
            continue
        task = create_evolution_task(
            task_type="model_shadow_observation",
            payload={
                "model_key": model_key,
                "model_version_id": int(version["id"]),
                "through_trade_date": latest_ready,
                "production_change_forbidden": True,
            },
            dedupe_key=(
                f"model_shadow_observation:v1:{int(version['id'])}:"
                f"{latest_ready}"
            ),
            priority=68,
        )
        queued.append({
            "model_key": model_key,
            "model_version_id": int(version["id"]),
            "task_id": int(task.get("id") or 0),
            "status": str(task.get("status") or "queued"),
            "through_trade_date": latest_ready,
        })
    return {
        "ok": True,
        "status": "ready",
        "latest_ready_trade_date": latest_ready,
        "enrolled": enrolled,
        "queued": queued,
        "skipped": skipped,
    }
