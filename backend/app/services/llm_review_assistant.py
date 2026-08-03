from __future__ import annotations

import json
import re
import threading
from typing import Any

import httpx
from datetime import datetime

from app.core.project_env import get_env_value
from app.services.decision_store import build_review_outcome_suggestion, list_decision_snapshots
from app.services.review_learning import decision_outcomes
from app.db.store import _conn


ALLOWED_ERROR_TYPES = {"无错误", "数据质量", "模型失效", "估值判断", "风险执行", "交易执行", "研究遗漏", "其他"}
_usage_lock = threading.Lock()


def _token_limits() -> tuple[int, int]:
    try: per_request = max(200, min(int(get_env_value("DEEPSEEK_MAX_TOKENS_PER_REQUEST", "2400")), 8000))
    except ValueError: per_request = 2400
    try: daily = max(per_request, int(get_env_value("DEEPSEEK_DAILY_TOKEN_BUDGET", "10000000")))
    except ValueError: daily = 10_000_000
    return per_request, daily


def _ensure_usage_table() -> None:
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS llm_token_usage (
            id BIGSERIAL PRIMARY KEY, provider VARCHAR(32) NOT NULL, model VARCHAR(128) NOT NULL,
            purpose VARCHAR(64) NOT NULL, prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0, total_tokens INTEGER NOT NULL DEFAULT 0,
            created_at VARCHAR(64) NOT NULL
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_token_usage(created_at)")


def llm_token_budget_status() -> dict[str, Any]:
    _ensure_usage_table(); per_request, daily = _token_limits(); day = datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        row = c.execute("""SELECT COALESCE(SUM(total_tokens),0) used,COUNT(*) requests
            FROM llm_token_usage WHERE created_at>=? AND created_at<?""", (day, f"{day}T23:59:59.999999")).fetchone()
    used = int(dict(row).get("used") or 0); requests = int(dict(row).get("requests") or 0)
    return {"ok": True, "date": day, "used_tokens": used, "daily_budget": daily,
        "remaining_tokens": max(0,daily-used), "per_request_limit": per_request, "requests": requests,
        "status": "exhausted" if used >= daily else "warning" if used >= daily*.8 else "available"}


def _call_deepseek_json(*, system: str, user: str, purpose: str) -> tuple[dict[str, Any], str]:
    # Keep budget check, provider call and usage persistence in one critical
    # section so concurrent UI/background requests cannot overspend the budget.
    with _usage_lock:
        return _call_deepseek_json_unlocked(system=system, user=user, purpose=purpose)


def _call_deepseek_json_unlocked(*, system: str, user: str, purpose: str) -> tuple[dict[str, Any], str]:
    api_key = get_env_value("DEEPSEEK_API_KEY").strip()
    if not api_key: raise ValueError("deepseek_api_key_not_configured")
    try: max_input_chars = max(2000,min(int(get_env_value("DEEPSEEK_MAX_INPUT_CHARS","16000")),100000))
    except ValueError: max_input_chars = 16000
    if len(user) > max_input_chars:
        user = user[:max_input_chars] + "\n[输入已按Token预算截断]"
    estimated_prompt = max(1,(len(system)+len(user)+3)//4)
    budget = llm_token_budget_status(); per_request = int(budget["per_request_limit"])
    allowed = min(per_request, int(budget["remaining_tokens"])-estimated_prompt)
    if allowed < 200: raise RuntimeError("llm_token_budget_exhausted")
    base_url = get_env_value("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = get_env_value("DEEPSEEK_MODEL", "deepseek-v4-flash").strip() or "deepseek-v4-flash"
    timeout = max(5.0, min(float(get_env_value("DEEPSEEK_TIMEOUT_SECONDS", "60")), 180.0))
    with httpx.Client(timeout=timeout) as client:
        response = client.post(f"{base_url}/chat/completions", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model":model,"temperature":.1,"max_tokens":allowed,"response_format":{"type":"json_object"},
                  "messages":[{"role":"system","content":system},{"role":"user","content":user}]})
    if response.status_code >= 400: raise RuntimeError(f"deepseek_http_{response.status_code}: {response.text[:300]}")
    body=response.json(); usage=body.get("usage") or {}; content=str((((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""))
    prompt=int(usage.get("prompt_tokens") or 0); completion=int(usage.get("completion_tokens") or 0); total=int(usage.get("total_tokens") or prompt+completion)
    with _conn() as c:
        c.execute("""INSERT INTO llm_token_usage(provider,model,purpose,prompt_tokens,completion_tokens,total_tokens,created_at)
            VALUES('deepseek',?,?,?,?,?,?)""", (model,purpose,prompt,completion,total,datetime.now().isoformat(timespec="seconds")))
    return _extract_json(content), model


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.S)
    if not cleaned:
        raise ValueError("llm_response_not_json")
    try:
        payload = json.loads(cleaned)
        if isinstance(payload, dict):
            return payload
        raise ValueError("llm_response_not_object")
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", cleaned):
        try:
            payload, _end = decoder.raw_decode(cleaned[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("llm_response_not_json")


def generate_review_draft(decision_snapshot_id: int) -> dict[str, Any]:
    decision = next((row for row in list_decision_snapshots(limit=500) if int(row.get("id") or 0) == int(decision_snapshot_id)), None)
    if not decision:
        raise ValueError("decision_snapshot_not_found")
    fixed_outcomes = decision_outcomes(decision_snapshot_id)
    fallback = build_review_outcome_suggestion(decision_snapshot_id) or {"ok": False, "reason": "后续价格证据不足"}
    evidence = {
        "decision_snapshot": decision,
        "fixed_horizon_outcomes": fixed_outcomes,
        "execution_or_latest_price_outcome": fallback,
        "evidence_priority": "优先使用固定周期结果；有真实成交时再使用成交批次归因",
    }
    system = (
        "你是长线投资系统的人工复核助手。只能根据输入的冻结证据生成复核草稿，不得补造事实。"
        "固定周期结果中的directional_return已经按买卖方向调整：正数代表方向正确，负数代表方向错误。"
        "market_contribution、industry_contribution、selection_contribution分别表示市场、行业和选股贡献。"
        "输出严格JSON，字段为summary,outcome_assessment,error_type,attribution,lessons,evidence_points,confidence,limitations。"
        "error_type只能是：无错误、数据质量、模型失效、估值判断、风险执行、交易执行、研究遗漏、其他。"
        "evidence_points和limitations必须是字符串数组；confidence为0到1。证据不成熟时必须明确说明观察周期，而不是声称完全没有数据。"
    )
    user = "请复核以下历史决策，并生成可供人工直接采纳或修改的中文草稿：\n" + json.dumps(evidence, ensure_ascii=False, default=str)
    draft, model = _call_deepseek_json(system=system,user=user,purpose="decision_review")
    error_type = str(draft.get("error_type") or "其他")
    if error_type not in ALLOWED_ERROR_TYPES:
        error_type = "其他"
    return {"ok": True, "model": model, "decision_snapshot_id": decision_snapshot_id,
        "draft": {"summary": str(draft.get("summary") or ""), "outcome_assessment": str(draft.get("outcome_assessment") or ""),
            "error_type": error_type, "attribution": str(draft.get("attribution") or ""), "lessons": str(draft.get("lessons") or ""),
            "evidence_points": [str(x) for x in (draft.get("evidence_points") or [])],
            "confidence": max(0.0, min(float(draft.get("confidence") or 0.0), 1.0)),
            "limitations": [str(x) for x in (draft.get("limitations") or [])]},
        "disclaimer": "LLM生成的是复核草稿；采纳后会连同证据和模型版本一起保存。"}


def triage_review_issue(item: dict[str, Any]) -> dict[str, Any]:
    """Judge any review item before it is allowed to enter the human queue."""
    api_key = get_env_value("DEEPSEEK_API_KEY").strip()
    if not api_key:
        return {"can_handle": False, "confidence": 0.0, "summary": "LLM未配置，无法预审",
                "escalation_reason": "需要人工处理：LLM服务不可用", "model": ""}
    kind = str(item.get("kind") or "")
    hard_human_gate = kind in {"model_release", "system_exception", "data_quality"}
    system = (
        "你是投资系统的问题分诊器。先判断证据是否足以形成明确结论，以及系统是否能安全自动执行。"
        "输出严格JSON：can_handle(boolean),recommended_action,summary,confidence(0到1),escalation_reason。"
        "涉及模型上线、真实交易、后台故障修复、数据补全或数据质量放行时，can_handle必须为false，"
        "但仍需给出中文判断和交给人工的具体原因。不要把缺少执行权限误写成证据不足。"
    )
    evidence = {"kind": kind, "title": item.get("title"), "reason": item.get("reason"),
                "status": item.get("status"), "subject": item.get("subject"), "evidence": item.get("evidence") or {}}
    draft, model = _call_deepseek_json(system=system,user=json.dumps(evidence,ensure_ascii=False,default=str),purpose="issue_triage")
    can_handle = bool(draft.get("can_handle")) and not hard_human_gate
    return {"can_handle": can_handle, "recommended_action": str(draft.get("recommended_action") or ""),
        "summary": str(draft.get("summary") or ""), "confidence": max(0.0,min(float(draft.get("confidence") or 0),1.0)),
        "escalation_reason": str(draft.get("escalation_reason") or ("属于必须人工确认的高风险事项" if hard_human_gate else "")),
        "model": model, "draft": draft}
