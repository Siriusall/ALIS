"""LLM-assisted data operations with deterministic execution boundaries."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Callable

from app.core.project_env import get_env_value
from app.db.store import _conn
from app.services.llm_review_assistant import _call_deepseek_json, llm_token_budget_status


ALLOWED_ACTIONS = {
    "retry_transient_requests",
    "resolve_superseded_requests",
    "delegate_hard_data_repair",
    "delegate_daily_refresh",
    "escalate_external_dependency",
    "audit_only",
}
SAFE_RETRY_CATEGORIES = {"transient", "empty_pending", "rate_limit"}
EXTERNAL_ESCALATION_CATEGORIES = {"authentication", "permission", "invalid_request", "unknown"}
MAX_ACTIONS = 5
MAX_RETRY_REQUESTS = 5
MIN_EXECUTION_CONFIDENCE = 0.60


def _ensure_table() -> None:
    with _conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS llm_data_operation_cases (
            id BIGSERIAL PRIMARY KEY,
            case_key VARCHAR(64) NOT NULL UNIQUE,
            status VARCHAR(32) NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            proposal_json TEXT NOT NULL DEFAULT '{}',
            governance_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            model VARCHAR(128) NOT NULL DEFAULT '',
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL
        )""")


def list_llm_data_operation_cases(*, limit: int = 20) -> dict[str, Any]:
    _ensure_table()
    with _conn() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM llm_data_operation_cases ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()]
    items = []
    for row in rows:
        item = {key: row.get(key) for key in ("id", "case_key", "status", "model", "created_at", "updated_at")}
        for source, target in (
            ("proposal_json", "proposal"), ("governance_json", "governance"), ("result_json", "execution"),
        ):
            try:
                value = json.loads(row.get(source) or "{}")
            except (TypeError, json.JSONDecodeError):
                value = {}
            item[target] = value if isinstance(value, dict) else {}
        try:
            evidence = json.loads(row.get("evidence_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            evidence = {}
        item["evidence_summary"] = {
            "pending_failure_count": evidence.get("pending_failure_count"),
            "pending_failure_sample_count": evidence.get("pending_failure_sample_count"),
            "failure_category_counts": evidence.get("failure_category_counts") or {},
            "safe_retry_count": len(evidence.get("safe_retry_candidates") or []),
            "superseded_api_count": len(evidence.get("superseded_api_candidates") or []),
            "external_dependency_count": len(evidence.get("external_dependency_failures") or []),
            "oos_hard_data": evidence.get("oos_hard_data") or {},
            "sealed_holdout": evidence.get("sealed_holdout") or {},
            "decision_data": evidence.get("decision_data") or {},
        }
        items.append(item)
    return {
        "ok": True,
        "schema_version": "alis_llm_data_operations_cases_v1",
        "count": len(items),
        "items": items,
        "policy": "read_only_redacted_evidence_summary",
    }


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(float(value or 0.0), 1.0))
    except (TypeError, ValueError):
        return 0.0


def _string_list(value: Any, *, limit: int, item_limit: int) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = []
    return [str(item)[:item_limit] for item in values[:limit] if str(item).strip()]


def _normalise_proposal(payload: dict[str, Any]) -> dict[str, Any]:
    actions = []
    for item in payload.get("actions") or []:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").strip().lower()
        if action not in ALLOWED_ACTIONS:
            continue
        actions.append({
            "action": action,
            "rationale": str(item.get("rationale") or "")[:1000],
            "evidence_refs": _string_list(item.get("evidence_refs"), limit=8, item_limit=160),
            "confidence": _confidence(item.get("confidence")),
        })
    return {
        "schema_version": "alis_llm_data_operations_proposal_v1",
        "summary": str(payload.get("summary") or "")[:1600],
        "actions": actions[:MAX_ACTIONS],
        "escalation": str(payload.get("escalation") or "")[:1200],
        "limitations": _string_list(payload.get("limitations"), limit=8, item_limit=300),
        "policy": "proposal_only; no_sql_shell_credentials_or_unregistered_actions",
    }


def _redact_error(value: Any) -> str:
    text = str(value or "")[:1000]
    text = re.sub(
        r"(?i)(authorization|api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        text,
    )
    return text[:300]


def build_data_operations_evidence(*, cycle_steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    from app.data_platform.services.data_center import SUPERSEDED_API_REPLACEMENTS, list_failed_tushare_requests
    from app.data_platform.services.oos_hard_data import audit_oos_hard_data
    from app.services.data_health import assess_latest_decision_data_health
    from app.services.sealed_strategy_holdout import audit_sealed_strategy_holdout

    pending_result = list_failed_tushare_requests(status="pending", limit=100)
    pending = pending_result.get("items") or []
    failures = [{
        "id": int(item.get("id") or 0),
        "api_name": str(item.get("api_name") or "")[:80],
        "category": str(item.get("error_category") or "unknown")[:40],
        "attempts": int(item.get("attempts") or 0),
        "last_error": _redact_error(item.get("last_error")),
        "last_failed_at": str(item.get("last_failed_at") or ""),
    } for item in pending]
    category_counts: dict[str, int] = {}
    for item in failures:
        category_counts[item["category"]] = category_counts.get(item["category"], 0) + 1
    step_anomalies = []
    for step in cycle_steps or []:
        status = str(((step.get("result") or {}).get("status") if isinstance(step.get("result"), dict) else "") or "")
        if not step.get("ok", True) or status in {"failed", "blocked", "degraded", "partial", "timed_out"}:
            step_anomalies.append({
                "name": str(step.get("name") or "")[:80],
                "status": status or "failed",
                "error": str(step.get("error") or "")[:300],
            })
    oos = audit_oos_hard_data(include_dates=False)
    holdout = audit_sealed_strategy_holdout(include_dates=False)
    decision = assess_latest_decision_data_health()
    return {
        "schema_version": "alis_llm_data_operations_evidence_v1",
        "pending_failure_count": int(pending_result.get("total") or len(failures)),
        "pending_failure_sample_count": len(failures),
        "failure_category_counts": category_counts,
        "safe_retry_candidates": [
            item for item in failures
            if item["category"] in SAFE_RETRY_CATEGORIES and item["attempts"] < 5
        ][:20],
        "external_dependency_failures": [
            item for item in failures
            if item["category"] in EXTERNAL_ESCALATION_CATEGORIES and item["api_name"] not in SUPERSEDED_API_REPLACEMENTS
        ][:20],
        "superseded_api_candidates": [
            {**item, "replacement": SUPERSEDED_API_REPLACEMENTS[item["api_name"]]}
            for item in failures if item["api_name"] in SUPERSEDED_API_REPLACEMENTS
        ][:20],
        "oos_hard_data": {
            "status": oos.get("status"), "eligible_sessions": oos.get("eligible_sessions"),
            "required_sessions": oos.get("required_sessions"), "missing_sessions": oos.get("missing_sessions"),
        },
        "sealed_holdout": {
            "status": holdout.get("status"), "eligible_sessions": holdout.get("eligible_sessions"),
            "required_sessions": holdout.get("required_sessions"), "missing_sessions": holdout.get("missing_sessions"),
        },
        "decision_data": {
            "status": decision.get("status"), "decision_ready": decision.get("decision_ready"),
            "experiment_ready": decision.get("experiment_ready"),
            "issue_codes": [str(item.get("code") or "") for item in decision.get("issues") or []],
        },
        "cycle_step_anomalies": step_anomalies[:20],
        "execution_boundary": {
            "safe": [
                "retry selected transient/empty requests, maximum 5",
                "resolve explicitly registered superseded APIs, maximum 5",
            ],
            "delegated": ["daily refresh, OOS repair, sealed holdout repair use existing deterministic healing"],
            "escalation_only": sorted(EXTERNAL_ESCALATION_CATEGORIES),
            "forbidden": ["arbitrary SQL", "shell", "credential changes", "billing", "quality gate relaxation", "history overwrite"],
        },
    }


def govern_data_operations_proposal(proposal: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    safe_ids = [int(item.get("id") or 0) for item in evidence.get("safe_retry_candidates") or [] if int(item.get("id") or 0)]
    superseded_ids = [int(item.get("id") or 0) for item in evidence.get("superseded_api_candidates") or [] if int(item.get("id") or 0)]
    external = evidence.get("external_dependency_failures") or []
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(proposal.get("actions") or []):
        action = str(item.get("action") or "")
        confidence = _confidence(item.get("confidence"))
        if action in seen:
            rejected.append({"index": index, "action": action, "reason": "duplicate_action"})
            continue
        seen.add(action)
        if action == "retry_transient_requests":
            if confidence < MIN_EXECUTION_CONFIDENCE or not safe_ids:
                rejected.append({"index": index, "action": action, "reason": "insufficient_confidence_or_no_safe_requests"})
                continue
            accepted.append({"action": action, "request_ids": safe_ids[:MAX_RETRY_REQUESTS], "confidence": confidence})
        elif action == "resolve_superseded_requests":
            if confidence < MIN_EXECUTION_CONFIDENCE or not superseded_ids:
                rejected.append({"index": index, "action": action, "reason": "insufficient_confidence_or_no_registered_superseded_requests"})
                continue
            accepted.append({"action": action, "request_ids": superseded_ids[:MAX_RETRY_REQUESTS], "confidence": confidence})
        elif action == "delegate_hard_data_repair":
            needs_repair = (
                str((evidence.get("oos_hard_data") or {}).get("status") or "") != "ready"
                or str((evidence.get("sealed_holdout") or {}).get("status") or "") != "ready"
            )
            if needs_repair:
                accepted.append({"action": action, "execution": "existing_autonomous_healing", "confidence": confidence})
            else:
                rejected.append({"index": index, "action": action, "reason": "hard_data_already_ready"})
        elif action == "delegate_daily_refresh":
            if str((evidence.get("decision_data") or {}).get("status") or "") != "ready":
                accepted.append({"action": action, "execution": "existing_autonomous_healing", "confidence": confidence})
            else:
                rejected.append({"index": index, "action": action, "reason": "decision_data_already_ready"})
        elif action == "escalate_external_dependency":
            if external:
                accepted.append({
                    "action": action,
                    "categories": sorted({str(item.get("category") or "unknown") for item in external}),
                    "affected_apis": sorted({str(item.get("api_name") or "") for item in external})[:20],
                    "confidence": confidence,
                })
            else:
                rejected.append({"index": index, "action": action, "reason": "no_external_dependency_failure"})
        elif action == "audit_only":
            accepted.append({"action": action, "confidence": confidence})
        else:
            rejected.append({"index": index, "action": action, "reason": "action_not_whitelisted"})
    return {
        "schema_version": "alis_llm_data_operations_governance_v1",
        "accepted": accepted,
        "rejected": rejected,
        "policy": "deterministic_evidence_revalidation_and_fixed_execution_templates_only",
    }


def deterministic_data_operations_fallback(evidence: dict[str, Any], *, reason: str) -> dict[str, Any]:
    """Keep the operations bus safe when the advisory model is unavailable or malformed."""
    actions = []
    if evidence.get("safe_retry_candidates"):
        actions.append({
            "action": "retry_transient_requests", "confidence": 1.0,
            "rationale": "确定性证据存在未超过尝试上限的安全重试请求。",
            "evidence_refs": ["safe_retry_candidates"],
        })
    if evidence.get("superseded_api_candidates"):
        actions.append({
            "action": "resolve_superseded_requests", "confidence": 1.0,
            "rationale": "废止接口已有登记的确定性替代路径，可关闭旧失败。",
            "evidence_refs": ["superseded_api_candidates"],
        })
    if (
        str((evidence.get("oos_hard_data") or {}).get("status") or "") != "ready"
        or str((evidence.get("sealed_holdout") or {}).get("status") or "") != "ready"
    ):
        actions.append({
            "action": "delegate_hard_data_repair", "confidence": 1.0,
            "rationale": "硬数据合同存在明确缺口，继续交由既有有界自愈。",
            "evidence_refs": ["oos_hard_data", "sealed_holdout"],
        })
    if str((evidence.get("decision_data") or {}).get("status") or "") != "ready":
        actions.append({
            "action": "delegate_daily_refresh", "confidence": 1.0,
            "rationale": "最新决策数据门未就绪，交由既有单飞日更刷新。",
            "evidence_refs": ["decision_data"],
        })
    if evidence.get("external_dependency_failures"):
        actions.append({
            "action": "escalate_external_dependency", "confidence": 1.0,
            "rationale": "外部依赖故障不允许自动绕过权限或接口语义。",
            "evidence_refs": ["external_dependency_failures"],
        })
    if not actions:
        actions.append({"action": "audit_only", "confidence": 1.0, "rationale": "没有安全执行动作。", "evidence_refs": []})
    return _normalise_proposal({
        "summary": "LLM 运维建议不可用，已按冻结证据使用确定性安全降级。",
        "actions": actions,
        "escalation": "LLM 输出合同异常已留痕，不扩大执行权限。",
        "limitations": [str(reason)[:300]],
    })


def execute_governed_data_operations(
    governance: dict[str, Any], *,
    retry_runner: Callable[..., dict[str, Any]] | None = None,
    superseded_runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if retry_runner is None:
        from app.data_platform.services.data_center import resume_safe_failed_tushare_requests_by_id
        retry_runner = resume_safe_failed_tushare_requests_by_id
    if superseded_runner is None:
        from app.data_platform.services.data_center import resolve_superseded_failed_requests_by_id
        superseded_runner = resolve_superseded_failed_requests_by_id
    actions = []
    escalation = []
    for item in governance.get("accepted") or []:
        action = str(item.get("action") or "")
        if action == "retry_transient_requests":
            try:
                result = retry_runner(item.get("request_ids") or [], limit=MAX_RETRY_REQUESTS)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "attempted": 0, "error": _redact_error(exc)}
            actions.append({"action": action, "result": result})
        elif action == "resolve_superseded_requests":
            try:
                result = superseded_runner(item.get("request_ids") or [], limit=MAX_RETRY_REQUESTS)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "resolved": 0, "execution_error": True, "error": _redact_error(exc)}
            actions.append({"action": action, "result": result})
        elif action in {"delegate_hard_data_repair", "delegate_daily_refresh"}:
            actions.append({"action": action, "status": "delegated", "executor": "existing_autonomous_healing"})
        elif action == "escalate_external_dependency":
            escalation.append({
                "action": action, "categories": item.get("categories") or [],
                "affected_apis": item.get("affected_apis") or [],
                "required_human_authority": "credentials_permissions_billing_or_business_semantics",
            })
        elif action == "audit_only":
            actions.append({"action": action, "status": "completed"})
    unresolved = any(not bool((item.get("result") or {}).get("ok", True)) for item in actions)
    execution_error = any(bool((item.get("result") or {}).get("execution_error")) for item in actions)
    return {
        "ok": not execution_error,
        "schema_version": "alis_llm_data_operations_execution_v1",
        "status": "failed" if execution_error else "escalated" if escalation else "degraded" if unresolved else "completed",
        "actions": actions,
        "escalations": escalation,
        "policy": "no_arbitrary_code_sql_shell_credentials_or_gate_relaxation",
    }


def run_llm_data_operations_cycle(*, cycle_steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Diagnose long-tail data failures and execute only governed safe actions."""
    _ensure_table()
    evidence = build_data_operations_evidence(cycle_steps=cycle_steps)
    case_key = hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    with _conn() as conn:
        existing = conn.execute("SELECT * FROM llm_data_operation_cases WHERE case_key=?", (case_key,)).fetchone()
    if existing and str(dict(existing).get("status") or "") in {"completed", "escalated", "no_action"}:
        item = dict(existing)
        try:
            cached_result = json.loads(item.get("result_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            cached_result = {}
        return {
            "ok": bool(cached_result.get("ok", True)), "status": str(item["status"]),
            "case_id": int(item["id"]), "case_key": case_key, "reused": True,
            "execution": cached_result,
        }
    actionable = bool(
        evidence.get("safe_retry_candidates")
        or evidence.get("superseded_api_candidates")
        or evidence.get("external_dependency_failures")
        or str((evidence.get("oos_hard_data") or {}).get("status") or "") != "ready"
        or str((evidence.get("sealed_holdout") or {}).get("status") or "") != "ready"
        or str((evidence.get("decision_data") or {}).get("status") or "") != "ready"
        or evidence.get("cycle_step_anomalies")
    )
    if not actionable:
        proposal, governance = _normalise_proposal({"summary": "未发现可操作异常", "actions": [{"action": "audit_only", "confidence": 1.0}]}), {"accepted": []}
        result, model, status = {"ok": True, "status": "no_action", "actions": [], "escalations": []}, "", "no_action"
    elif not get_env_value("DEEPSEEK_API_KEY").strip():
        model = "deterministic_fallback"
        proposal = deterministic_data_operations_fallback(evidence, reason="llm_not_configured")
        governance = govern_data_operations_proposal(proposal, evidence)
        result = execute_governed_data_operations(governance)
        result["llm_advisory"] = {"status": "fallback", "error": "llm_not_configured"}
        status = str(result.get("status") or "completed")
    elif str(llm_token_budget_status().get("status") or "") == "exhausted":
        model = "deterministic_fallback"
        proposal = deterministic_data_operations_fallback(evidence, reason="llm_token_budget_exhausted")
        governance = govern_data_operations_proposal(proposal, evidence)
        result = execute_governed_data_operations(governance)
        result["llm_advisory"] = {"status": "fallback", "error": "llm_token_budget_exhausted"}
        status = str(result.get("status") or "completed")
    else:
        system = (
            "你是数据中台运维诊断代理。只能根据冻结证据选择白名单动作，输出严格JSON："
            "summary,actions,escalation,limitations。actions最多5项，每项字段action,rationale,evidence_refs,confidence。"
            "action只能是retry_transient_requests、resolve_superseded_requests、delegate_hard_data_repair、delegate_daily_refresh、"
            "escalate_external_dependency、audit_only。不得生成SQL、Shell、代码、密钥、付费或放宽质量门。"
            "已登记替代路径的废止接口应选择resolve_superseded_requests；没有替代路径的认证、权限、计费、接口永久下线、"
            "参数或字段语义变化必须升级；瞬时网络、冷却后的限流和未达上限的空结果才可建议重试。"
        )
        prompt = "请诊断并提出受约束的数据运维动作：\n" + json.dumps(evidence, ensure_ascii=False, default=str)
        llm_error = ""
        try:
            payload, model = _call_deepseek_json(system=system, user=prompt, purpose="data_operations")
            proposal = _normalise_proposal(payload)
        except ValueError as exc:
            llm_error = str(exc)[:300]
            try:
                payload, model = _call_deepseek_json(
                    system=system + " 上一次响应未通过JSON解析；本次只能输出一个JSON对象，不得使用Markdown或解释文字。",
                    user=prompt,
                    purpose="data_operations_json_retry",
                )
                proposal = _normalise_proposal(payload)
            except Exception as retry_exc:  # noqa: BLE001
                llm_error = f"{llm_error}; retry={str(retry_exc)[:220]}"
                model = "deterministic_fallback"
                proposal = deterministic_data_operations_fallback(evidence, reason=llm_error)
        except Exception as exc:  # noqa: BLE001
            llm_error = str(exc)[:300]
            model = "deterministic_fallback"
            proposal = deterministic_data_operations_fallback(evidence, reason=llm_error)
        governance = govern_data_operations_proposal(proposal, evidence)
        result = execute_governed_data_operations(governance)
        if llm_error:
            result["llm_advisory"] = {"status": "fallback" if model == "deterministic_fallback" else "recovered_after_json_retry", "error": _redact_error(llm_error)}
        status = str(result.get("status") or "completed")
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as conn:
        row = conn.execute(
            """INSERT INTO llm_data_operation_cases(
                   case_key,status,evidence_json,proposal_json,governance_json,result_json,model,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(case_key) DO UPDATE SET status=excluded.status,proposal_json=excluded.proposal_json,
               governance_json=excluded.governance_json,result_json=excluded.result_json,model=excluded.model,updated_at=excluded.updated_at
               RETURNING id""",
            (
                case_key, status, json.dumps(evidence, ensure_ascii=False, default=str),
                json.dumps(proposal, ensure_ascii=False, default=str), json.dumps(governance, ensure_ascii=False, default=str),
                json.dumps(result, ensure_ascii=False, default=str), model, now, now,
            ),
        ).fetchone()
    return {
        "ok": bool(result.get("ok", True)),
        "schema_version": "alis_llm_data_operations_cycle_v1",
        "status": status,
        "case_id": int(dict(row).get("id") or 0),
        "proposal": proposal,
        "governance": governance,
        "execution": result,
        "evidence_summary": {
            "pending_failure_count": evidence.get("pending_failure_count"),
            "safe_retry_count": len(evidence.get("safe_retry_candidates") or []),
            "superseded_api_count": len(evidence.get("superseded_api_candidates") or []),
            "external_dependency_count": len(evidence.get("external_dependency_failures") or []),
        },
    }


def run_deterministic_data_operations_cycle(
    *, cycle_steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute the governed data-operations policy without an LLM call.

    This is the production autonomy entry point.  The LLM variant remains an
    optional research/advisory facility, but it is not allowed to add latency
    or availability risk to the daily deterministic cycle.
    """
    _ensure_table()
    from app.data_platform.services.data_center import (
        resolve_mature_fund_nav_empty_requests,
        resolve_mature_optional_daily_empty_requests,
    )

    fund_nav_cleanup = resolve_mature_fund_nav_empty_requests(limit=500)
    optional_daily_cleanup = resolve_mature_optional_daily_empty_requests(limit=100)
    evidence = build_data_operations_evidence(cycle_steps=cycle_steps)
    case_key = hashlib.sha256(json.dumps(
        {"execution_mode": "deterministic_v2", "evidence": evidence},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")).hexdigest()
    with _conn() as conn:
        existing = conn.execute(
            "SELECT * FROM llm_data_operation_cases WHERE case_key=?", (case_key,)
        ).fetchone()
    if existing and str(dict(existing).get("status") or "") in {
        "completed", "escalated", "no_action",
    }:
        item = dict(existing)
        try:
            cached_result = json.loads(item.get("result_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            cached_result = {}
        return {
            "ok": bool(cached_result.get("ok", True)),
            "schema_version": "alis_deterministic_data_operations_cycle_v2",
            "status": str(item["status"]),
            "case_id": int(item["id"]),
            "case_key": case_key,
            "reused": True,
            "execution_mode": "deterministic",
            "fund_nav_cleanup": fund_nav_cleanup,
            "optional_daily_cleanup": optional_daily_cleanup,
            "execution": cached_result,
        }

    actionable = bool(
        evidence.get("safe_retry_candidates")
        or evidence.get("superseded_api_candidates")
        or evidence.get("external_dependency_failures")
        or str((evidence.get("oos_hard_data") or {}).get("status") or "") != "ready"
        or str((evidence.get("sealed_holdout") or {}).get("status") or "") != "ready"
        or str((evidence.get("decision_data") or {}).get("status") or "") != "ready"
        or evidence.get("cycle_step_anomalies")
    )
    proposal = deterministic_data_operations_fallback(
        evidence,
        reason="production_cycle_uses_deterministic_governance",
    ) if actionable else _normalise_proposal({
        "summary": "未发现可操作异常",
        "actions": [{"action": "audit_only", "confidence": 1.0}],
    })
    governance = govern_data_operations_proposal(proposal, evidence)
    result = execute_governed_data_operations(governance) if actionable else {
        "ok": True, "status": "no_action", "actions": [], "escalations": [],
    }
    status = str(result.get("status") or "completed")
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as conn:
        row = conn.execute(
            """INSERT INTO llm_data_operation_cases(
                   case_key,status,evidence_json,proposal_json,governance_json,result_json,model,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(case_key) DO UPDATE SET status=excluded.status,proposal_json=excluded.proposal_json,
               governance_json=excluded.governance_json,result_json=excluded.result_json,model=excluded.model,updated_at=excluded.updated_at
               RETURNING id""",
            (
                case_key, status, json.dumps(evidence, ensure_ascii=False, default=str),
                json.dumps(proposal, ensure_ascii=False, default=str),
                json.dumps(governance, ensure_ascii=False, default=str),
                json.dumps(result, ensure_ascii=False, default=str),
                "deterministic_governance", now, now,
            ),
        ).fetchone()
    return {
        "ok": bool(result.get("ok", True)),
        "schema_version": "alis_deterministic_data_operations_cycle_v2",
        "status": status,
        "case_id": int(dict(row).get("id") or 0),
        "case_key": case_key,
        "execution_mode": "deterministic",
        "fund_nav_cleanup": fund_nav_cleanup,
        "optional_daily_cleanup": optional_daily_cleanup,
        "proposal": proposal,
        "governance": governance,
        "execution": result,
        "llm_advisory": {"status": "detached_from_production_cycle"},
        "evidence_summary": {
            "pending_failure_count": evidence.get("pending_failure_count"),
            "safe_retry_count": len(evidence.get("safe_retry_candidates") or []),
            "superseded_api_count": len(evidence.get("superseded_api_candidates") or []),
            "external_dependency_count": len(evidence.get("external_dependency_failures") or []),
        },
    }
