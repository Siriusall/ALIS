"""LLM-assisted incident triage with deterministic ownership boundaries."""

from __future__ import annotations

import json
from typing import Any

from app.core.project_env import get_env_value
from app.services.autonomous_healing import classify_step_health
from app.services.llm_review_assistant import _call_deepseek_json, llm_token_budget_status


_AUTO_POLICIES: dict[str, tuple[str, str]] = {
    "sealed_holdout_data_gate": ("data_backfill", "系统按每周期上限继续补齐密封验证集，完成前禁止消费"),
    "decision_data_gate": ("daily_refresh", "系统已暂停正式计划和实验，并委托收盘数据刷新后复核"),
    "periodic_performance_review": ("research_iteration", "系统将复盘信号转成受约束候选并继续验证"),
    "official_policy_documents": ("source_observation", "系统继续观察官方来源，不影响模拟交易安全主链"),
    "research_data_assets": ("research_observation", "未达生产门的数据保持研究态，系统继续积累证据"),
    "simulation_ledger_audit": ("execution_quarantine", "系统冻结新增风险并只读复核账本，不自动改账"),
}

_WAITING_REASONS: dict[str, str] = {
    "decision_data_gate": "等待当前收盘数据刷新任务完成",
    "periodic_performance_review": "等待新交易日、正式 OOS 或影子样本自然成熟",
    "official_policy_documents": "等待官方来源连接恢复后按计划重试",
    "llm_data_operations": "等待上游发布数据或冷却窗口结束",
    "data_operations": "等待上游发布数据或冷却窗口结束",
}

_RETRYABLE_DATA_CATEGORIES = {"empty_pending", "rate_limit", "transient"}


def _result(step: dict[str, Any]) -> dict[str, Any]:
    return step.get("result") if isinstance(step.get("result"), dict) else {}


def _healing_action(steps: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    healing = next((step for step in steps if step.get("name") == "autonomous_self_healing"), {})
    actions = _result(healing).get("actions")
    if not isinstance(actions, list):
        return {}
    return next((item for item in actions if isinstance(item, dict) and item.get("kind") == kind), {})


def _sealed_holdout_resolution(step: dict[str, Any], steps: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    before = _result(step).get("eligible_sessions")
    healing = _healing_action(steps, "sealed_strategy_holdout")
    verification = healing.get("verification") if isinstance(healing.get("verification"), dict) else {}
    after = verification.get("eligible_sessions")
    progress: dict[str, Any] = {
        "before": before,
        "after": after,
        "delta": after - before if isinstance(before, int) and isinstance(after, int) else None,
        "required": _result(step).get("required_sessions"),
        "unit": "交易日",
    }
    if str(healing.get("status") or "") == "bulk_repair_scheduled":
        task = healing.get("task") if isinstance(healing.get("task"), dict) else {}
        return "waiting_condition", {
            "waiting_for": "密封验证集 adj_factor 持久批量补全任务正在推进",
            "progress": progress,
            "retry_policy": f"后台任务 {task.get('id') or '待创建'} 每批最多 20 日，按检查点恢复并在完成后自动复核",
        }
    if isinstance(progress["delta"], int) and progress["delta"] > 0:
        return "waiting_condition", {
            "waiting_for": "等待下一有界补数周期继续推进",
            "progress": progress,
            "retry_policy": "每周期最多修复 5 个明确缺口，完成前禁止消费",
        }
    return "internal_fault", {
        "failure_signature": "sealed_holdout_repair_no_progress",
        "progress": progress,
        "retry_policy": "执行补数后完整率未增长，必须先诊断点时合同或缺失字段，不能只等待下周期",
    }


def _llm_data_operations_resolution(step: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    execution = _result(step).get("execution")
    actions = execution.get("actions") if isinstance(execution, dict) else []
    categories: list[str] = []
    for action in actions if isinstance(actions, list) else []:
        result = action.get("result") if isinstance(action, dict) and isinstance(action.get("result"), dict) else {}
        rows = result.get("results") if isinstance(result.get("results"), list) else []
        categories.extend(str(row.get("category") or "") for row in rows if isinstance(row, dict))
    normalized = {item for item in categories if item}
    if normalized and normalized.issubset(_RETRYABLE_DATA_CATEGORIES):
        return "waiting_condition", {
            "waiting_for": _WAITING_REASONS["data_operations"],
            "retry_policy": "只对安全类别做有界重试；连续为空后必须重新判定 valid_empty 或接口适用性",
            "failure_signature": ",".join(sorted(normalized)),
        }
    return "internal_fault", {
        "failure_signature": "llm_data_operations_degraded_without_retryable_wait",
        "retry_policy": "重新诊断冻结证据，禁止无限重试和危险修改",
    }


def _transient_read_failure(step: dict[str, Any]) -> str:
    """Return a stable retry signature for a failed optional DB read."""
    result = _result(step)
    health = step.get("health") if isinstance(step.get("health"), dict) else {}
    text = " ".join(
        str(value or "")
        for value in (
            step.get("error"),
            health.get("reason"),
            result.get("error"),
            result.get("reason"),
        )
    ).lower()
    if "canceling statement" in text or "querycanceled" in text or "statement timeout" in text or "timeout" in text:
        return "database_query_timeout"
    if "connection" in text or "could not connect" in text or "server closed" in text:
        return "database_unavailable"
    return ""


def _resolution(step: dict[str, Any], steps: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    name = str(step.get("name") or "")
    if name == "sealed_holdout_data_gate":
        return _sealed_holdout_resolution(step, steps)
    if name == "research_data_assets":
        transient = _transient_read_failure(step)
        if transient:
            return "waiting_condition", {
                "waiting_for": "研究数据资产只读查询恢复或完成下一次有界重试",
                "failure_signature": transient,
                "retry_policy": "只读查询按有限退避重试；连续失败时继续保留降级证据，不把空结果当作研究缺口",
            }
        gaps = _result(step).get("gaps")
        return "internal_fault", {
            "failure_signature": "research_capability_not_integrated",
            "evidence": gaps if isinstance(gaps, list) else [],
            "retry_policy": "缺少数据接入或标注基准时由系统建设能力，时间本身不会补齐",
        }
    if name in {"llm_data_operations", "data_operations"}:
        return _llm_data_operations_resolution(step)
    if name in _WAITING_REASONS:
        result = _result(step)
        evidence: dict[str, Any] = {}
        if name == "decision_data_gate":
            healing = _healing_action(steps, "latest_decision_data")
            refresh_status = str(healing.get("status") or "")
            start = healing.get("start") if isinstance(healing.get("start"), dict) else {}
            refresh_category = str(start.get("category") or "")
            if refresh_status == "refresh_cooling_down" and refresh_category.startswith("internal_fault"):
                return "internal_fault", {
                    "failure_signature": refresh_category,
                    "retry_policy": "内部切片故障按冷却窗口复核；再次失败时继续诊断有界分页和数据契约",
                }
            if refresh_status not in {
                "refresh_started", "refresh_scheduled", "refresh_already_running",
                "refresh_cooling_down", "completed", "ready",
            }:
                return "internal_fault", {
                    "failure_signature": "decision_data_blocked_without_active_refresh",
                    "retry_policy": "未发现正在运行或已完成的刷新任务，系统必须重新调度并验证，不能只等待",
                }
            evidence = {
                "refresh_status": refresh_status,
                "refresh_category": refresh_category,
                "retry_after_seconds": start.get("retry_after_seconds"),
                "target_trade_date": ((result.get("readiness") or {}).get("target_trade_date")
                                      if isinstance(result.get("readiness"), dict) else None),
            }
        elif name == "official_policy_documents":
            failed_sources = result.get("failed_sources") or []
            sources = result.get("sources") if isinstance(result.get("sources"), list) else []
            failed_errors = [
                str(item.get("error") or "").lower()
                for item in sources
                if isinstance(item, dict) and item.get("status") == "failed"
            ]
            transient_markers = ("ssl", "timeout", "connection", "remote", "eof", "temporarily")
            if failed_sources and (not failed_errors or any(not any(marker in error for marker in transient_markers) for error in failed_errors)):
                return "internal_fault", {
                    "failure_signature": "official_source_non_transient_failure",
                    "evidence": {"failed_sources": failed_sources},
                    "retry_policy": "非瞬时来源错误必须进入采集契约诊断，不能仅等待站点恢复",
                }
            evidence = {"failed_sources": result.get("failed_sources") or []}
        return "waiting_condition", {
            "waiting_for": _WAITING_REASONS[name],
            "retry_policy": "条件到达后立即复核；到期仍失败或连续无进展时自动转为内部故障",
            "evidence": evidence,
        }
    return "internal_fault", {
        "failure_signature": f"{name or 'unknown'}:{str((_result(step).get('status') or step.get('error') or 'failed'))[:120]}",
        "retry_policy": "系统立即诊断并执行受约束修复，不能仅等待时间经过",
    }


def _step_cases(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for step in steps:
        name = str(step.get("name") or "")
        if name in {"llm_incident_supervisor", "autonomous_self_healing"}:
            continue
        health = step.get("health") if isinstance(step.get("health"), dict) else classify_step_health(step)
        status = str(health.get("status") or "")
        if status not in {"failed", "degraded"}:
            continue
        action, policy = _AUTO_POLICIES.get(name, ("bounded_recheck", "系统保留隔离并在后续周期重新诊断，未授权任何危险修改"))
        resolution_class, resolution_evidence = _resolution(step, steps)
        if resolution_class == "internal_fault" and name == "sealed_holdout_data_gate":
            action = "diagnose_backfill_contract"
            policy = "补数已经执行但完整率没有增长，系统转入点时合同与缺失字段诊断"
        elif resolution_class == "internal_fault" and name == "research_data_assets":
            action = "build_research_capability"
            policy = "系统将缺失的数据接入或标注基准作为内部能力缺口推进，不能仅等待"
        elif resolution_class == "internal_fault" and name in {"llm_data_operations", "data_operations"}:
            action = "diagnose_data_operations"
            policy = "系统重新诊断数据运维失败类型，禁止无上限重复请求"
        case = {
            "module": name,
            "health": status,
            "reason": str(health.get("reason") or step.get("error") or "module_returned_not_ok")[:300],
            "owner": "system",
            "action": action,
            "policy": policy,
            "owner_action_required": False,
            "resolution_class": resolution_class,
        }
        case.update(resolution_evidence)
        cases.append(case)
    return cases


def _external_escalations(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    data_ops = next((step for step in steps if step.get("name") in {"data_operations", "llm_data_operations"}), {})
    result = data_ops.get("result") if isinstance(data_ops.get("result"), dict) else {}
    execution = result.get("execution") if isinstance(result.get("execution"), dict) else {}
    escalations = execution.get("escalations") if isinstance(execution.get("escalations"), list) else []
    return [item for item in escalations if isinstance(item, dict)]


def _llm_explanations(cases: list[dict[str, Any]]) -> tuple[dict[str, dict[str, str]], str, str]:
    if not cases:
        return {}, "not_needed", ""
    if not get_env_value("DEEPSEEK_API_KEY").strip():
        return {}, "deterministic_fallback", "llm_not_configured"
    if str(llm_token_budget_status().get("status") or "") == "exhausted":
        return {}, "deterministic_fallback", "llm_token_budget_exhausted"
    system = (
        "你是ALIS模拟投资系统的异常总监。根据冻结证据，用用户能理解的中文解释每个问题的原因、系统已经采取的保护、"
        "下一次如何验证。输出严格JSON：cases数组；每项只能包含module,summary,system_action,next_check。"
        "不得提出SQL、Shell、改代码、改密钥、改历史、放宽数据门、修改Champion或交易指令。"
        "不得改变resolution_class、owner、action、progress或retry_policy。"
        "不得把系统内部等待事项转交给用户；只有凭证、权限、计费、机器/数据库不可用等外部条件才能说明需要所有者。"
    )
    prompt = "请解释以下由确定性治理冻结的异常，不改变其归属和动作：\n" + json.dumps(cases, ensure_ascii=False)
    try:
        payload, model = _call_deepseek_json(system=system, user=prompt, purpose="incident_supervision")
        llm_status = "completed"
    except ValueError:
        payload, model = _call_deepseek_json(
            system=system + " 上一次响应不是合法JSON；本次只能输出一个JSON对象，不得使用Markdown或解释文字。",
            user=prompt,
            purpose="incident_supervision_json_retry",
        )
        llm_status = "recovered_after_json_retry"
    explanations: dict[str, dict[str, str]] = {}
    for item in payload.get("cases") or []:
        if not isinstance(item, dict):
            continue
        module = str(item.get("module") or "")
        if module not in {case["module"] for case in cases}:
            continue
        explanations[module] = {
            "summary": str(item.get("summary") or "")[:300],
            "system_action": str(item.get("system_action") or "")[:300],
            "next_check": str(item.get("next_check") or "")[:300],
        }
    return explanations, llm_status, model


def run_incident_supervisor(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Explain every cycle issue while deterministic rules retain control."""
    cases = _step_cases(steps)
    external = _external_escalations(steps)
    for item in external:
        cases.append({
            "module": "external_dependency",
            "health": "blocked",
            "reason": "外部认证、权限、计费或业务语义需要所有者提供条件",
            "owner": "owner",
            "action": "restore_external_authority",
            "policy": "系统不会自动修改凭证、购买服务或猜测业务口径",
            "owner_action_required": True,
            "resolution_class": "owner_required",
            "evidence": item,
        })
    llm_error = ""
    try:
        explanations, llm_status, model = _llm_explanations(cases)
    except Exception as exc:  # noqa: BLE001 - deterministic ownership must survive LLM failure
        explanations, llm_status, model = {}, "deterministic_fallback", ""
        llm_error = str(exc)[:300]
    for case in cases:
        explanation = explanations.get(str(case.get("module") or ""), {})
        case.update({key: value for key, value in explanation.items() if value})
    owner_cases = [case for case in cases if case.get("owner_action_required")]
    waiting_cases = [case for case in cases if case.get("resolution_class") == "waiting_condition"]
    internal_faults = [case for case in cases if case.get("resolution_class") == "internal_fault"]
    return {
        "ok": True,
        "schema_version": "alis_incident_supervisor_v2",
        "status": "owner_action_required" if owner_cases else "self_managed" if cases else "healthy",
        "summary": (
            f"发现 {len(cases)} 项：{len(waiting_cases)} 项等待条件，{len(internal_faults)} 项内部故障；系统正在消化"
            if cases and not owner_cases else
            f"有 {len(owner_cases)} 项外部条件需要所有者提供"
            if owner_cases else "未发现需要处置的问题"
        ),
        "auto_managed_count": len(cases) - len(owner_cases),
        "waiting_condition_count": len(waiting_cases),
        "internal_fault_count": len(internal_faults),
        "owner_action_required_count": len(owner_cases),
        "cases": cases,
        "llm": {"status": llm_status, "model": model, "error": llm_error},
        "policy": "waiting_conditions_have_deadlines_and_promote_to_internal_fault_on_no_progress; deterministic_governance_owns_actions; owner_only_for_external_authority",
    }
