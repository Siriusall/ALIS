from __future__ import annotations

import threading
import os
import time
from typing import Any

from app.core.project_env import get_env_value
from app.services.llm_review_assistant import generate_review_draft, triage_review_issue
from app.services.manual_review_queue import (
    build_manual_review_queue,
    list_manual_review_triage,
    save_manual_review_triage,
    submit_manual_review_action,
)
from app.services.review_learning import decision_outcomes

_lock = threading.Lock()
_running = False
_scheduler_started = False


def _retryable_llm_configuration_failure(state: dict[str, Any]) -> bool:
    """Retry persisted pre-triage failures after an API key is configured."""
    if not get_env_value("DEEPSEEK_API_KEY").strip():
        return False
    text = " ".join(str(state.get(key) or "") for key in ("summary", "escalation_reason"))
    return not str(state.get("model") or "").strip() and any(marker in text for marker in (
        "deepseek_api_key_not_configured",
        "LLM未配置",
        "LLM服务不可用",
    ))


def run_manual_review_triage_once(limit: int = 100) -> dict[str, Any]:
    """LLM-first review: resolve safe evidence-complete items, escalate the rest."""
    raw = build_manual_review_queue(limit=limit, human_only=False)
    known = list_manual_review_triage()
    processed = auto_resolved = escalated = awaiting_outcome = 0
    errors: list[dict[str, str]] = []
    try: cycle_limit = max(1,min(int(os.getenv("DEEPSEEK_TRIAGE_MAX_ITEMS_PER_CYCLE","10")),50))
    except ValueError: cycle_limit = 10
    candidates = []
    for item in raw.get("items") or []:
        key = str(item.get("key") or "")
        state = known.get(key) or {}
        if item.get("kind") == "decision_review":
            decision_id = int(key.split(":", 1)[1])
            if not decision_outcomes(decision_id):
                save_manual_review_triage(
                    review_key=key, item_kind="decision_review", status="awaiting_outcome",
                    can_handle=False, confidence=0, summary="等待固定周期收益结果",
                    escalation_reason="尚未达到首个5交易日观察点，不需要人工判断",
                    draft={"result_maturity": 0, "available_horizons": []}, model="",
                )
                awaiting_outcome += 1
                continue
        if state.get("status") not in {"auto_resolved", "escalated"} or _retryable_llm_configuration_failure(state):
            candidates.append(item)
    for item in candidates[:cycle_limit]:
        key = str(item.get("key") or "")
        if not key:
            continue
        try:
            if item.get("kind") == "decision_review":
                decision_id = int(key.split(":", 1)[1])
                result = generate_review_draft(decision_id)
                draft = result.get("draft") or {}
                mature = decision_outcomes(decision_id)
                available_horizons = sorted({int(row.get("horizon_sessions") or 0) for row in mature})
                draft["result_maturity"] = len(set(available_horizons) & {5, 20, 60}) / 3
                draft["available_horizons"] = available_horizons
                confidence = float(draft.get("confidence") or 0)
                can_handle = bool(mature) and confidence >= 0.60 and bool(draft.get("outcome_assessment"))
                summary = str(draft.get("outcome_assessment") or draft.get("summary") or "")
                escalation_reason = "" if can_handle else ("尚无固定周期收益结果" if not mature else "LLM置信度不足，需人工判断")
                model = str(result.get("model") or "")
                if can_handle:
                    submit_manual_review_action(review_key=key,item_kind=str(item["kind"]),action="llm_auto_resolved",
                        error_type=str(draft.get("error_type") or ""),comment=str(draft.get("lessons") or summary),
                        llm_draft=draft,actor="llm_pretriage")
            else:
                result = triage_review_issue(item)
                draft = result.get("draft") or result
                confidence = float(result.get("confidence") or 0)
                can_handle = bool(result.get("can_handle")) and confidence >= 0.75
                summary = str(result.get("summary") or "")
                escalation_reason = str(result.get("escalation_reason") or ("LLM置信度不足" if not can_handle else ""))
                model = str(result.get("model") or "")
                if can_handle:
                    submit_manual_review_action(review_key=key,item_kind=str(item["kind"]),action="llm_auto_resolved",
                        error_type="",comment=summary,llm_draft=draft,actor="llm_pretriage")
            status = "auto_resolved" if can_handle else "escalated"
            save_manual_review_triage(review_key=key,item_kind=str(item["kind"]),status=status,can_handle=can_handle,
                confidence=confidence,summary=summary,escalation_reason=escalation_reason,draft=draft,model=model)
            processed += 1; auto_resolved += int(can_handle); escalated += int(not can_handle)
        except Exception as exc:  # noqa: BLE001
            reason = str(exc)[:300]
            budget_wait = "llm_token_budget_exhausted" in reason
            save_manual_review_triage(review_key=key,item_kind=str(item.get("kind") or "unknown"),status="budget_wait" if budget_wait else "escalated",
                can_handle=False,confidence=0,summary="等待下一个Token预算周期" if budget_wait else "LLM预审失败",
                escalation_reason="今日LLM Token预算已用完" if budget_wait else reason,draft={},model="")
            escalated += int(not budget_wait); processed += 1; errors.append({"review_key": key, "error": reason})
    return {"ok": True, "processed": processed, "auto_resolved": auto_resolved,
            "escalated": escalated, "awaiting_outcome": awaiting_outcome, "errors": errors}


def start_manual_review_triage(limit: int = 100) -> bool:
    global _running
    with _lock:
        if _running:
            return False
        _running = True

    def worker() -> None:
        global _running
        try: run_manual_review_triage_once(limit=limit)
        finally:
            with _lock: _running = False

    threading.Thread(target=worker,daemon=True,name="manual-review-llm-triage").start()
    return True


def triage_status() -> dict[str, Any]:
    rows = list_manual_review_triage().values()
    return {"running": _running, "auto_resolved": sum(row.get("status") == "auto_resolved" for row in rows),
            "escalated": sum(row.get("status") == "escalated" for row in rows),
            "awaiting_outcome": sum(row.get("status") == "awaiting_outcome" for row in rows)}


def start_manual_review_triage_scheduler() -> bool:
    global _scheduler_started
    with _lock:
        if _scheduler_started:
            return False
        _scheduler_started = True
    try: interval = max(60, int(os.getenv("ALIS_MANUAL_REVIEW_TRIAGE_INTERVAL_SECONDS", "300")))
    except ValueError: interval = 300

    def loop() -> None:
        from app.services.runtime_heartbeat import mark_started, beat
        mark_started("manual_review_scheduler", interval_seconds=interval)
        while True:
            try:
                start_manual_review_triage(limit=200)
                beat("manual_review_scheduler")
            except Exception as exc:  # a single triage failure must not kill the scheduler
                beat("manual_review_scheduler", error=str(exc))
            time.sleep(interval)

    threading.Thread(target=loop,daemon=True,name="manual-review-triage-scheduler").start()
    return True
