from __future__ import annotations

import json
import time
from typing import Any

from app.data_platform.db import _conn
from app.data_platform.redis_store import (
    STREAM_CONSUMER,
    STREAM_DEAD_LETTER,
    STREAM_GROUP,
    STREAM_KEY,
    ack_task,
    client,
    ensure_stream_group,
)


TERMINAL_BACKFILL_STATUSES = {"completed", "completed_with_gaps", "failed", "interrupted", "cancelled"}
STREAM_KEEP_MESSAGES = 50
DEAD_LETTER_KEEP_MESSAGES = 50


def _decode_fields(fields: dict[str, Any] | None) -> dict[str, Any]:
    raw = {str(k): v for k, v in (fields or {}).items()}
    payload_text = str(raw.get("payload_json") or "{}")
    try:
        payload = json.loads(payload_text)
    except Exception:
        payload = {}
    return {
        **raw,
        "payload": payload,
        "payload_json": payload_text,
    }


def _message(stream: str, message_id: str) -> dict[str, Any] | None:
    value = client()
    if value is None:
        return None
    rows = value.xrange(stream, min=message_id, max=message_id, count=1)
    if not rows:
        return None
    found_id, fields = rows[0]
    return {"id": str(found_id), "fields": _decode_fields(fields)}


def stream_admin_status(*, limit: int = 50, pending_limit: int = 50) -> dict[str, Any]:
    auto = auto_heal_stream(limit=500)
    value = client()
    if value is None:
        return {"ok": False, "available": False, "error": "redis_unavailable", "stream": STREAM_KEY, "group": STREAM_GROUP}
    ensure_stream_group(STREAM_KEY, STREAM_GROUP)
    info = value.xinfo_stream(STREAM_KEY)
    groups = value.xinfo_groups(STREAM_KEY)
    group = next((item for item in groups if item.get("name") == STREAM_GROUP), None) or {}
    consumers = value.xinfo_consumers(STREAM_KEY, STREAM_GROUP)
    pending_summary = value.xpending(STREAM_KEY, STREAM_GROUP)
    pending_rows = value.xpending_range(STREAM_KEY, STREAM_GROUP, "-", "+", max(1, pending_limit))
    pending = []
    for row in pending_rows:
        message_id = str(row.get("message_id") or row.get("message-id") or "")
        item = _message(STREAM_KEY, message_id)
        pending.append({
            "id": message_id,
            "consumer": str(row.get("consumer") or ""),
            "idle_ms": int(row.get("time_since_delivered") or row.get("idle") or 0),
            "deliveries": int(row.get("times_delivered") or row.get("delivery_count") or 0),
            "fields": (item or {}).get("fields") or {},
        })
    recent = [
        {"id": str(message_id), "fields": _decode_fields(fields)}
        for message_id, fields in value.xrevrange(STREAM_KEY, count=max(1, limit))
    ]
    try:
        dead_length = int(value.xlen(STREAM_DEAD_LETTER))
        dead_recent = [
            {"id": str(message_id), "fields": _decode_fields(fields)}
            for message_id, fields in value.xrevrange(STREAM_DEAD_LETTER, count=min(20, max(1, limit)))
        ]
    except Exception:
        dead_length = 0
        dead_recent = []
    return {
        "ok": True,
        "available": True,
        "health": "healthy" if int(pending_summary.get("pending") or 0) <= 0 and dead_length <= 0 else "needs_attention",
        "summary": (
            "任务队列正常，系统会自动处理历史残留"
            if int(pending_summary.get("pending") or 0) <= 0 and dead_length <= 0
            else f"队列有 {int(pending_summary.get('pending') or 0)} 条待确认、{dead_length} 条死信"
        ),
        "stream": STREAM_KEY,
        "group": STREAM_GROUP,
        "consumer": STREAM_CONSUMER,
        "length": int(info.get("length") or 0),
        "pending": int(pending_summary.get("pending") or 0),
        "last_delivered_id": str(group.get("last-delivered-id") or group.get("last_delivered_id") or ""),
        "lag": int(group.get("lag") or 0),
        "consumers": [
            {
                "name": str(item.get("name") or ""),
                "pending": int(item.get("pending") or 0),
                "idle_ms": int(item.get("idle") or 0),
            }
            for item in consumers
        ],
        "pending_messages": pending,
        "recent_messages": recent,
        "dead_letter": {"stream": STREAM_DEAD_LETTER, "length": dead_length, "recent_messages": dead_recent},
        "auto_cleanup": auto,
    }


def auto_heal_stream(*, limit: int = 500) -> dict[str, Any]:
    cleanup = auto_ack_stale_backfill_pending(limit=limit)
    trim = trim_stream_history()
    return {"ok": bool(cleanup.get("ok")) and bool(trim.get("ok")), "cleanup": cleanup, "trim": trim}


def trim_stream_history(*, keep: int = STREAM_KEEP_MESSAGES) -> dict[str, Any]:
    value = client()
    if value is None:
        return {"ok": False, "available": False, "trimmed": 0}
    trimmed = 0
    dead_trimmed = 0
    try:
        # Exact trimming keeps the displayed stream length aligned with the
        # product mental model: it is only recent history, not work remaining.
        before = int(value.xlen(STREAM_KEY))
        value.xtrim(STREAM_KEY, maxlen=max(1, keep), approximate=False)
        after = int(value.xlen(STREAM_KEY))
        trimmed = max(0, before - after)
    except Exception:
        pass
    try:
        before_dead = int(value.xlen(STREAM_DEAD_LETTER))
        value.xtrim(STREAM_DEAD_LETTER, maxlen=max(1, DEAD_LETTER_KEEP_MESSAGES), approximate=False)
        after_dead = int(value.xlen(STREAM_DEAD_LETTER))
        dead_trimmed = max(0, before_dead - after_dead)
    except Exception:
        pass
    return {"ok": True, "available": True, "trimmed": trimmed, "dead_trimmed": dead_trimmed, "keep": keep}


def auto_ack_stale_backfill_pending(*, limit: int = 500) -> dict[str, Any]:
    """Ack stale backfill stream messages whose DB run is no longer running.

    This turns the stream into an implementation detail: users should not need
    to manually clear old replay messages after an interrupted backfill.
    """
    value = client()
    if value is None or not ensure_stream_group(STREAM_KEY, STREAM_GROUP):
        return {"ok": False, "available": False, "acked": 0, "kept": 0}
    try:
        pending_rows = value.xpending_range(STREAM_KEY, STREAM_GROUP, "-", "+", max(1, limit))
    except Exception as exc:
        return {"ok": False, "available": True, "acked": 0, "kept": 0, "error": str(exc)[:300]}
    candidates: list[tuple[str, int]] = []
    kept = 0
    for row in pending_rows:
        message_id = str(row.get("message_id") or row.get("message-id") or "")
        message = _message(STREAM_KEY, message_id)
        fields = (message or {}).get("fields") or {}
        if str(fields.get("task_type") or "") != "data_platform.backfill":
            kept += 1
            continue
        payload = fields.get("payload") or {}
        try:
            run_id = int(payload.get("run_id") or 0)
        except Exception:
            run_id = 0
        if run_id <= 0:
            candidates.append((message_id, run_id))
        else:
            candidates.append((message_id, run_id))
    run_ids = sorted({run_id for _, run_id in candidates if run_id > 0})
    statuses: dict[int, str] = {}
    if run_ids:
        placeholders = ",".join("?" for _ in run_ids)
        with _conn() as c:
            rows = c.execute(
                f"SELECT id,status FROM data_ingestion_runs WHERE id IN ({placeholders})",
                tuple(run_ids),
            ).fetchall()
        statuses = {int(dict(row).get("id") or 0): str(dict(row).get("status") or "") for row in rows}
    acked = 0
    acked_ids: list[str] = []
    for message_id, run_id in candidates:
        status = statuses.get(run_id, "missing" if run_id > 0 else "unknown")
        if status == "running":
            kept += 1
            continue
        if status in TERMINAL_BACKFILL_STATUSES or status in {"missing", "unknown"}:
            if ack_task(message_id):
                acked += 1
                acked_ids.append(message_id)
            else:
                kept += 1
        else:
            kept += 1
    return {"ok": True, "available": True, "acked": acked, "kept": kept, "sample": acked_ids[:10]}


def handle_stream_message(message_id: str, action: str) -> dict[str, Any]:
    value = client()
    if value is None:
        return {"ok": False, "error": "redis_unavailable"}
    ensure_stream_group(STREAM_KEY, STREAM_GROUP)
    message = _message(STREAM_KEY, message_id)
    if not message:
        return {"ok": False, "error": "message_not_found", "message_id": message_id}
    fields = message["fields"]
    if action == "ack":
        return {"ok": bool(ack_task(message_id)), "action": action, "message_id": message_id}
    if action == "delete":
        acked = ack_task(message_id)
        deleted = int(value.xdel(STREAM_KEY, message_id) or 0)
        return {"ok": bool(acked or deleted), "action": action, "message_id": message_id, "acked": acked, "deleted": deleted}
    if action == "dead_letter":
        dead = {k: str(v) for k, v in fields.items() if k != "payload"}
        dead["failed_at"] = str(time.time())
        dead["source_message_id"] = message_id
        dead_id = value.xadd(STREAM_DEAD_LETTER, dead, maxlen=100000, approximate=True)
        acked = ack_task(message_id)
        deleted = int(value.xdel(STREAM_KEY, message_id) or 0)
        return {
            "ok": True,
            "action": action,
            "message_id": message_id,
            "dead_letter_id": str(dead_id),
            "acked": acked,
            "deleted": deleted,
        }
    if action == "retry":
        from app.data_platform.services.backfill import process_stream_task

        value.xclaim(STREAM_KEY, STREAM_GROUP, STREAM_CONSUMER, min_idle_time=0, message_ids=[message_id])
        result = process_stream_task({k: str(v) for k, v in fields.items() if k != "payload"})
        if result.get("ok"):
            ack_task(message_id)
        return {"ok": bool(result.get("ok")), "action": action, "message_id": message_id, "result": result}
    return {"ok": False, "error": "unsupported_action", "action": action}


def bulk_handle_stream_messages(action: str, *, task_type: str = "data_platform.backfill", limit: int = 200) -> dict[str, Any]:
    status = stream_admin_status(limit=1, pending_limit=limit)
    handled = []
    for item in status.get("pending_messages") or []:
        fields = item.get("fields") or {}
        if task_type and str(fields.get("task_type") or "") != task_type:
            continue
        handled.append(handle_stream_message(str(item.get("id") or ""), action))
    return {"ok": all(bool(item.get("ok")) for item in handled), "action": action, "handled": handled, "count": len(handled)}
