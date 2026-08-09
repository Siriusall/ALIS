from __future__ import annotations

from datetime import datetime
from typing import Any


def degraded_read(component: str, exc: Exception, **extra: Any) -> dict[str, Any]:
    """Return a stable, non-sensitive payload for optional read-only evidence."""
    text = str(exc).lower()
    if "timeout" in text or "querycanceled" in text or "canceling statement" in text:
        code = "database_query_timeout"
    elif "connect" in text or "connection" in text:
        code = "database_unavailable"
    else:
        code = "diagnostic_unavailable"
    return {
        "ok": False,
        "status": "degraded",
        "data_available": False,
        "component": component,
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "error": {"code": code, "message": "只读诊断暂时不可用，未使用推测值填充。"},
        **extra,
    }
