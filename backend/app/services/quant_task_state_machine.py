from __future__ import annotations

from typing import Final

TASK_STATES: Final[set[str]] = {"pending", "running", "paused", "stopping", "stopped", "failed"}

# 显式事件驱动转移，避免散落 if 写状态。
EVENT_TRANSITIONS: Final[dict[str, dict[str, set[str]]]] = {
    "start_planning": {
        "pending": {"running"},
        "paused": {"running"},
    },
    "manual_resume": {
        "paused": {"pending"},
        "pending": {"pending"},
        "stopped": {"pending"},
    },
    "manual_stop_request": {
        "running": {"stopping"},
        "stopping": {"stopping"},
        "pending": {"stopping"},
        "paused": {"stopping"},
    },
    "manual_stop_finalize": {
        "running": {"stopped"},
        "stopping": {"stopped"},
        "pending": {"stopped"},
        "paused": {"stopped"},
    },
    "step_running": {
        "running": {"running"},
    },
    "checkpoint_saved": {
        "running": {"pending"},
    },
    "schedule_wait": {
        "running": {"pending"},
        "paused": {"pending"},
        "pending": {"pending"},
    },
    "batch_completed": {
        "running": {"paused"},
    },
    "risk_pause": {
        "running": {"paused"},
        "pending": {"paused"},
    },
    "infra_fail": {
        "running": {"failed"},
        "pending": {"failed"},
        "paused": {"failed"},
    },
    "startup_pause": {
        "running": {"paused"},
    },
}


def normalize_status(status: str | None) -> str:
    s = str(status or "").strip().lower()
    if s == "completed":
        return "paused"
    return s


def can_transition(current: str | None, target: str | None, event: str | None = None) -> bool:
    cur = normalize_status(current)
    tgt = normalize_status(target)
    if not cur or not tgt:
        return False
    if cur == tgt:
        return True
    if tgt not in TASK_STATES:
        return False
    if event:
        allowed = EVENT_TRANSITIONS.get(event, {}).get(cur, set())
        return tgt in allowed
    # 默认兜底：只允许在活动态间和终态方向流动
    fallback = {
        "pending": {"running", "paused", "stopped", "failed"},
        "running": {"pending", "paused", "stopping", "stopped", "failed"},
        "paused": {"pending", "running", "stopped", "failed"},
        "stopping": {"stopped", "failed"},
        "stopped": {"pending"},
        "failed": set(),
    }
    return tgt in fallback.get(cur, set())
