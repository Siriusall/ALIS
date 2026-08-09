from __future__ import annotations

import json
import threading
from datetime import datetime
from typing import Any

from app.db.store import _conn
from app.services.decision_store import create_review, list_decision_snapshot_summaries, list_reviews
from app.services.industry_event_engine import list_event_followups
from app.services.market_context import market_state_model_governance
from app.services.operations_overview import build_governance_overview

_ACTION_TABLE_READY = False
_TRIAGE_TABLE_READY = False
_TABLE_INIT_LOCK = threading.Lock()


def _model_release_ready_for_manual_review(latest: dict[str, Any]) -> bool:
    """Only expose a model to operators after strict strategy A/B has passed."""

    validation = latest.get("validation") if isinstance(latest, dict) else None
    if not isinstance(validation, dict):
        return False
    eligibility = validation.get("production_eligibility")
    if not isinstance(eligibility, dict):
        return False
    gates = eligibility.get("gates")
    if not isinstance(gates, dict):
        return False
    # A candidate with task-native OOS but unfinished A/B or future Shadow is
    # still an automated research task, not an operator approval item. The
    # current release contract forbids waiving the 20-session observation.
    return (
        gates.get("controlled_strategy_ab") is True
        and gates.get("live_observation_20_sessions") is True
    )


def _ensure_action_table() -> None:
    global _ACTION_TABLE_READY
    if _ACTION_TABLE_READY:
        return
    with _TABLE_INIT_LOCK:
        if _ACTION_TABLE_READY:
            return
        with _conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS manual_review_actions (
                id BIGSERIAL PRIMARY KEY, review_key VARCHAR(160) NOT NULL UNIQUE,
                item_kind VARCHAR(64) NOT NULL, action VARCHAR(32) NOT NULL,
                error_type VARCHAR(64), comment TEXT NOT NULL DEFAULT '',
                llm_draft_json TEXT NOT NULL DEFAULT '{}', actor VARCHAR(128) NOT NULL DEFAULT '',
                created_at VARCHAR(64) NOT NULL
            )""")
        _ACTION_TABLE_READY = True


def _ensure_triage_table() -> None:
    global _TRIAGE_TABLE_READY
    if _TRIAGE_TABLE_READY:
        return
    with _TABLE_INIT_LOCK:
        if _TRIAGE_TABLE_READY:
            return
        with _conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS manual_review_triage (
                review_key VARCHAR(160) PRIMARY KEY, item_kind VARCHAR(64) NOT NULL,
                status VARCHAR(32) NOT NULL, can_handle INTEGER NOT NULL DEFAULT 0,
                confidence DOUBLE PRECISION NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT '',
                escalation_reason TEXT NOT NULL DEFAULT '', draft_json TEXT NOT NULL DEFAULT '{}',
                model VARCHAR(128) NOT NULL DEFAULT '', updated_at VARCHAR(64) NOT NULL
            )""")
        _TRIAGE_TABLE_READY = True


def list_manual_review_triage() -> dict[str, dict[str, Any]]:
    _ensure_triage_table()
    with _conn() as c:
        rows = c.execute("SELECT * FROM manual_review_triage").fetchall()
    result: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        try: row["draft"] = json.loads(row.pop("draft_json") or "{}")
        except (TypeError, json.JSONDecodeError): row["draft"] = {}
        result[str(row["review_key"])] = row
    return result


def save_manual_review_triage(*, review_key: str, item_kind: str, status: str, can_handle: bool,
                              confidence: float, summary: str, escalation_reason: str = "",
                              draft: dict[str, Any] | None = None, model: str = "") -> None:
    _ensure_triage_table()
    with _conn() as c:
        c.execute("""INSERT INTO manual_review_triage
            (review_key,item_kind,status,can_handle,confidence,summary,escalation_reason,draft_json,model,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(review_key) DO UPDATE SET status=excluded.status,
            can_handle=excluded.can_handle,confidence=excluded.confidence,summary=excluded.summary,
            escalation_reason=excluded.escalation_reason,draft_json=excluded.draft_json,model=excluded.model,updated_at=excluded.updated_at""",
            (review_key,item_kind,status,int(can_handle),float(confidence),summary,escalation_reason,
             json.dumps(draft or {},ensure_ascii=False),model,datetime.now().isoformat(timespec="seconds")))


def _action_keys() -> set[str]:
    _ensure_action_table()
    with _conn() as c:
        return {str(row["review_key"]) for row in c.execute("SELECT review_key FROM manual_review_actions").fetchall()}


def submit_manual_review_action(*, review_key: str, item_kind: str, action: str, error_type: str = "", comment: str = "", llm_draft: dict[str, Any] | None = None, actor: str = "") -> dict[str, Any]:
    if action not in {"approved", "rejected", "modified_approved", "ai_adopted", "llm_auto_resolved"}:
        raise ValueError("invalid_review_action")
    if action == "rejected" and (not error_type or error_type == "其他") and not comment.strip():
        raise ValueError("rejection_reason_required")
    now = datetime.now().isoformat(timespec="seconds")
    _ensure_action_table()
    with _conn() as c:
        row = c.execute("""INSERT INTO manual_review_actions
            (review_key,item_kind,action,error_type,comment,llm_draft_json,actor,created_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(review_key) DO UPDATE SET
            action=excluded.action,error_type=excluded.error_type,comment=excluded.comment,
            llm_draft_json=excluded.llm_draft_json,actor=excluded.actor,created_at=excluded.created_at RETURNING *""",
            (review_key, item_kind, action, error_type or None, comment.strip(), json.dumps(llm_draft or {}, ensure_ascii=False), actor, now)).fetchone()
    if item_kind == "decision_review" and review_key.startswith("decision:"):
        decision_id = int(review_key.split(":", 1)[1])
        draft = llm_draft or {}
        create_review({
            "decision_snapshot_id": decision_id, "review_time": now,
            "outcome": {"human_verdict": action, "llm_summary": draft.get("summary"), "outcome_assessment": draft.get("outcome_assessment")},
            "attribution": {"summary": draft.get("attribution"), "evidence_points": draft.get("evidence_points") or []},
            "error_type": None if action == "approved" else error_type or draft.get("error_type") or "其他",
            "lessons": comment.strip() or str(draft.get("lessons") or "人工确认系统结论"),
        })
        from app.services.review_learning import materialize_training_feedback
        materialize_training_feedback()
    if item_kind == "model_release" and review_key.startswith("model:"):
        try:
            from app.services.plan_model_control import model_family_status, review_plan_model_version

            target_version = review_key.split(":", 1)[1]
            for item in model_family_status().get("items") or []:
                latest = item.get("latest_version") or {}
                if str(latest.get("version") or latest.get("id") or "") == target_version:
                    review_plan_model_version(
                        int(latest["id"]),
                        reviewer=actor or "manual-review",
                        decision="approved" if action in {"approved", "modified_approved", "ai_adopted"} else "rejected",
                        comment=comment,
                        waive_live_observation=False,
                    )
                    break
        except Exception:
            pass
    payload = dict(row)
    payload["llm_draft"] = json.loads(payload.pop("llm_draft_json") or "{}")
    return payload


def build_manual_review_queue(limit: int = 200, *, human_only: bool = True) -> dict[str, Any]:
    """Aggregate every existing human decision gate into one operator queue."""
    items: list[dict[str, Any]] = []
    handled = _action_keys()

    reviews = list_reviews(limit=max(limit, 500))
    reviewed_ids = {int(row["decision_snapshot_id"]) for row in reviews if row.get("decision_snapshot_id")}
    for row in list_decision_snapshot_summaries(limit=limit):
        decision_id = int(row.get("id") or 0)
        if not decision_id or decision_id in reviewed_ids:
            continue
        if f"decision:{decision_id}" in handled:
            continue
        items.append({
            "key": f"decision:{decision_id}", "kind": "decision_review", "priority": "normal",
            "title": f"复盘决策 · {row.get('ts_code') or '组合'}", "reason": "冻结决策尚未完成结果归因与错误分类",
            "status": "pending", "created_at": row.get("decision_time"), "subject": row.get("ts_code") or "",
            "evidence": {"decision_type": row.get("decision_type"), "model_version": row.get("model_version"), "reason": row.get("reason")},
            "target_section": "review",
        })

    followups = list_event_followups(limit=limit).get("items") or []
    for row in followups:
        status = str(row.get("status") or "")
        if status not in {"urgent", "in_review"}:
            continue
        if f"event:{row.get('event_key')}" in handled:
            continue
        items.append({
            "key": f"event:{row.get('event_key')}", "kind": "event_evidence", "priority": "high" if status == "urgent" else "normal",
            "title": f"公告事件复核 · {row.get('ts_code') or '未知标的'}", "reason": row.get("next_action") or "事件证据或投资逻辑需要人工确认",
            "status": status, "created_at": row.get("updated_at"), "subject": row.get("ts_code") or "",
            "evidence": row.get("evidence") or {}, "target_section": "events",
        })

    governance = market_state_model_governance()
    hsmm = governance.get("hsmm") or {}
    if hsmm.get("review_required") and not hsmm.get("review_completed"):
        model_key = f"model:{hsmm.get('latest_shadow_version') or 'hsmm'}"
        if model_key not in handled:
            items.append({
            "key": f"model:{hsmm.get('latest_shadow_version') or 'hsmm'}", "kind": "model_release", "priority": "high",
            "title": "HSMM 上线评审", "reason": hsmm.get("blocking_reason") or "影子观察已达标，等待人工上线评审",
            "status": "pending", "created_at": hsmm.get("live_observation_end"), "subject": hsmm.get("latest_shadow_version") or "HSMM",
            "evidence": {"validation": hsmm.get("validation"), "live_observation_weeks": hsmm.get("live_observation_weeks")},
            "target_section": "governance",
            })

    try:
        from app.services.plan_model_control import model_family_status

        for plan_item in model_family_status().get("items") or []:
            latest = plan_item.get("latest_version") or {}
            if not latest or str(plan_item.get("status") or "") != "candidate":
                continue
            if not _model_release_ready_for_manual_review(latest):
                continue
            review = ((latest.get("validation") or {}).get("review") or {})
            if review.get("status"):
                continue
            model_key = f"model:{latest.get('version') or latest.get('id')}"
            if model_key in handled:
                continue
            items.append({
                "key": model_key,
                "kind": "model_release",
                "priority": "high",
                "title": f"计划书模型上线评审 · {plan_item.get('name')}",
                "reason": "候选模型已完成滚动OOS与费用后回放，等待人工评审；模拟系统可豁免真实20日观察。",
                "status": "pending",
                "created_at": latest.get("updated_at"),
                "subject": latest.get("version") or plan_item.get("model_key"),
                "evidence": {
                    "model_version_id": latest.get("id"),
                    "model_key": plan_item.get("model_key"),
                    "validation": latest.get("validation"),
                    "coverage": plan_item.get("coverage"),
                    "blocked_reasons": plan_item.get("blocked_reasons"),
                },
                "target_section": "governance",
            })
    except Exception:
        pass

    system = build_governance_overview()
    for row in (system.get("tasks") or {}).get("failed_or_paused") or []:
        task_id = row.get("id") or row.get("task_id") or "unknown"
        if f"task:{task_id}" in handled:
            continue
        items.append({
            "key": f"task:{task_id}", "kind": "system_exception", "priority": "high" if str(row.get("status")) == "failed" else "normal",
            "title": f"异常任务复核 · #{task_id}", "reason": row.get("error") or row.get("last_message") or "任务失败或暂停，需要确认恢复条件",
            "status": row.get("status") or "unknown", "created_at": row.get("updated_at"), "subject": str(task_id),
            "evidence": {"phase": row.get("phase"), "task_type": row.get("task_type")}, "target_section": "governance",
        })
    for index, issue in enumerate(((system.get("data") or {}).get("health") or {}).get("issues") or []):
        detail = issue if isinstance(issue, dict) else {"message": str(issue)}
        issue_key = f"data-quality:{detail.get('code') or index}"
        if issue_key in handled:
            continue
        items.append({
            "key": f"data-quality:{detail.get('code') or index}", "kind": "data_quality", "priority": "high",
            "title": "数据质量复核", "reason": detail.get("message") or detail.get("detail") or "数据质量门控未通过",
            "status": detail.get("status") or "blocked", "created_at": detail.get("updated_at"), "subject": str(detail.get("api_name") or detail.get("code") or "数据中台"),
            "evidence": detail, "target_section": "dataCenter",
        })

    triage = list_manual_review_triage()
    pretriage_pending = sum(item["key"] not in triage or triage[item["key"]].get("status") == "budget_wait" for item in items)
    auto_resolved = sum(row.get("status") == "auto_resolved" for row in triage.values())
    awaiting_outcome = sum(row.get("status") == "awaiting_outcome" for row in triage.values())
    if human_only:
        human_items = []
        for item in items:
            state = triage.get(item["key"])
            if not state or state.get("status") != "escalated":
                continue
            item["evidence"] = {**(item.get("evidence") or {}), "llm_triage": state}
            human_items.append(item)
        items = human_items
    rank = {"high": 0, "normal": 1, "low": 2}
    items.sort(key=lambda item: (rank.get(str(item.get("priority")), 9), str(item.get("created_at") or "")))
    counts = {"total": len(items), "high": sum(x["priority"] == "high" for x in items)}
    for kind in ("decision_review", "event_evidence", "model_release", "system_exception", "data_quality"):
        counts[kind] = sum(x["kind"] == kind for x in items)
    return {"ok": True, "counts": counts, "items": items[: max(1, min(int(limit), 500))],
            "ai_triage": {"pending": pretriage_pending, "auto_resolved": auto_resolved,
                          "escalated": sum(row.get("status") == "escalated" for row in triage.values()),
                          "awaiting_outcome": awaiting_outcome}}
