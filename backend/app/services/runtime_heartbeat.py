"""Process-local health state for long-lived background components."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any

_lock = Lock()
_states: dict[str, dict[str, Any]] = {}


def beat(name: str, *, error: str = "") -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        state = _states.setdefault(name, {"started": now, "errors": 0})
        state.update({"heartbeat_at": now, "last_error": str(error or "")[:300], "alive": not bool(error)})
        if error:
            state["errors"] = int(state.get("errors") or 0) + 1


def mark_started(name: str, *, interval_seconds: int = 300) -> None:
    beat(name)
    with _lock:
        _states[name]["interval_seconds"] = max(30, int(interval_seconds))


def status() -> dict[str, dict[str, Any]]:
    with _lock:
        result = {key: dict(value) for key, value in _states.items()}
    now = datetime.now(timezone.utc)
    for state in result.values():
        try:
            heartbeat = datetime.fromisoformat(str(state.get("heartbeat_at")).replace("Z", "+00:00"))
            age = max(0.0, (now - heartbeat).total_seconds())
        except (TypeError, ValueError):
            age = None
        state["heartbeat_age_seconds"] = round(age, 3) if age is not None else None
        timeout = int(state.get("interval_seconds") or 300) * 3
        state["healthy"] = bool(state.get("alive")) and not state.get("last_error") and (age is None or age <= timeout)
        if age is not None and age > timeout:
            state["health_reason"] = "heartbeat_timeout"
    return result
