from __future__ import annotations

from datetime import datetime, timedelta
import os
from typing import Any

from app.services.plan_model_control import (
    MODEL_EVALUATION_CONTRACT,
    controlled_strategy_ab_passed_current_contract,
    model_family_status,
    process_plan_model_training_failure,
    reconcile_unhandled_plan_model_training_failures,
    request_plan_model_training,
    review_plan_model_version,
)
from app.services.narrative_lifecycle_contract import SCHEMA_VERSION as NARRATIVE_LABEL_CONTRACT


NARRATIVE_RESEARCH_REFRESH_SESSIONS = 20
NARRATIVE_FORMAL_COHORT_SESSIONS = 126


def _metric(metrics: dict[str, Any] | None, key: str) -> float | None:
    try:
        value = (metrics or {}).get(key)
        return float(value) if value is not None else None
    except Exception:
        return None


def _candidate_evaluation_passed(
    metrics: dict[str, Any] | None,
    *,
    model_key: str = "",
) -> tuple[bool, str]:
    if str((metrics or {}).get("model_evaluation_contract") or "") != MODEL_EVALUATION_CONTRACT:
        return False, "legacy_model_evaluation_contract"
    if not bool((metrics or {}).get("task_metric_passed")):
        return False, "task_native_oos_not_passed"
    task_oos = (metrics or {}).get("task_native_oos")
    if not isinstance(task_oos, dict) or not bool(task_oos.get("passed")):
        return False, "task_native_walk_forward_not_passed"
    if not bool(task_oos.get("baseline_outperformed")):
        return False, "task_native_baseline_not_outperformed"
    impact = (metrics or {}).get("strategy_impact_ab")
    if not controlled_strategy_ab_passed_current_contract(model_key, impact):
        return False, "controlled_strategy_ab_not_passed"
    return True, ""


def _training_data_advanced(item: dict[str, Any]) -> bool:
    current = item.get("coverage") or {}
    run = item.get("latest_train_run") or {}
    prior = ((run.get("metrics") or {}).get("coverage") or {})
    if not prior:
        latest = item.get("latest_version") or {}
        prior_sessions = _metric(latest.get("metrics") or {}, "effective_sessions")
        current_sessions = _metric(current, "effective_sessions")
        return prior_sessions is None or current_sessions is None or current_sessions > prior_sessions
    current_labeled = _metric(current, "latest_training_sessions")
    prior_labeled = _metric(run, "labeled_trade_date_count")
    if prior_labeled is None:
        prior_labeled = _metric(run.get("metrics") or {}, "labeled_trade_date_count")
    if (
        current_labeled is not None
        and prior_labeled is not None
        and current_labeled > prior_labeled
    ):
        return True
    keys = ("trade_sessions", "theme_snapshot_sessions", "event_rows", "capital_coverage_slices", "effective_sessions")
    return any(float(current.get(key) or 0) > float(prior.get(key) or 0) for key in keys)


def _model_training_date_advance(item: dict[str, Any]) -> int:
    current = item.get("coverage") or {}
    latest = item.get("latest_version") or {}
    latest_metrics = latest.get("metrics") or {}
    run = item.get("latest_train_run") or {}
    current_sessions = int(
        current.get("latest_training_sessions")
        or current.get("effective_sessions")
        or 0
    )
    prior_sessions = int(
        latest_metrics.get("effective_sessions")
        or run.get("labeled_trade_date_count")
        or ((run.get("metrics") or {}).get("labeled_trade_date_count") or 0)
    )
    return max(0, current_sessions - prior_sessions)


def _event_training_date_advance(item: dict[str, Any]) -> int:
    return _model_training_date_advance(item)


def _training_request_context(
    item: dict[str, Any],
    *,
    trigger: str,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "trigger_reason": item.get("reason") or trigger,
        "requested_by": "auto_optimizer",
        "trigger": trigger,
    }
    if (
        str(item.get("model_key") or "")
        == "narrative_transition_multitask_v1"
        and trigger == "train_research_shadow"
    ):
        context.update({
            "evaluation_scope": "research_shadow_only",
            "production_change_forbidden": True,
            "formal_promotion_forbidden": True,
            "formal_promotion_block_reason": (
                "waiting_for_complete_new_126_session_oos_cohort"
            ),
            "research_refresh_sessions": NARRATIVE_RESEARCH_REFRESH_SESSIONS,
            "formal_cohort_sessions": NARRATIVE_FORMAL_COHORT_SESSIONS,
        })
    if str(item.get("model_key") or "") != "event_impact_assessor_v2":
        return context
    latest = item.get("latest_version") or {}
    latest_metrics = latest.get("metrics") or {}
    candidate_spec = latest_metrics.get("candidate_spec")
    if int(latest.get("id") or 0) > 0 and isinstance(candidate_spec, dict) and candidate_spec:
        context.update({
            "source_model_version_id": int(latest["id"]),
            "model_candidate_spec": candidate_spec,
            "event_research_resume_contract": (
                "inherit_frozen_candidate_after_complete_new_event_test_window_v1"
            ),
        })
    return context


def _narrative_research_shadow_only(item: dict[str, Any]) -> bool:
    latest = item.get("latest_version") or {}
    context = (latest.get("metrics") or {}).get("targeted_training_context")
    return bool(
        isinstance(context, dict)
        and str(context.get("evaluation_scope") or "")
        == "research_shadow_only"
    )


def _narrative_research_shadow_due(item: dict[str, Any]) -> bool:
    coverage = item.get("coverage") if isinstance(item.get("coverage"), dict) else {}
    if not bool(coverage.get("data_ready")):
        return False
    latest = item.get("latest_version") or {}
    metrics = latest.get("metrics") or {}
    feature_contract = metrics.get("feature_computation_contract")
    trained_label_contract = (
        str(feature_contract.get("narrative_lifecycle") or "")
        if isinstance(feature_contract, dict)
        else ""
    )
    if trained_label_contract != NARRATIVE_LABEL_CONTRACT:
        return True
    return bool(
        _narrative_research_shadow_only(item)
        and _model_training_date_advance(item)
        >= NARRATIVE_RESEARCH_REFRESH_SESSIONS
        and _periodic_retrain_due(item)
    )


def _periodic_retrain_due(item: dict[str, Any], *, require_rejected: bool = False) -> bool:
    """Permit a new challenger only after both data and cadence advance."""
    latest = item.get("latest_version") or {}
    review = ((latest.get("validation") or {}).get("review") or {})
    if require_rejected and str(review.get("status") or "") != "rejected":
        return False
    coverage = item.get("coverage") or {}
    if not bool(coverage.get("data_ready")) or not _training_data_advanced(item):
        return False
    run = item.get("latest_train_run") or {}
    finished = run.get("finished_at") or latest.get("created_at")
    try:
        current = datetime.now().astimezone()
        parsed = datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=current.tzinfo)
        interval = max(24, int(os.getenv("ALIS_PLAN_MODEL_RETRAIN_INTERVAL_HOURS", "168")))
    except (TypeError, ValueError):
        return False
    return current - parsed.astimezone(current.tzinfo) >= timedelta(hours=interval)


def _recommendation(item: dict[str, Any]) -> dict[str, Any]:
    status = str(item.get("status") or "unknown")
    active = item.get("active_version") or {}
    latest = item.get("latest_version") or {}
    blocked = list(item.get("blocked_reasons") or [])
    latest_metrics = latest.get("metrics") or {}
    latest_is_challenger = bool(latest.get("id")) and int(latest.get("id") or 0) != int(active.get("id") or 0) and str(latest.get("status") or "") == "candidate"
    latest_review = ((latest.get("validation") or {}).get("review") or {}) if isinstance(latest.get("validation"), dict) else {}
    training_protocol_status = str(
        item.get("latest_training_protocol_status") or ""
    )
    current_contract = (
        training_protocol_status == "current"
        if training_protocol_status
        else str(latest_metrics.get("model_evaluation_contract") or "")
        == MODEL_EVALUATION_CONTRACT
    )
    if not current_contract:
        coverage = item.get("coverage") if isinstance(item.get("coverage"), dict) else {}
        if bool(coverage.get("data_ready")):
            return {
                "action": "retrain_challenger",
                "reason": "现有版本不满足当前训练、评价或特征协议；点时数据已满足，按当前完整协议重训。",
                "requires_human": False,
            }
        return {
            "action": "wait_for_data",
            "reason": "现有版本不满足当前训练、评价或特征协议，但真实点时训练数据不足。",
            "requires_human": False,
        }

    event_cohort_governance = latest_metrics.get(
        "event_research_cohort_governance"
    )
    if (
        str(item.get("model_key") or "") == "event_impact_assessor_v2"
        and isinstance(event_cohort_governance, dict)
        and str(event_cohort_governance.get("status") or "")
        == "adaptive_same_cohort_non_promotable"
        and _event_training_date_advance(item) < 10
    ):
        return {
            "action": "wait_for_oos_cohort_advance",
            "reason": (
                "当前正式五窗已被用于后继特征设计；至少形成10个全新有效事件日"
                "的完整测试窗后，系统再继承冻结方案自动评价。"
            ),
            "requires_human": False,
        }

    if (
        str(item.get("model_key") or "")
        == "narrative_transition_multitask_v1"
        and (
            str(latest_review.get("status") or "") == "rejected"
            or _narrative_research_shadow_only(item)
        )
        and _model_training_date_advance(item)
        < NARRATIVE_FORMAL_COHORT_SESSIONS
    ):
        if _narrative_research_shadow_due(item):
            return {
                "action": "train_research_shadow",
                "reason": (
                    "叙事 V3 可立即训练研究候选并并行累计未来预测；"
                    "该候选固定禁止生产晋升，正式评价仍等待全新126交易日测试窗。"
                ),
                "requires_human": False,
            }
        return {
            "action": "observe_research_shadow",
            "reason": (
                "叙事研究候选继续并行观察；每新增20个交易日最多刷新一次，"
                "正式晋升仍等待完整的126交易日新测试窗。"
            ),
            "requires_human": False,
        }

    if latest_is_challenger:
        if str(latest_review.get("status") or "") == "rejected" and _periodic_retrain_due(item, require_rejected=True):
            return {"action": "retrain_challenger", "reason": "候选已驳回且已有新数据周期，可生成下一候选。", "requires_human": False}
        passed, reason_code = _candidate_evaluation_passed(
            latest_metrics,
            model_key=str(item.get("model_key") or ""),
        )
        if str(latest_review.get("status") or "") == "rejected":
            return {"action": "auto_rejected", "reason": "弱候选未进入批准队列。", "requires_human": False}
        if not passed:
            if (
                reason_code == "controlled_strategy_ab_not_passed"
                and not isinstance(latest_metrics.get("strategy_impact_ab"), dict)
            ):
                return {
                    "action": "observe",
                    "reason": "任务原生 OOS 已通过，等待冻结策略 A/B 完成。",
                    "requires_human": False,
                }
            return {"action": "reject_recommended", "reason": f"候选模型本职任务或独立策略A/B未通过，系统应自动驳回（{reason_code}）。", "requires_human": False}
        if blocked and any("requires_review" not in reason and "live_observation" not in reason for reason in blocked):
            action = "observe"
            reason = "候选模型还有非人工评审类阻塞，先观察。"
        else:
            action = "observe"
            reason = "任务原生 OOS 与冻结策略 A/B 已通过，继续完成影子观察硬门。"
        return {"action": action, "reason": reason, "requires_human": False}
    if status == "trainable":
        return {"action": "train_challenger", "reason": "数据覆盖已满足，可以自动训练 challenger。", "requires_human": False}
    if status == "proxy":
        return {"action": "wait_for_data", "reason": "数据覆盖不足，继续等待数据中台补齐。", "requires_human": False}
    if status == "production":
        if _periodic_retrain_due(item):
            return {"action": "retrain_challenger", "reason": "生产模型已有新数据且达到最小重训周期，生成周期性 challenger。", "requires_human": False}
        return {"action": "healthy", "reason": "生产模型可用；仅做周期性 challenger 检查。", "requires_human": False}
    return {"action": "observe", "reason": "状态未识别，保持观察。", "requires_human": False}


def _auto_reject_reason(item: dict[str, Any]) -> str:
    active = item.get("active_version") or {}
    latest = item.get("latest_version") or {}
    if _narrative_research_shadow_only(item):
        return ""
    if not latest.get("id") or int(latest.get("id") or 0) == int(active.get("id") or 0):
        return ""
    if str(latest.get("status") or "") != "candidate":
        return ""
    validation = latest.get("validation") or {}
    review = validation.get("review") if isinstance(validation.get("review"), dict) else {}
    if str((review or {}).get("status") or "") == "rejected":
        return ""
    model_shadow = validation.get("model_shadow") if isinstance(
        validation.get("model_shadow"), dict
    ) else {}
    if str(model_shadow.get("status") or "") == "failed":
        return "auto_reject_future_shadow_failed"
    candidate = latest.get("metrics") or {}
    passed, reason = _candidate_evaluation_passed(
        candidate,
        model_key=str(item.get("model_key") or ""),
    )
    if (
        not passed
        and reason == "controlled_strategy_ab_not_passed"
        and not isinstance(candidate.get("strategy_impact_ab"), dict)
    ):
        return ""
    if not passed:
        return f"auto_reject_{reason}"
    return ""


def _auto_reject_weaker_candidates(family: dict[str, Any]) -> list[dict[str, Any]]:
    rejected: list[dict[str, Any]] = []
    for item in family.get("items") or []:
        reason = _auto_reject_reason(item)
        latest = item.get("latest_version") or {}
        if not reason or not latest.get("id"):
            continue
        result = review_plan_model_version(
            int(latest["id"]),
            reviewer="auto_optimizer",
            decision="rejected",
            comment=f"自优化自动驳回：候选模型指标弱于生产模型或核心指标为负（{reason}）",
            waive_live_observation=False,
        )
        rejected.append({"model_key": item.get("model_key"), "version": latest.get("version"), "reason": reason, "result": result})
    return rejected


def _auto_promote_ready_candidates(family: dict[str, Any]) -> list[dict[str, Any]]:
    """Promote only task-valid candidates with separate strategy and Shadow proof."""
    promoted: list[dict[str, Any]] = []
    for item in family.get("items") or []:
        if _narrative_research_shadow_only(item):
            continue
        latest = item.get("latest_version") or {}
        active = item.get("active_version") or {}
        if (
            not latest.get("id")
            or int(latest.get("id") or 0) == int(active.get("id") or 0)
            or str(latest.get("status") or "") != "candidate"
        ):
            continue
        validation = latest.get("validation") if isinstance(latest.get("validation"), dict) else {}
        eligibility = validation.get("production_eligibility") if isinstance(validation.get("production_eligibility"), dict) else {}
        gates = eligibility.get("gates") if isinstance(eligibility.get("gates"), dict) else {}
        candidate_metrics = latest.get("metrics") or {}
        evaluation_passed, reason = _candidate_evaluation_passed(
            candidate_metrics,
            model_key=str(item.get("model_key") or ""),
        )
        live_passed = bool(gates.get("live_observation_20_sessions"))
        non_review_gates = {
            key: bool(value) for key, value in gates.items()
            if key not in {"human_review", "simulation_only_live_observation_waived"}
        }
        if not evaluation_passed or not live_passed or not all(non_review_gates.values()):
            continue
        result = review_plan_model_version(
            int(latest["id"]),
            reviewer="deterministic_auto_governance",
            decision="approved",
            comment=(
                "本职任务滚动样本外、同任务基线、独立策略A/B和20交易日影子观察全部通过；"
                "仅在研究与模拟交易范围自动晋升。"
            ),
            waive_live_observation=False,
        )
        if result.get("production_eligible"):
            promoted.append({
                "model_key": item.get("model_key"),
                "version": latest.get("version"),
                "reason": reason or "all_deterministic_gates_passed",
                "result": result,
            })
            # One release changes the frozen reference bundle for every other
            # candidate. They must restart future observation against it.
            break
    return promoted


def _shadow_ab(item: dict[str, Any]) -> dict[str, Any]:
    latest = item.get("latest_version") or {}
    active = item.get("active_version") or {}
    same_version = bool(latest.get("id")) and int(latest.get("id") or 0) == int(active.get("id") or 0)
    latest_review = ((latest.get("validation") or {}).get("review") or {}) if isinstance(latest.get("validation"), dict) else {}
    latest_metrics = latest.get("metrics") or {}
    strategy_impact = latest_metrics.get("strategy_impact_ab")
    strategy_impact = strategy_impact if isinstance(strategy_impact, dict) else {}
    latest_validation = latest.get("validation") or {}
    sessions = int(
        _metric(latest_validation, "live_observation_sessions")
        or _metric(latest_validation, "history_sessions")
        or _metric(latest_validation, "oos_sessions")
        or 0
    )
    delta = _metric(strategy_impact, "mean_return_delta")
    ready = sessions >= 20
    status = "no_challenger" if same_version else "auto_rejected" if str(latest_review.get("status") or "") == "rejected" else "review_ready" if ready else "shadow_running"
    return {
        "champion_version": active.get("version"),
        "challenger_version": None if same_version else latest.get("version"),
        "has_challenger": bool(latest.get("id")) and not same_version and str(latest.get("status") or "") == "candidate",
        "observation_sessions": sessions,
        "minimum_sessions": 20,
        "controlled_strategy_ab_status": str(
            strategy_impact.get("status") or "not_run"
        ),
        "mean_return_delta": delta,
        "status": status,
        "recommendation": (
            "no_action"
            if same_version
            else "rejected"
            if status == "auto_rejected"
            else "ready_for_governance"
            if ready and str(strategy_impact.get("status") or "") == "passed"
            else "observe"
        ),
    }


def build_auto_optimization_status(*, apply_auto_reject: bool = True) -> dict[str, Any]:
    family = model_family_status()
    auto_rejected = _auto_reject_weaker_candidates(family) if apply_auto_reject else []
    if auto_rejected:
        family = model_family_status()
    auto_promoted = _auto_promote_ready_candidates(family) if apply_auto_reject else []
    if auto_promoted:
        family = model_family_status()
    items: list[dict[str, Any]] = []
    for item in family.get("items") or []:
        latest = item.get("latest_version") or {}
        active = item.get("active_version") or {}
        recommendation = _recommendation(item)
        items.append({
            "model_key": item.get("model_key"),
            "name": item.get("name"),
            "role": item.get("role"),
            "status": item.get("status"),
            "trigger": recommendation["action"],
            "reason": recommendation["reason"],
            "requires_human": recommendation["requires_human"],
            "latest_version_id": latest.get("id"),
            "latest_version": latest.get("version"),
            "active_version": active.get("version"),
            "candidate_metrics": latest.get("metrics") or {},
            "production_metrics": active.get("metrics") or {},
            "blocked_reasons": item.get("blocked_reasons") or [],
            "running": (item.get("latest_train_run") or {}).get("status") == "running",
            "shadow_ab": _shadow_ab(item),
        })
    return {
        "ok": True,
        "schema_version": "plan_model_auto_optimizer_v1",
        "policy": {
            "mode": "deterministic_full_autonomy_simulation_only",
            "summary": "系统自动训练、驳回弱候选，并仅在任务原生 OOS、冻结策略 A/B 与20日影子观察全部通过后自动晋升。",
            "auto_allowed": [
                "data_ready_check", "train_challenger", "compare_candidate",
                "auto_reject_weaker_candidate", "shadow_observation",
                "promote_after_all_deterministic_gates",
            ],
            "human_required": ["data_or_contract_anomaly_only"],
        },
        "auto_rejected": auto_rejected,
        "auto_promoted": auto_promoted,
        "review_queue": [item for item in items if item["requires_human"]],
        "items": items,
        "generated_at": datetime.now().isoformat(),
    }


def run_auto_optimization_once(
    force: bool = False,
    *,
    training_failure_recovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from app.services.model_shadow_observation import ensure_model_shadow_tasks
    from app.services.model_strategy_impact import (
        reconcile_narrative_strategy_impact_contract,
        reconcile_plan_model_strategy_impact_contracts,
    )

    strategy_ab_contracts = reconcile_plan_model_strategy_impact_contracts()
    narrative_strategy_ab = reconcile_narrative_strategy_impact_contract()
    model_shadow_tasks = ensure_model_shadow_tasks()
    if training_failure_recovery:
        recovery = process_plan_model_training_failure(
            training_failure_recovery
        )
        started = list(recovery.get("started") or [])
        return {
            "ok": not bool(recovery.get("owner_intervention_required")),
            "mode": "training_failure_recovery",
            "started_count": len(started),
            "started": started,
            "skipped_count": 0,
            "skipped": [],
            "training_failure_recovery": recovery,
            "narrative_strategy_ab": narrative_strategy_ab,
            "strategy_ab_contracts": strategy_ab_contracts,
            "model_shadow_tasks": model_shadow_tasks,
        }
    failure_reconciliation = reconcile_unhandled_plan_model_training_failures()
    recovery_model_keys = set(
        failure_reconciliation.get("managed_model_keys") or []
    )
    before = build_auto_optimization_status(apply_auto_reject=True)
    waiting_model_keys = {
        str(item.get("model_key") or "")
        for item in before["items"]
        if str(item.get("trigger") or "") == "wait_for_data"
    }
    model_data_backfill: dict[str, Any] = {
        "ok": True,
        "created": False,
        "reason": "no_registered_model_data_gap",
    }
    if waiting_model_keys & {
        "capital_persistence_multitask_v1",
        "event_impact_assessor_v2",
    }:
        from app.data_platform.services.backfill import (
            ensure_plan_model_data_backfill,
        )

        model_data_backfill = ensure_plan_model_data_backfill()
    started: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    maximum_starts = 1
    for item in before["items"]:
        trigger = str(item.get("trigger") or "")
        if str(item.get("model_key") or "") in recovery_model_keys:
            skipped.append({
                "model_key": item["model_key"],
                "reason": "training_failure_recovery_managed",
            })
            continue
        if item.get("running"):
            skipped.append({"model_key": item["model_key"], "reason": "training_running"})
            continue
        if trigger in {
            "train_challenger", "retrain_challenger", "train_research_shadow",
        } or (force and item.get("status") == "production"):
            if len(started) >= maximum_starts:
                skipped.append({
                    "model_key": item["model_key"],
                    "reason": "single_model_training_resource_budget",
                })
                continue
            result = request_plan_model_training(
                str(item["model_key"]),
                training_context=_training_request_context(
                    item,
                    trigger=trigger,
                ),
            )
            if result.get("started"):
                started.append(result)
            else:
                skipped.append({"model_key": item["model_key"], "reason": result.get("reason") or result.get("error") or "not_started"})
        else:
            skipped.append({"model_key": item["model_key"], "reason": trigger})
    after = build_auto_optimization_status(apply_auto_reject=True)
    return {
        "ok": True,
        "auto_rejected_count": len(before.get("auto_rejected") or []),
        "auto_rejected": before.get("auto_rejected") or [],
        "auto_promoted_count": len(before.get("auto_promoted") or []),
        "auto_promoted": before.get("auto_promoted") or [],
        "started_count": len(started),
        "maximum_starts_per_cycle": maximum_starts,
        "skipped_count": len(skipped),
        "started": started,
        "skipped": skipped,
        "model_data_backfill": model_data_backfill,
        "training_failure_reconciliation": failure_reconciliation,
        "narrative_strategy_ab": narrative_strategy_ab,
        "strategy_ab_contracts": strategy_ab_contracts,
        "model_shadow_tasks": model_shadow_tasks,
        "status": after,
    }
