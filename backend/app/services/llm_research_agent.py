from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from app.core.project_env import get_env_value
from app.db.store import _conn
from app.services.llm_review_assistant import _call_deepseek_json, llm_token_budget_status
from app.services.performance_reports import latest_performance_learning_context


ALLOWED_TARGETS = {"factor", "model", "strategy", "data"}


def _confidence(value: Any) -> float:
    if isinstance(value, str):
        mapped = {"low": 0.3, "medium": 0.5, "high": 0.75, "低": 0.3, "中": 0.5, "高": 0.75}
        if value.strip().lower() in mapped:
            return mapped[value.strip().lower()]
    try:
        return max(0.0, min(float(value or 0.0), 1.0))
    except (TypeError, ValueError):
        return 0.0


def _string_list(value: Any, *, limit: int, item_limit: int) -> list[str]:
    if isinstance(value, list) and len(value) > 1 and all(isinstance(item, str) and len(item) == 1 for item in value):
        value = "".join(value)
    values = value if isinstance(value, list) else [value] if isinstance(value, str) and value.strip() else []
    return [str(item)[:item_limit] for item in values[:limit] if str(item).strip()]


def _ensure_tables() -> None:
    with _conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS llm_research_proposals (
            id BIGSERIAL PRIMARY KEY,
            proposal_key VARCHAR(64) NOT NULL UNIQUE,
            status VARCHAR(32) NOT NULL DEFAULT 'proposed',
            source_period VARCHAR(32) NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            model VARCHAR(128) NOT NULL DEFAULT '',
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL
        )""")
        conn.execute(
            "ALTER TABLE llm_research_proposals ADD COLUMN IF NOT EXISTS evidence_json TEXT NOT NULL DEFAULT '{}'"
        )


def _json_object(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        decoded = {}
    return decoded if isinstance(decoded, dict) else {}


def list_llm_research_proposals(*, limit: int = 50) -> dict[str, Any]:
    """Return proposal, deterministic assessment and route evidence without executing work."""
    _ensure_tables()
    n = max(1, min(int(limit or 50), 200))
    with _conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS llm_research_proposal_assessments (
            id BIGSERIAL PRIMARY KEY, proposal_id BIGINT NOT NULL UNIQUE, status VARCHAR(32) NOT NULL,
            assessment_json TEXT NOT NULL DEFAULT '{}', created_at VARCHAR(64) NOT NULL, updated_at VARCHAR(64) NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS llm_research_routes (
            id BIGSERIAL PRIMARY KEY, proposal_id BIGINT NOT NULL, hypothesis_index INTEGER NOT NULL,
            route_key VARCHAR(64) NOT NULL, status VARCHAR(32) NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}', created_at VARCHAR(64) NOT NULL, updated_at VARCHAR(64) NOT NULL,
            UNIQUE(proposal_id,hypothesis_index,route_key)
        )""")
        proposals = [dict(row) for row in conn.execute(
            "SELECT * FROM llm_research_proposals ORDER BY id DESC LIMIT ?", (n,),
        ).fetchall()]
        ids = [int(row["id"]) for row in proposals]
        assessments: dict[int, dict[str, Any]] = {}
        routes: dict[int, list[dict[str, Any]]] = {}
        if ids:
            placeholders = ",".join("?" for _ in ids)
            for row in conn.execute(
                f"SELECT * FROM llm_research_proposal_assessments WHERE proposal_id IN ({placeholders})", ids,
            ).fetchall():
                item = dict(row)
                item["assessment"] = _json_object(item.pop("assessment_json", "{}"))
                assessments[int(item["proposal_id"])] = item
            for row in conn.execute(
                f"SELECT * FROM llm_research_routes WHERE proposal_id IN ({placeholders}) ORDER BY id", ids,
            ).fetchall():
                item = dict(row)
                item["evidence"] = _json_object(item.pop("evidence_json", "{}"))
                item["result"] = _json_object(item.pop("result_json", "{}"))
                routes.setdefault(int(item["proposal_id"]), []).append(item)
    items = []
    for row in proposals:
        proposal_id = int(row["id"])
        row["payload"] = _json_object(row.pop("payload_json", "{}"))
        row["evidence_snapshot"] = _json_object(row.pop("evidence_json", "{}"))
        row["assessment"] = assessments.get(proposal_id)
        row["routes"] = routes.get(proposal_id, [])
        items.append(row)
    from app.services.llm_research_governance import llm_research_proposal_quality_report

    return {
        "ok": True,
        "schema_version": "alis_llm_research_proposal_audit_v1",
        "count": len(items),
        "items": items,
        "quality_report": llm_research_proposal_quality_report(limit=200),
        "policy": "read_only; proposal_evidence_only; no_generation_or_execution",
    }


def _normalise_proposal(
    payload: dict[str, Any],
    context: dict[str, Any],
    *,
    evidence_catalog: set[str] | None = None,
) -> dict[str, Any]:
    hypotheses = []
    for item in payload.get("hypotheses") or []:
        if not isinstance(item, dict):
            continue
        target = str(item.get("target") or "").lower()
        if target not in ALLOWED_TARGETS:
            continue
        confidence = _confidence(item.get("confidence"))
        proposed_refs = _string_list(item.get("evidence_refs"), limit=8, item_limit=200)
        supported_refs = (
            [ref for ref in proposed_refs if ref in evidence_catalog]
            if evidence_catalog is not None
            else proposed_refs
        )
        unsupported_refs = (
            [ref for ref in proposed_refs if ref not in evidence_catalog]
            if evidence_catalog is not None
            else []
        )
        hypotheses.append({
            "title": str(item.get("title") or "")[:160],
            "target": target,
            "rationale": str(item.get("rationale") or "")[:1200],
            "proposed_experiment": str(item.get("proposed_experiment") or "")[:1200],
            "evidence_refs": supported_refs,
            "unsupported_evidence_refs": unsupported_refs,
            "confidence": confidence,
            "safety_notes": str(item.get("safety_notes") or "")[:600],
        })
    return {
        "schema_version": "alis_llm_research_proposal_v1",
        "summary": str(payload.get("summary") or "")[:1600],
        "hypotheses": hypotheses[:3],
        "limitations": _string_list(payload.get("limitations"), limit=8, item_limit=300),
        "source_period": str(context.get("period") or ""),
        "policy": "proposal_only; deterministic_schema_and_OOS_shadow_gates_required; no_production_write",
    }


def _frozen_evidence(context: dict[str, Any]) -> dict[str, Any]:
    catalog: list[str] = []
    for item in context.get("learning_signals") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if code:
            catalog.append(f"learning_signals.{code}={item.get('value')}")
    for scope, values in (context.get("error_breakdown") or {}).items():
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            catalog.append(f"error_breakdown.{scope}.{key}={value}")
    for key, value in (context.get("benchmark_coverage") or {}).items():
        catalog.append(f"benchmark_coverage.{key}={value}")
    return {
        "period": context.get("period"),
        "learning_signals": context.get("learning_signals") or [],
        "error_breakdown": context.get("error_breakdown") or {},
        "benchmark_coverage": context.get("benchmark_coverage") or {},
        "evidence_catalog": sorted(set(catalog)),
        "policy_boundary": "只能提出研究假设；不能要求交易、晋升、回退、数据库修改或绕过样本外验证。",
    }


def run_llm_research_agent() -> dict[str, Any]:
    """Produce bounded research hypotheses; deterministic services own all changes."""
    _ensure_tables()
    if not get_env_value("DEEPSEEK_API_KEY").strip():
        return {"ok": True, "status": "skipped_not_configured", "proposal_count": 0}
    context = latest_performance_learning_context()
    evidence = _frozen_evidence(context)
    key_payload = {key: value for key, value in evidence.items() if key != "evidence_catalog"}
    key = hashlib.sha256(json.dumps(key_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    with _conn() as conn:
        existing = conn.execute("SELECT * FROM llm_research_proposals WHERE proposal_key=?", (key,)).fetchone()
    if existing:
        item = dict(existing)
        try:
            repaired = _normalise_proposal(
                json.loads(item.get("payload_json") or "{}"),
                context,
                evidence_catalog=set(evidence["evidence_catalog"]),
            )
        except (TypeError, json.JSONDecodeError):
            repaired = {}
        if repaired and json.dumps(repaired, ensure_ascii=False, sort_keys=True) != json.dumps(json.loads(item.get("payload_json") or "{}"), ensure_ascii=False, sort_keys=True):
            now = datetime.now().isoformat(timespec="seconds")
            with _conn() as conn:
                row = conn.execute(
                    "UPDATE llm_research_proposals SET payload_json=?,evidence_json=?,updated_at=? WHERE id=? RETURNING *",
                    (json.dumps(repaired, ensure_ascii=False), json.dumps(evidence, ensure_ascii=False), now, int(item["id"])),
                ).fetchone()
            item = dict(row)
        elif not _json_object(item.get("evidence_json")):
            now = datetime.now().isoformat(timespec="seconds")
            with _conn() as conn:
                row = conn.execute(
                    "UPDATE llm_research_proposals SET evidence_json=?,updated_at=? WHERE id=? RETURNING *",
                    (json.dumps(evidence, ensure_ascii=False), now, int(item["id"])),
                ).fetchone()
            item = dict(row)
        return {"ok": True, "status": "unchanged", "proposal_count": 1, "proposal": item}
    budget = llm_token_budget_status()
    if str(budget.get("status") or "") == "exhausted":
        return {"ok": True, "status": "skipped_budget_exhausted", "proposal_count": 0, "budget": budget}
    system = (
        "你是A股模拟投资系统的受约束研究代理。只能根据输入的结构化复盘证据提出可验证研究假设。"
        "严禁给出买卖指令、修改生产版本、批准候选、绕过OOS/影子观察或声称收益保证。"
        "输出严格JSON：summary,hypotheses,limitations。hypotheses最多3项，每项字段为"
        "title,target,rationale,proposed_experiment,evidence_refs,confidence,safety_notes。"
        "target只能为factor、model、strategy、data。证据不足时提出数据/观察需求，不得编造结论。"
        "evidence_refs只能逐字选择输入evidence_catalog中的完整字符串，禁止自造引用。"
    )
    user_prompt = (
        "请基于以下冻结复盘上下文提出下一轮研究假设：\n"
        + json.dumps(evidence, ensure_ascii=False, default=str)
    )
    retry_count = 0
    try:
        payload, model = _call_deepseek_json(
            system=system,
            user=user_prompt,
            purpose="research_proposal",
        )
    except ValueError as exc:
        if str(exc) not in {"llm_response_not_json", "llm_response_not_object"}:
            raise
        retry_count = 1
        try:
            payload, model = _call_deepseek_json(
                system=(
                    system
                    + " 上一次输出未形成完整JSON。本次禁止解释、禁止Markdown，"
                    "直接输出一个简洁JSON对象；每个文本字段不超过300字。"
                ),
                user=user_prompt,
                purpose="research_proposal_json_retry",
            )
        except ValueError as retry_exc:
            if str(retry_exc) not in {
                "llm_response_not_json",
                "llm_response_not_object",
            }:
                raise
            return {
                "ok": True,
                "status": "skipped_invalid_response",
                "proposal_count": 0,
                "retry_count": retry_count,
                "error": str(retry_exc),
                "production_change_forbidden": True,
            }
    proposal = _normalise_proposal(
        payload,
        context,
        evidence_catalog=set(evidence["evidence_catalog"]),
    )
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as conn:
        row = conn.execute(
            """INSERT INTO llm_research_proposals
               (proposal_key,status,source_period,payload_json,evidence_json,model,created_at,updated_at)
               VALUES(?,'proposed',?,?,?,?,?,?) RETURNING *""",
            (
                key,
                str(context.get("period") or ""),
                json.dumps(proposal, ensure_ascii=False),
                json.dumps(evidence, ensure_ascii=False),
                model,
                now,
                now,
            ),
        ).fetchone()
    return {"ok": True, "status": "proposed", "proposal_count": 1, "proposal": dict(row), "budget": llm_token_budget_status(), "retry_count": retry_count}
