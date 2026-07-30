from __future__ import annotations

import hashlib
import json
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from typing import Any, Callable

import numpy as np

from app.db.store import (
    get_model_version,
    list_model_versions,
    update_model_version_metrics,
    update_model_version_validation,
)
from app.services.backtest_data_source import BacktestDataSource
from app.services.factor_platform import registered_factor_bindings
from app.services.plan_model_control import (
    MODEL_EVALUATION_CONTRACT,
    NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT,
    PLAN_MODELS,
    active_production_plan_model_bindings,
)
from app.services.production_strategy import resolve_production_strategy_champion


NARRATIVE_MODEL_KEY = "narrative_transition_multitask_v1"
DEFAULT_MODEL_EFFECT_CONTRACT = "weighted_model_signal_fusion_v1"


def _model_effect_contract(model_key: str) -> str:
    if model_key == NARRATIVE_MODEL_KEY:
        return NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT
    return DEFAULT_MODEL_EFFECT_CONTRACT


def _compact_replay_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "metrics": result.get("metrics") or {},
        "equity_series": result.get("equity_series") or [],
        "model_fusion_history": result.get("model_fusion_history") or [],
    }


def _run_default_replay_pair(job: dict[str, Any]) -> dict[str, Any]:
    """Run one independent OOS fold in a spawned process."""

    from app.services.strategy_replay import run_strategy_replay

    common = dict(job["common"])
    reference = run_strategy_replay(
        **common,
        model_bindings=job["reference_bindings"],
        data_source=BacktestDataSource(),
    )
    challenger = run_strategy_replay(
        **common,
        model_bindings=job["candidate_bindings"],
        data_source=BacktestDataSource(),
    )
    return {
        "reference": _compact_replay_result(reference),
        "challenger": _compact_replay_result(challenger),
    }


def _model_binding(version: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(version["id"]),
        "model_key": str(version.get("model_key") or ""),
        "version": str(version.get("version") or ""),
        "status": str(version.get("status") or ""),
        "production_eligible": False,
    }


def _fold_binding(
    version: dict[str, Any],
    window: dict[str, Any],
) -> dict[str, Any]:
    frozen = window.get("point_in_time_artifact")
    if not isinstance(frozen, dict):
        raise ValueError("missing_point_in_time_fold_artifact")
    required = {
        "binding_contract",
        "fold",
        "artifact_path",
        "model_sha256",
        "available_at",
        "validation_end",
        "test_start",
        "test_end",
    }
    if (
        not required.issubset(frozen)
        or str(frozen.get("binding_contract") or "")
        != "point_in_time_fold_model_v1"
        or int(frozen.get("fold") or 0) != int(window.get("fold") or 0)
        or str(frozen.get("test_start") or "") != str(window.get("test_start") or "")
        or str(frozen.get("test_end") or "") != str(window.get("test_end") or "")
        or str(frozen.get("validation_end") or "")
        >= str(frozen.get("test_start") or "")
    ):
        raise ValueError("invalid_point_in_time_fold_artifact")
    return {
        **_model_binding(version),
        "point_in_time_fold_artifact": dict(frozen),
    }


def _daily_returns(replay: dict[str, Any]) -> dict[str, float]:
    points = [
        (str(item.get("date") or ""), float(item.get("total_value") or 0.0))
        for item in replay.get("equity_series") or []
        if str(item.get("date") or "") and float(item.get("total_value") or 0.0) > 0
    ]
    result: dict[str, float] = {}
    for index in range(1, len(points)):
        prior_date, prior_value = points[index - 1]
        current_date, current_value = points[index]
        if prior_value > 0:
            result[current_date] = current_value / prior_value - 1.0
    return result


def _paired_bootstrap_lower(deltas: list[float], *, trials: int = 1000) -> float | None:
    if len(deltas) < 20:
        return None
    values = np.asarray(deltas, dtype=float)
    rng = np.random.default_rng(20260728)
    block = min(5, len(values))
    means = []
    for _ in range(trials):
        sampled: list[float] = []
        while len(sampled) < len(values):
            start = int(rng.integers(0, max(1, len(values) - block + 1)))
            sampled.extend(values[start:start + block].tolist())
        means.append(float(np.mean(sampled[:len(values)])))
    return float(np.quantile(means, 0.025))


def _persist_blocked_validation(
    candidate: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    metrics = dict(candidate.get("metrics") or {})
    metrics["strategy_impact_ab"] = result
    update_model_version_metrics(int(candidate["id"]), metrics=metrics)
    validation = dict(candidate.get("validation") or {})
    eligibility = dict(validation.get("production_eligibility") or {})
    gates = dict(eligibility.get("gates") or {})
    gates["controlled_strategy_ab"] = False
    eligibility["gates"] = gates
    eligibility["eligible"] = False
    eligibility["blocked_reasons"] = sorted(set([
        *list(eligibility.get("blocked_reasons") or []),
        str(result["status"]),
    ]))
    validation["status"] = "failed"
    validation["production_eligible"] = False
    validation["production_eligibility"] = eligibility
    update_model_version_validation(int(candidate["id"]), validation=validation)
    return result


def _fold_evidence(
    job: dict[str, Any],
    pair: dict[str, Any],
    *,
    model_key: str,
) -> tuple[dict[str, Any], list[float]]:
    window = job["window"]
    fold_binding = job["fold_binding"]
    reference = pair["reference"]
    challenger = pair["challenger"]
    ref_metrics = dict(reference.get("metrics") or {})
    cand_metrics = dict(challenger.get("metrics") or {})
    ref_daily = _daily_returns(reference)
    cand_daily = _daily_returns(challenger)
    common_dates = sorted(set(ref_daily) & set(cand_daily))
    candidate_fusion_days = [
        item for item in challenger.get("model_fusion_history") or []
        if isinstance(item, dict)
    ]
    candidate_consumed_days = sum(
        any(
            str(model.get("model_key") or "") == model_key
            and bool(model.get("consumed"))
            and str(model.get("availability_reason") or "")
            == "point_in_time_available"
            for model in item.get("production_models") or []
            if isinstance(model, dict)
        )
        for item in candidate_fusion_days
    )
    candidate_effect_days = sum(
        any(
            str(model.get("model_key") or "") == model_key
            and bool(model.get("consumed"))
            and bool(
                model.get(
                    "effective",
                    model_key != "narrative_transition_multitask_v1",
                )
            )
            for model in item.get("production_models") or []
            if isinstance(model, dict)
        )
        for item in candidate_fusion_days
    )
    candidate_effect_contract_days = sum(
        any(
            str(model.get("model_key") or "") == model_key
            and (
                model_key != NARRATIVE_MODEL_KEY
                or str(model.get("effect_contract") or "")
                == NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT
            )
            for model in item.get("production_models") or []
            if isinstance(model, dict)
        )
        for item in candidate_fusion_days
    )
    evidence = {
        "fold": int(window.get("fold") or 0),
        "test_start": window["test_start"],
        "test_end": window["test_end"],
        "reference_total_return": float(ref_metrics.get("total_return") or 0.0),
        "candidate_total_return": float(cand_metrics.get("total_return") or 0.0),
        "return_delta": float(cand_metrics.get("total_return") or 0.0)
        - float(ref_metrics.get("total_return") or 0.0),
        "reference_max_drawdown": float(ref_metrics.get("max_drawdown") or 0.0),
        "candidate_max_drawdown": float(cand_metrics.get("max_drawdown") or 0.0),
        "reference_turnover": float(ref_metrics.get("turnover_rate") or 0.0),
        "candidate_turnover": float(cand_metrics.get("turnover_rate") or 0.0),
        "paired_trade_dates": len(common_dates),
        "reference_replay_status": str(reference.get("status") or ""),
        "candidate_replay_status": str(challenger.get("status") or ""),
        "candidate_model_consumed_days": candidate_consumed_days,
        "candidate_model_effect_days": candidate_effect_days,
        "candidate_model_effect_contract_days": candidate_effect_contract_days,
        "candidate_fusion_days": len(candidate_fusion_days),
        "candidate_fold_artifact": dict(
            fold_binding["point_in_time_fold_artifact"]
        ),
    }
    return evidence, [
        cand_daily[date] - ref_daily[date]
        for date in common_dates
    ]


def run_controlled_model_strategy_ab(
    *,
    model_version_id: int,
    replay_runner: Callable[..., dict[str, Any]] | None = None,
    parallel_workers: int = 1,
    checkpoint_results: list[dict[str, Any]] | None = None,
    checkpoint_contract_hash: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    candidate = get_model_version(int(model_version_id))
    if not candidate:
        return {"ok": False, "status": "blocked_by_missing_model_version"}
    metrics = dict(candidate.get("metrics") or {})
    windows = [
        dict(item) for item in metrics.get("walk_forward_evidence") or []
        if isinstance(item, dict) and item.get("test_start") and item.get("test_end")
    ]
    if len(windows) != 5:
        return _persist_blocked_validation(candidate, {
            "ok": False,
            "status": "blocked_by_incomplete_five_windows",
            "model_version_id": int(model_version_id),
            "production_change_forbidden": True,
        })
    try:
        candidate_fold_bindings = [
            _fold_binding(candidate, window)
            for window in windows
        ]
    except (TypeError, ValueError):
        return _persist_blocked_validation(candidate, {
            "ok": False,
            "status": "blocked_by_missing_point_in_time_fold_artifacts",
            "model_version_id": int(model_version_id),
            "production_change_forbidden": True,
        })
    fold_numbers = [int(item["point_in_time_fold_artifact"]["fold"]) for item in candidate_fold_bindings]
    if len(set(fold_numbers)) != 5:
        return _persist_blocked_validation(candidate, {
            "ok": False,
            "status": "blocked_by_invalid_point_in_time_fold_artifacts",
            "model_version_id": int(model_version_id),
            "production_change_forbidden": True,
        })
    champion = resolve_production_strategy_champion()
    model_key = str(candidate.get("model_key") or "")
    reference_bindings = active_production_plan_model_bindings()
    reference_bindings[model_key] = None
    factor_bindings = dict(champion.get("factor_bindings") or {})
    for key, binding in registered_factor_bindings(
        ("momentum_20", "momentum_60", "volatility_20")
    ).items():
        factor_bindings.setdefault(key, binding)
    frozen_contract = {
        "champion_id": int(champion["champion_id"]),
        "strategy_id": int(champion["strategy_id"]),
        "strategy_controller_version_id": int(champion["strategy_controller_version_id"]),
        "factor_bindings": factor_bindings,
        "reference_model_bindings": reference_bindings,
        "reference_binding_policy": (
            "current_active_models_passing_all_production_gates_only_v1"
        ),
        "candidate_fold_model_bindings": candidate_fold_bindings,
        "target_model_reference": "disabled_ablation",
        "market_state_binding": champion.get("market_state_binding") or {},
        "portfolio_optimizer": champion.get("portfolio_optimizer") or {},
        "execution_contract": champion.get("execution_contract") or {},
        "model_effect_contract": _model_effect_contract(model_key),
    }
    frozen_contract["contract_hash"] = hashlib.sha256(
        json.dumps(frozen_contract, sort_keys=True, default=str).encode()
    ).hexdigest()
    if (
        checkpoint_contract_hash
        and str(checkpoint_contract_hash) != frozen_contract["contract_hash"]
    ):
        checkpoint_results = []
    uses_default_replay = replay_runner is None
    jobs: list[dict[str, Any]] = []
    for window, fold_binding in zip(windows, candidate_fold_bindings):
        candidate_bindings = dict(reference_bindings)
        candidate_bindings[model_key] = fold_binding
        common = {
            "strategy_id": int(champion["strategy_id"]),
            "start_date": str(window["test_start"]),
            "end_date": str(window["test_end"]),
            "factor_bindings": factor_bindings,
            "market_state_binding": champion.get("market_state_binding") or {},
            "strategy_controller_version_id": int(champion["strategy_controller_version_id"]),
            "strategy_content": (champion.get("strategy") or {}).get("content"),
            "strategy_name": str((champion.get("strategy") or {}).get("name") or ""),
            "research_package_hash": frozen_contract["contract_hash"],
        }
        jobs.append({
            "window": window,
            "fold_binding": fold_binding,
            "common": common,
            "reference_bindings": reference_bindings,
            "candidate_bindings": candidate_bindings,
        })
    completed_windows: list[dict[str, Any]] = []
    prior_by_fold: dict[int, dict[str, Any]] = {}
    for item in checkpoint_results or []:
        if not isinstance(item, dict):
            continue
        fold = int(item.get("fold") or 0)
        evidence = item.get("evidence")
        daily_deltas = item.get("paired_daily_deltas")
        if (
            fold > 0
            and isinstance(evidence, dict)
            and int(evidence.get("fold") or 0) == fold
            and isinstance(daily_deltas, list)
        ):
            prior_by_fold[fold] = {
                "fold": fold,
                "evidence": dict(evidence),
                "paired_daily_deltas": [
                    float(value) for value in daily_deltas
                ],
            }
    replay_pairs: list[tuple[dict[str, Any], dict[str, Any] | Exception]] = []
    if uses_default_replay and int(parallel_workers) > 1:
        with ProcessPoolExecutor(
            max_workers=min(len(jobs), int(parallel_workers)),
        ) as executor:
            future_jobs = [
                (job, executor.submit(_run_default_replay_pair, job))
                for job in jobs
                if int(job["window"].get("fold") or 0) not in prior_by_fold
            ]
            for job, future in future_jobs:
                try:
                    replay_pairs.append((job, future.result()))
                except Exception as exc:
                    replay_pairs.append((job, exc))
    else:
        if replay_runner is None:
            from app.services.strategy_replay import run_strategy_replay
            replay_runner = run_strategy_replay
        reference_data_source = BacktestDataSource()
        candidate_data_source = BacktestDataSource()
        for job in jobs:
            fold = int(job["window"].get("fold") or 0)
            if fold in prior_by_fold:
                continue
            try:
                reference = replay_runner(
                    **job["common"],
                    model_bindings=job["reference_bindings"],
                    data_source=reference_data_source,
                )
                challenger = replay_runner(
                    **job["common"],
                    model_bindings=job["candidate_bindings"],
                    data_source=candidate_data_source,
                )
                replay_pairs.append((job, {
                    "reference": reference,
                    "challenger": challenger,
                }))
            except Exception as exc:
                replay_pairs.append((job, exc))
            current_job, current_pair = replay_pairs[-1]
            if isinstance(current_pair, Exception):
                continue
            fold_evidence, fold_deltas = _fold_evidence(
                current_job,
                current_pair,
                model_key=model_key,
            )
            completed_windows.append({
                "fold": fold,
                "evidence": fold_evidence,
                "paired_daily_deltas": fold_deltas,
            })
            if progress_callback:
                merged = [
                    *prior_by_fold.values(),
                    *completed_windows,
                ]
                progress_callback({
                    "phase": "model_strategy_impact_validation",
                    "progress": round(
                        0.1 + 0.85 * len(merged) / len(jobs),
                        6,
                    ),
                    "message": (
                        f"模型完整策略影响验证已完成 "
                        f"{len(merged)}/{len(jobs)} 个窗口"
                    ),
                    "contract_hash": frozen_contract["contract_hash"],
                    "completed_windows": sorted(
                        merged,
                        key=lambda item: int(item.get("fold") or 0),
                    ),
                })
    if uses_default_replay and int(parallel_workers) > 1:
        for job, pair in replay_pairs:
            if isinstance(pair, Exception):
                continue
            fold_evidence, fold_deltas = _fold_evidence(
                job,
                pair,
                model_key=model_key,
            )
            completed_windows.append({
                "fold": int(job["window"].get("fold") or 0),
                "evidence": fold_evidence,
                "paired_daily_deltas": fold_deltas,
            })
    all_completed = {
        int(item["fold"]): item
        for item in [*prior_by_fold.values(), *completed_windows]
    }
    evidence: list[dict[str, Any]] = []
    paired_deltas: list[float] = []
    pair_by_fold = {
        int(job["window"].get("fold") or 0): pair
        for job, pair in replay_pairs
    }
    for job in jobs:
        window = job["window"]
        fold = int(window.get("fold") or 0)
        pair = pair_by_fold.get(fold)
        if isinstance(pair, Exception):
            exc = pair
            return _persist_blocked_validation(candidate, {
                "ok": False,
                "status": "blocked_by_strategy_replay_error",
                "model_version_id": int(model_version_id),
                "model_key": model_key,
                "failed_fold": int(window.get("fold") or len(evidence) + 1),
                "error_type": type(exc).__name__,
                "error": str(exc)[:300],
                "production_change_forbidden": True,
            })
        completed = all_completed.get(fold)
        if not completed:
            return _persist_blocked_validation(candidate, {
                "ok": False,
                "status": "blocked_by_incomplete_strategy_replay_checkpoint",
                "model_version_id": int(model_version_id),
                "model_key": model_key,
                "failed_fold": fold,
                "production_change_forbidden": True,
            })
        evidence.append(dict(completed["evidence"]))
        paired_deltas.extend(completed["paired_daily_deltas"])
    deltas = [float(item["return_delta"]) for item in evidence]
    bootstrap_lower = _paired_bootstrap_lower(paired_deltas)
    gates = {
        "five_complete_windows": len(evidence) == 5,
        "all_replays_completed": all(
            item["reference_replay_status"] == "completed"
            and item["candidate_replay_status"] == "completed"
            for item in evidence
        ),
        "paired_dates_complete": all(
            int(item["paired_trade_dates"]) >= 20
            for item in evidence
        ),
        "candidate_fold_model_actually_consumed": all(
            int(item["candidate_fusion_days"]) > 0
            and int(item["candidate_model_consumed_days"])
            == int(item["candidate_fusion_days"])
            for item in evidence
        ),
        "candidate_fold_model_actually_effective": all(
            int(item["candidate_model_effect_days"]) > 0
            for item in evidence
        ),
        "candidate_fold_model_effect_contract_current": all(
            model_key != NARRATIVE_MODEL_KEY
            or (
                int(item["candidate_fusion_days"]) > 0
                and int(item["candidate_model_effect_contract_days"])
                == int(item["candidate_fusion_days"])
            )
            for item in evidence
        ),
        "majority_windows_improved": sum(value > 0 for value in deltas) >= 3,
        "mean_return_delta_positive": float(np.mean(deltas)) > 0,
        "paired_daily_bootstrap_lower_positive": (
            bootstrap_lower is not None and bootstrap_lower > 0
        ),
        "worst_drawdown_not_materially_worse": all(
            float(item["candidate_max_drawdown"])
            >= float(item["reference_max_drawdown"]) - 0.02
            for item in evidence
        ),
        "turnover_not_explosive": all(
            float(item["candidate_turnover"])
            <= max(0.05, float(item["reference_turnover"]) * 1.5)
            for item in evidence
        ),
    }
    blocked = [key for key, passed in gates.items() if not passed]
    result = {
        "ok": True,
        "schema_version": "alis_controlled_model_strategy_ab_v3",
        "comparison_contract": "point_in_time_fold_model_vs_disabled_ablation_v1",
        "status": "passed" if not blocked else "failed",
        "model_version_id": int(model_version_id),
        "model_key": model_key,
        "model_effect_contract": _model_effect_contract(model_key),
        "frozen_contract": frozen_contract,
        "windows": evidence,
        "mean_return_delta": round(float(np.mean(deltas)), 8),
        "worst_return_delta": round(float(min(deltas)), 8),
        "paired_daily_observations": len(paired_deltas),
        "paired_bootstrap_95_lower": (
            round(float(bootstrap_lower), 8) if bootstrap_lower is not None else None
        ),
        "gates": gates,
        "blocked_reasons": blocked,
        "production_change_forbidden": True,
    }
    metrics["strategy_impact_ab"] = result
    update_model_version_metrics(int(model_version_id), metrics=metrics)
    validation = dict(candidate.get("validation") or {})
    eligibility = dict(validation.get("production_eligibility") or {})
    eligibility_gates = dict(eligibility.get("gates") or {})
    eligibility_gates["controlled_strategy_ab"] = not blocked
    eligibility["gates"] = eligibility_gates
    eligibility["blocked_reasons"] = [
        key for key, passed in eligibility_gates.items()
        if key not in {
            "human_review",
            "live_observation_20_sessions",
            "simulation_only_live_observation_waived",
        } and not passed
    ]
    eligibility["eligible"] = False
    validation["status"] = "passed" if not eligibility["blocked_reasons"] else "failed"
    validation["production_eligible"] = False
    validation["production_eligibility"] = eligibility
    update_model_version_validation(int(model_version_id), validation=validation)
    follow_up = {
        "status": (
            "retired_legacy_model_candidate_protocol"
            if blocked
            else "not_required"
        ),
        "replacement_contract": "alis_task_native_model_evaluation_v1",
        "production_change_forbidden": True,
    }
    result["follow_up_model_candidate_research"] = follow_up
    if not blocked:
        from app.services.model_shadow_observation import (
            enroll_model_shadow_candidate,
        )

        try:
            result["future_shadow_enrollment"] = enroll_model_shadow_candidate(
                int(model_version_id)
            )
        except Exception as exc:
            result["future_shadow_enrollment"] = {
                "ok": False,
                "status": "enrollment_deferred_to_daily_autonomy",
                "error_type": type(exc).__name__,
                "error": str(exc)[:300],
                "production_change_forbidden": True,
            }
    return result


def reconcile_narrative_strategy_impact_contract() -> dict[str, Any]:
    """Requeue only narrative A/B evidence produced before lifecycle effects existed."""

    candidate = next(
        (
            version
            for version in list_model_versions(NARRATIVE_MODEL_KEY, limit=10)
            if str(version.get("status") or "") == "candidate"
            and str(
                ((version.get("metrics") or {}).get("model_evaluation_contract"))
                or ""
            )
            == MODEL_EVALUATION_CONTRACT
            and bool(
                ((version.get("metrics") or {}).get("task_native_oos") or {}).get(
                    "passed"
                )
            )
            and bool(
                ((version.get("metrics") or {}).get("task_native_oos") or {}).get(
                    "baseline_outperformed"
                )
            )
        ),
        None,
    )
    if not candidate:
        return {
            "ok": True,
            "changed": False,
            "status": "no_task_valid_narrative_candidate",
        }

    metrics = dict(candidate.get("metrics") or {})
    impact = metrics.get("strategy_impact_ab")
    impact = impact if isinstance(impact, dict) else {}
    validation = dict(candidate.get("validation") or {})
    reconciliation = validation.get("strategy_ab_contract_reconciliation")
    reconciliation = reconciliation if isinstance(reconciliation, dict) else {}
    current_contract = (
        str(impact.get("model_effect_contract") or "")
        == NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT
        and "candidate_fold_model_actually_effective"
        in (impact.get("gates") or {})
        and bool(
            (impact.get("gates") or {}).get(
                "candidate_fold_model_effect_contract_current"
            )
        )
    )
    already_invalidated = (
        not impact
        and str(reconciliation.get("required_contract") or "")
        == NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT
    )
    if current_contract:
        return {
            "ok": True,
            "changed": False,
            "status": "current_contract_already_evaluated",
            "model_version_id": int(candidate["id"]),
        }

    if not already_invalidated:
        metrics.pop("strategy_impact_ab", None)
        eligibility = dict(validation.get("production_eligibility") or {})
        gates = dict(eligibility.get("gates") or {})
        gates["controlled_strategy_ab"] = False
        gates["live_observation_20_sessions"] = False
        eligibility["gates"] = gates
        eligibility["eligible"] = False
        eligibility["blocked_reasons"] = [
            key
            for key, passed in gates.items()
            if key
            not in {
                "human_review",
                "live_observation_20_sessions",
                "simulation_only_live_observation_waived",
            }
            and not bool(passed)
        ]
        validation["status"] = "pending_strategy_ab"
        validation["production_eligible"] = False
        validation["production_eligibility"] = eligibility
        validation.pop("model_shadow", None)
        validation["strategy_ab_contract_reconciliation"] = {
            "status": "old_evidence_invalidated",
            "required_contract": NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT,
            "invalidated_schema_version": impact.get("schema_version"),
            "invalidated_status": impact.get("status"),
            "reason": "supervised_narrative_output_had_no_strategy_effect",
            "invalidated_at": datetime.now().astimezone().isoformat(),
        }
        update_model_version_metrics(int(candidate["id"]), metrics=metrics)
        update_model_version_validation(
            int(candidate["id"]),
            validation=validation,
        )

    from app.services.evolution_orchestrator import create_evolution_task

    task = create_evolution_task(
        task_type="model_strategy_impact_validation",
        payload={
            "model_key": NARRATIVE_MODEL_KEY,
            "model_version_id": int(candidate["id"]),
            "version": str(candidate.get("version") or ""),
            "model_effect_contract": NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT,
            "production_change_forbidden": True,
        },
        dedupe_key=(
            "model_strategy_impact_validation:v4:"
            f"{int(candidate['id'])}:"
            f"{NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT}"
        ),
        priority=76,
    )
    return {
        "ok": True,
        "changed": not already_invalidated,
        "status": "requeued_with_current_lifecycle_effect_contract",
        "model_version_id": int(candidate["id"]),
        "task_id": int(task.get("id") or 0),
        "task_status": str(task.get("status") or ""),
        "required_contract": NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT,
        "production_change_forbidden": True,
    }
