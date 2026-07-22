from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.plan_model_control import model_family_status, request_plan_model_training, review_plan_model_version


def _metric(metrics: dict[str, Any] | None, key: str) -> float | None:
    try:
        value = (metrics or {}).get(key)
        return float(value) if value is not None else None
    except Exception:
        return None


def _cost_after_lift(metrics: dict[str, Any] | None) -> float | None:
    return _metric(metrics, "net_lift") if _metric(metrics, "net_lift") is not None else _metric(metrics, "lift")


def _cost_after_walk_forward(metrics: dict[str, Any] | None) -> float | None:
    return _metric(metrics, "walk_forward_lift") or _cost_after_lift(metrics)


def _cost_after_worst_window(metrics: dict[str, Any] | None) -> float | None:
    return _metric(metrics, "worst_window_net_lift") or _cost_after_walk_forward(metrics)


def _candidate_cost_after_passed(metrics: dict[str, Any] | None) -> tuple[bool, str]:
    net_lift = _cost_after_lift(metrics)
    walk_forward = _cost_after_walk_forward(metrics)
    worst_window = _cost_after_worst_window(metrics)
    if net_lift is None or walk_forward is None or worst_window is None:
        return False, "missing_cost_after_metrics"
    if net_lift <= 0:
        return False, f"candidate_net_lift_non_positive:{net_lift:.6f}"
    if walk_forward <= 0:
        return False, f"candidate_walk_forward_non_positive:{walk_forward:.6f}"
    if worst_window <= 0:
        return False, f"candidate_worst_window_non_positive:{worst_window:.6f}"
    return True, ""


def _recommendation(item: dict[str, Any]) -> dict[str, Any]:
    status = str(item.get("status") or "unknown")
    active = item.get("active_version") or {}
    latest = item.get("latest_version") or {}
    blocked = list(item.get("blocked_reasons") or [])
    latest_metrics = latest.get("metrics") or {}
    active_metrics = active.get("metrics") or {}
    latest_lift = _cost_after_walk_forward(latest_metrics)
    active_lift = _cost_after_walk_forward(active_metrics)
    latest_is_challenger = bool(latest.get("id")) and int(latest.get("id") or 0) != int(active.get("id") or 0) and str(latest.get("status") or "") == "candidate"
    latest_review = ((latest.get("validation") or {}).get("review") or {}) if isinstance(latest.get("validation"), dict) else {}

    if latest_is_challenger:
        passed, reason_code = _candidate_cost_after_passed(latest_metrics)
        if str(latest_review.get("status") or "") == "rejected":
            return {"action": "auto_rejected", "reason": "弱候选未进入批准队列。", "requires_human": False}
        if not passed:
            return {"action": "reject_recommended", "reason": f"候选模型费用后核心指标未通过，系统应自动驳回（{reason_code}）。", "requires_human": False}
        if blocked and any("requires_review" not in reason and "live_observation" not in reason for reason in blocked):
            action = "observe"
            reason = "候选模型还有非人工评审类阻塞，先观察。"
        elif active_lift is None or (latest_lift is not None and latest_lift >= active_lift):
            action = "approve_recommended"
            reason = "候选模型费用后/样本外指标不弱于生产模型，建议人工审核。"
        else:
            action = "reject_recommended"
            reason = "候选模型弱于现有生产模型，建议驳回或重训。"
        return {"action": action, "reason": reason, "requires_human": True}
    if status == "trainable":
        return {"action": "train_challenger", "reason": "数据覆盖已满足，可以自动训练 challenger。", "requires_human": False}
    if status == "proxy":
        return {"action": "wait_for_data", "reason": "数据覆盖不足，继续等待数据中台补齐。", "requires_human": False}
    if status == "production":
        if latest_lift is not None and latest_lift < 0:
            return {"action": "retrain_challenger", "reason": "生产模型最近指标偏弱，建议训练 challenger 对照。", "requires_human": False}
        return {"action": "healthy", "reason": "生产模型可用；仅做周期性 challenger 检查。", "requires_human": False}
    return {"action": "observe", "reason": "状态未识别，保持观察。", "requires_human": False}


def _auto_reject_reason(item: dict[str, Any]) -> str:
    active = item.get("active_version") or {}
    latest = item.get("latest_version") or {}
    if not latest.get("id") or int(latest.get("id") or 0) == int(active.get("id") or 0):
        return ""
    if str(latest.get("status") or "") != "candidate":
        return ""
    validation = latest.get("validation") or {}
    review = validation.get("review") if isinstance(validation.get("review"), dict) else {}
    if str((review or {}).get("status") or "") == "rejected":
        return ""
    candidate = latest.get("metrics") or {}
    production = active.get("metrics") or {}
    candidate_lift = _cost_after_lift(candidate)
    candidate_wf = _cost_after_walk_forward(candidate)
    candidate_worst = _cost_after_worst_window(candidate)
    production_lift = _cost_after_lift(production)
    production_wf = _cost_after_walk_forward(production)
    production_worst = _cost_after_worst_window(production)
    candidate_hit_rate = _metric(candidate, "hit_rate")
    production_hit_rate = _metric(production, "hit_rate")
    passed, reason = _candidate_cost_after_passed(candidate)
    if not passed:
        return f"auto_reject_{reason}"
    if candidate_wf is not None and production_wf is not None and candidate_wf < production_wf:
        return f"auto_reject_walk_forward_delta_negative:{candidate_wf - production_wf:.6f}"
    if candidate_lift is not None and production_lift is not None and candidate_lift < production_lift:
        return f"auto_reject_net_lift_delta_negative:{candidate_lift - production_lift:.6f}"
    if candidate_worst is not None and production_worst is not None and candidate_worst < production_worst:
        return f"auto_reject_worst_window_delta_negative:{candidate_worst - production_worst:.6f}"
    if candidate_hit_rate is not None and production_hit_rate is not None and candidate_hit_rate < production_hit_rate:
        return f"auto_reject_hit_rate_delta_negative:{candidate_hit_rate - production_hit_rate:.6f}"
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


def _shadow_ab(item: dict[str, Any]) -> dict[str, Any]:
    latest = item.get("latest_version") or {}
    active = item.get("active_version") or {}
    same_version = bool(latest.get("id")) and int(latest.get("id") or 0) == int(active.get("id") or 0)
    latest_review = ((latest.get("validation") or {}).get("review") or {}) if isinstance(latest.get("validation"), dict) else {}
    latest_metrics = latest.get("metrics") or {}
    active_metrics = active.get("metrics") or {}
    latest_validation = latest.get("validation") or {}
    sessions = int(
        _metric(latest_validation, "live_observation_sessions")
        or _metric(latest_validation, "history_sessions")
        or _metric(latest_validation, "oos_sessions")
        or 0
    )
    challenger_wf = _cost_after_walk_forward(latest_metrics) or 0.0
    champion_wf = _cost_after_walk_forward(active_metrics) or 0.0
    delta = round(challenger_wf - champion_wf, 6)
    ready = sessions >= 20
    status = "no_challenger" if same_version else "auto_rejected" if str(latest_review.get("status") or "") == "rejected" else "review_ready" if ready else "shadow_running"
    return {
        "champion_version": active.get("version"),
        "challenger_version": None if same_version else latest.get("version"),
        "has_challenger": bool(latest.get("id")) and not same_version and str(latest.get("status") or "") == "candidate",
        "observation_sessions": sessions,
        "minimum_sessions": 20,
        "challenger_cost_after_walk_forward_lift": challenger_wf,
        "champion_cost_after_walk_forward_lift": champion_wf,
        "delta": delta,
        "status": status,
        "recommendation": "no_action" if same_version else "rejected" if status == "auto_rejected" else "approve_review" if ready and delta >= 0 else "reject_or_retrain" if ready else "observe",
    }


def build_auto_optimization_status(*, apply_auto_reject: bool = True) -> dict[str, Any]:
    family = model_family_status()
    auto_rejected = _auto_reject_weaker_candidates(family) if apply_auto_reject else []
    if auto_rejected:
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
            "mode": "auto_train_manual_promote",
            "summary": "系统自动训练 challenger、自动驳回弱候选；只有更优候选上线需要人工批准。",
            "auto_allowed": ["data_ready_check", "train_challenger", "compare_candidate", "auto_reject_weaker_candidate", "build_review_queue"],
            "human_required": ["approve_production"],
        },
        "auto_rejected": auto_rejected,
        "review_queue": [item for item in items if item["requires_human"]],
        "items": items,
        "generated_at": datetime.now().isoformat(),
    }


def run_auto_optimization_once(force: bool = False) -> dict[str, Any]:
    before = build_auto_optimization_status(apply_auto_reject=True)
    started: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in before["items"]:
        trigger = str(item.get("trigger") or "")
        if item.get("running"):
            skipped.append({"model_key": item["model_key"], "reason": "training_running"})
            continue
        if trigger in {"train_challenger", "retrain_challenger"} or (force and item.get("status") == "production"):
            result = request_plan_model_training(str(item["model_key"]))
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
        "started_count": len(started),
        "skipped_count": len(skipped),
        "started": started,
        "skipped": skipped,
        "status": after,
    }
