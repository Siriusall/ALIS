"""Stable, serializable exception evidence for critical ALIS workflows."""

from __future__ import annotations

from datetime import datetime
from typing import Any

ERROR_EVIDENCE_CONTRACT = "alis_error_evidence_v1"
ERROR_CATEGORIES = {
    "valid_empty",
    "data_unavailable",
    "query_failed",
    "degraded_data",
    "contract_not_met",
    "internal_fault",
}

_CATEGORY_ALIASES = {
    "empty_pending": "data_unavailable",
    "upstream_not_ready": "data_unavailable",
    "transient": "data_unavailable",
    "rate_limit": "data_unavailable",
    "permission": "data_unavailable",
    "permission_denied": "data_unavailable",
    "incomplete_slice": "contract_not_met",
    "truncated": "contract_not_met",
    "pagination_truncated": "contract_not_met",
}


def classify_exception(exc: BaseException) -> str:
    declared = str(getattr(exc, "category", "") or "").strip().lower()
    if declared in ERROR_CATEGORIES:
        return declared
    if declared:
        return _CATEGORY_ALIASES.get(declared, "internal_fault")
    module = type(exc).__module__.lower()
    if "psycopg" in module or "sqlite" in module:
        return "query_failed"
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "data_unavailable"
    if isinstance(exc, (KeyError, TypeError, ValueError, AssertionError)):
        return "contract_not_met"
    return "internal_fault"


def error_evidence(
    exc: BaseException,
    *,
    operation: str,
    source: str,
    deferred: bool = False,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    category = classify_exception(exc)
    return {
        "contract": ERROR_EVIDENCE_CONTRACT,
        "ok": False,
        "category": category,
        "operation": str(operation),
        "source": str(source),
        "error_type": type(exc).__name__,
        "message": str(exc)[:500],
        "deferred": bool(deferred),
        "retryable": category in {"data_unavailable", "query_failed"},
        "occurred_at": datetime.now().isoformat(timespec="seconds"),
        "context": context or {},
    }
