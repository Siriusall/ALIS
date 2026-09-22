"""Durable, page-scoped read snapshots for the shared ALIS console.

PostgreSQL is the authority: every successful build is committed there first.
Redis only mirrors the latest document briefly so repeated browser opens avoid
both PostgreSQL and the expensive aggregation work.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from typing import Any, Callable

from fastapi.encoders import jsonable_encoder
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from app.data_platform.redis_store import delete as redis_delete
from app.data_platform.redis_store import get_json, set_json
from app.db.store import _conn, _read_conn
from app.services.network_budget import optional_network_work_allowed


PAGE_KEYS = (
    "overview", "decision", "portfolio", "operations", "runtime", "versions", "data",
    "models", "research", "factors", "waves", "evidence",
)
_DEFAULT_TTLS = {
    # Event invalidation is the freshness authority.  Periodic rebuilds are a
    # safety net, not a second polling system: sub-minute TTLs kept the single
    # control worker permanently rebuilding pages and repeatedly ran formal
    # historical audits beside the close update and execution probes.
    "overview": 600,
    "decision": 600,
    "portfolio": 600,
    "operations": 600,
    "runtime": 30,
    "versions": 600,
    "data": 600,
    "models": 600,
    "research": 600,
    "factors": 600,
    "waves": 600,
    "evidence": 600,
}
_FAILURE_RETRY_SECONDS = 15
_APP_CACHE_TTL_SECONDS = 86400
_DIAGNOSTIC_CACHE_SECONDS = 300
_SNAPSHOT_FORCE_REFRESH_SECONDS = max(
    30 * 60,
    min(int(os.getenv("ALIS_CONSOLE_SNAPSHOT_FORCE_REFRESH_SECONDS", str(60 * 60))), 24 * 60 * 60),
)
_DIAGNOSTIC_PAGE_KEYS = frozenset({"overview", "operations", "data"})
_DB_HEAVY_TASK_TYPES = frozenset({
    "factor_daily_cycle",
    "factor_research_cycle",
    "model_candidate_research",
    "model_strategy_impact_validation",
    "plan_model_autonomy",
    "research_maintenance",
    "strategy_controller_oos",
    "strategy_environment_backtest",
    "strategy_evolution_cycle",
})
_schema_ready = False
_schema_lock = Lock()
_diagnostic_cache: dict[str, tuple[float, Callable[..., Any], Any]] = {}
_diagnostic_cache_lock = Lock()
_diagnostic_cache_generation = 0
logger = logging.getLogger(__name__)

# The database-local worker is deliberately observable from the application
# node.  A process-local flag or systemd's ``active`` state cannot tell us
# whether the worker is making progress through page builds.
_SNAPSHOT_WORKER_KEY = "console_snapshot_worker"
_SNAPSHOT_WORKER_HEARTBEAT_INTERVAL_SECONDS = 30
_SNAPSHOT_WORKER_STALE_SECONDS = 180
_SNAPSHOT_WORKER_RUNTIME_SCHEMA_READY = False
_snapshot_worker_runtime_lock = Lock()
_snapshot_worker_runtime: dict[str, Any] = {
    "worker_id": "",
    "node_name": "",
    "status": "starting",
    "started_at": "",
    "heartbeat_at": "",
    "cycle_started_at": "",
    "cycle_completed_at": "",
    "current_page": "",
    "last_success_page": "",
    "last_success_at": "",
    "last_error_code": "",
    "last_error_at": "",
    "pages_completed": 0,
}
_snapshot_worker_runtime_last_persisted = 0.0
_snapshot_worker_status_cache: tuple[float, dict[str, Any]] | None = None
_snapshot_worker_status_cache_lock = Lock()
_SNAPSHOT_WORKER_STATUS_CACHE_SECONDS = 5.0
_BUSINESS_DATA_BLOCK_CODES = frozenset({
    "position_market_data_not_ready",
    "trade_calendar_not_ready",
    "decision_data_not_ready",
    "overview_data_not_ready",
    "equity_curve_data_not_ready",
    "strict_snapshot_trade_date_missing",
    "latest_market_day_not_materialized",
    "market_data_not_ready",
    "snapshot_data_not_ready",
})

_RUNTIME_OVERLAY_KEYS = frozenset({
    "health",
    "tradingReady",
    "adminTasksLive",
    "tasks",
    "evolutionTasks",
    "evolutionSchedule",
    "quantTasks",
    "quantStatus",
    "quantRecent",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redis_key(page_key: str) -> str:
    return f"alis:console-snapshot:{page_key}:v1"


def _ttl(page_key: str) -> int:
    raw = os.getenv(f"ALIS_CONSOLE_SNAPSHOT_{page_key.upper()}_TTL_SECONDS", "")
    try:
        return max(5, int(raw)) if raw else _DEFAULT_TTLS[page_key]
    except ValueError:
        return _DEFAULT_TTLS[page_key]


def _failure_retry_seconds() -> int:
    raw = os.getenv("ALIS_CONSOLE_SNAPSHOT_FAILURE_RETRY_SECONDS", "")
    try:
        return max(5, min(120, int(raw))) if raw else _FAILURE_RETRY_SECONDS
    except ValueError:
        return _FAILURE_RETRY_SECONDS


def _worker_stale_seconds() -> int:
    raw = os.getenv("ALIS_CONSOLE_SNAPSHOT_WORKER_STALE_SECONDS", "")
    try:
        return max(60, min(3600, int(raw))) if raw else _SNAPSHOT_WORKER_STALE_SECONDS
    except ValueError:
        return _SNAPSHOT_WORKER_STALE_SECONDS


def _worker_page_stale_seconds(page_key: str) -> int:
    raw = os.getenv(
        f"ALIS_CONSOLE_SNAPSHOT_{page_key.upper()}_MAX_STALE_SECONDS", "",
    )
    try:
        if raw:
            return max(_ttl(page_key), min(86400, int(raw)))
    except ValueError:
        pass
    # A short runtime TTL is useful for the browser but should not make the
    # operational health signal flap while a single expensive page is being
    # assembled.  The worker heartbeat catches a stopped process within three
    # minutes; this separate page limit detects an alive worker stuck on one
    # page or perpetually yielding its queue.
    return max(_ttl(page_key) * 3, _worker_stale_seconds() * 2, 300)


def _worker_identity() -> tuple[str, str]:
    node = str(os.getenv("ALIS_NODE_NAME") or socket.gethostname())[:160]
    return f"{node}:{os.getpid()}", node


def _ensure_runtime_table() -> None:
    """Verify/create the worker runtime table without doing production DDL.

    The explicit sim migration owns PostgreSQL schema changes.  The SQLite
    branch exists only for isolated test fixtures, matching the page snapshot
    compatibility path above.
    """

    global _SNAPSHOT_WORKER_RUNTIME_SCHEMA_READY
    if _SNAPSHOT_WORKER_RUNTIME_SCHEMA_READY:
        return
    with _schema_lock:
        if _SNAPSHOT_WORKER_RUNTIME_SCHEMA_READY:
            return
        with _conn() as conn:
            if bool(getattr(conn, "_is_postgres", False)):
                row = conn.execute(
                    "SELECT to_regclass(?) AS table_name",
                    ("public.console_snapshot_worker_runtime",),
                ).fetchone()
                table_name = str((dict(row) if row else {}).get("table_name") or "")
                if not table_name:
                    raise RuntimeError(
                        "console_snapshot_worker_runtime schema is missing; run sim migrations before starting the worker"
                    )
            else:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS console_snapshot_worker_runtime (
                        worker_key VARCHAR(64) PRIMARY KEY,
                        worker_id VARCHAR(160) NOT NULL,
                        node_name VARCHAR(160) NOT NULL,
                        status VARCHAR(32) NOT NULL,
                        started_at VARCHAR(64) NOT NULL,
                        heartbeat_at VARCHAR(64) NOT NULL,
                        cycle_started_at VARCHAR(64),
                        cycle_completed_at VARCHAR(64),
                        current_page VARCHAR(32) NOT NULL DEFAULT '',
                        last_success_page VARCHAR(32) NOT NULL DEFAULT '',
                        last_success_at VARCHAR(64),
                        last_error_code VARCHAR(128) NOT NULL DEFAULT '',
                        last_error_at VARCHAR(64),
                        pages_completed BIGINT NOT NULL DEFAULT 0,
                        details_json TEXT NOT NULL DEFAULT '{}',
                        updated_at VARCHAR(64) NOT NULL
                    )
                    """
                )
        _SNAPSHOT_WORKER_RUNTIME_SCHEMA_READY = True


def _runtime_row_payload(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    value = dict(row)
    try:
        details = json.loads(value.get("details_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        details = {}
    value["details"] = details if isinstance(details, dict) else {}
    value.pop("details_json", None)
    return value


def _persist_worker_runtime(*, force: bool = False) -> None:
    """Persist the in-memory worker state at most once per 30 seconds."""

    global _snapshot_worker_runtime_last_persisted
    now_monotonic = time.monotonic()
    with _snapshot_worker_runtime_lock:
        if not force and now_monotonic - _snapshot_worker_runtime_last_persisted < _SNAPSHOT_WORKER_HEARTBEAT_INTERVAL_SECONDS:
            return
        state = dict(_snapshot_worker_runtime)
        _snapshot_worker_runtime_last_persisted = now_monotonic
    try:
        _ensure_runtime_table()
        with _conn() as conn:
            conn.execute(
                """
                INSERT INTO console_snapshot_worker_runtime
                    (worker_key,worker_id,node_name,status,started_at,heartbeat_at,
                     cycle_started_at,cycle_completed_at,current_page,last_success_page,
                     last_success_at,last_error_code,last_error_at,pages_completed,
                     details_json,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (worker_key) DO UPDATE SET
                    worker_id=EXCLUDED.worker_id,
                    node_name=EXCLUDED.node_name,
                    status=EXCLUDED.status,
                    started_at=EXCLUDED.started_at,
                    heartbeat_at=EXCLUDED.heartbeat_at,
                    cycle_started_at=EXCLUDED.cycle_started_at,
                    cycle_completed_at=EXCLUDED.cycle_completed_at,
                    current_page=EXCLUDED.current_page,
                    last_success_page=EXCLUDED.last_success_page,
                    last_success_at=EXCLUDED.last_success_at,
                    last_error_code=EXCLUDED.last_error_code,
                    last_error_at=EXCLUDED.last_error_at,
                    pages_completed=EXCLUDED.pages_completed,
                    details_json=EXCLUDED.details_json,
                    updated_at=EXCLUDED.updated_at
                """,
                (
                    _SNAPSHOT_WORKER_KEY,
                    state["worker_id"],
                    state["node_name"],
                    state["status"],
                    state["started_at"],
                    state["heartbeat_at"],
                    state["cycle_started_at"] or None,
                    state["cycle_completed_at"] or None,
                    state["current_page"],
                    state["last_success_page"],
                    state["last_success_at"] or None,
                    state["last_error_code"],
                    state["last_error_at"] or None,
                    int(state.get("pages_completed") or 0),
                    json.dumps(state.get("details") or {}, ensure_ascii=False, separators=(",", ":")),
                    state["heartbeat_at"],
                ),
            )
    except Exception:
        # A heartbeat must never terminate the snapshot worker.  The health
        # reader will report ``worker_unavailable`` until the schema/DB recovers.
        logger.warning("Console snapshot worker heartbeat persistence failed", exc_info=True)


def record_worker_heartbeat(
    *,
    status: str = "running",
    current_page: str | None = None,
    last_success_page: str | None = None,
    last_error_code: str | None = None,
    details: dict[str, Any] | None = None,
    force: bool = False,
) -> None:
    """Update and durably persist the snapshot worker runtime projection."""

    now = _now()
    with _snapshot_worker_runtime_lock:
        if not _snapshot_worker_runtime["worker_id"]:
            worker_id, node_name = _worker_identity()
            _snapshot_worker_runtime.update({
                "worker_id": worker_id,
                "node_name": node_name,
                "started_at": now,
            })
        _snapshot_worker_runtime["status"] = str(status or "running")[:32]
        _snapshot_worker_runtime["heartbeat_at"] = now
        if current_page is not None:
            _snapshot_worker_runtime["current_page"] = str(current_page or "")[:32]
        if last_success_page is not None:
            _snapshot_worker_runtime["last_success_page"] = str(last_success_page or "")[:32]
            if last_success_page:
                _snapshot_worker_runtime["last_success_at"] = now
                _snapshot_worker_runtime["pages_completed"] = int(
                    _snapshot_worker_runtime.get("pages_completed") or 0
                ) + 1
        if last_error_code is not None:
            _snapshot_worker_runtime["last_error_code"] = str(last_error_code or "")[:128]
            _snapshot_worker_runtime["last_error_at"] = now if last_error_code else ""
        if details is not None:
            _snapshot_worker_runtime["details"] = dict(details)
            event = str(details.get("event") or "")
            if event == "cycle_started":
                _snapshot_worker_runtime["cycle_started_at"] = now
            elif event == "cycle_completed":
                _snapshot_worker_runtime["cycle_completed_at"] = now
    _persist_worker_runtime(force=force or status in {"stopped", "failed"} or bool(last_error_code))


def _read_worker_runtime() -> dict[str, Any] | None:
    try:
        _ensure_runtime_table()
        with _read_conn() as conn:
            row = conn.execute(
                """SELECT worker_key,worker_id,node_name,status,started_at,heartbeat_at,
                          cycle_started_at,cycle_completed_at,current_page,last_success_page,
                          last_success_at,last_error_code,last_error_at,pages_completed,
                          details_json,updated_at
                     FROM console_snapshot_worker_runtime WHERE worker_key=?""",
                (_SNAPSHOT_WORKER_KEY,),
            ).fetchone()
        return _runtime_row_payload(row)
    except Exception as exc:
        logger.warning("Console snapshot worker runtime unavailable error_type=%s", type(exc).__name__)
        return None


def worker_runtime_status(*, now: datetime | None = None) -> dict[str, Any]:
    """Return separate liveness and operational views of the snapshot worker.

    A fresh heartbeat only proves that the process is alive.  It does not prove
    that page builds are succeeding: the worker deliberately remains alive
    while retrying transient data gates and build failures.  Keep that
    distinction explicit so callers do not turn a continuously failing worker
    into a false ``healthy`` signal (or, conversely, stop refresh retries just
    because one build is degraded).
    """

    record = _read_worker_runtime()
    current = now or datetime.now(timezone.utc)
    if not record:
        return {
            "status": "worker_unavailable",
            "operational_status": "unavailable",
            "healthy": False,
            "heartbeat_healthy": False,
            "live": False,
            "degraded": False,
            "reason": "snapshot_worker_heartbeat_missing",
            "heartbeat_at": None,
            "heartbeat_age_seconds": None,
            "worker": None,
        }
    heartbeat = _parse_timestamp(record.get("heartbeat_at"))
    age = None if heartbeat is None else max(0.0, (current - heartbeat).total_seconds())
    raw_status = str(record.get("status") or "").strip().lower()
    active = raw_status in {"starting", "running", "degraded"}
    heartbeat_healthy = bool(active and age is not None and age <= _worker_stale_seconds())
    operational_degraded = bool(heartbeat_healthy and raw_status == "degraded")
    healthy = bool(heartbeat_healthy and not operational_degraded)
    status = "degraded" if operational_degraded else "ready" if heartbeat_healthy else "worker_unavailable"
    reason = (str(record.get("last_error_code") or "snapshot_worker_degraded")
              if operational_degraded else "" if heartbeat_healthy else (
        "snapshot_worker_heartbeat_invalid" if heartbeat is None
        else "snapshot_worker_heartbeat_expired" if age > _worker_stale_seconds()
        else "snapshot_worker_not_running"
    ))
    return {
        "status": status,
        "operational_status": "degraded" if operational_degraded else "ready" if heartbeat_healthy else "unavailable",
        "healthy": healthy,
        "heartbeat_healthy": heartbeat_healthy,
        "live": heartbeat_healthy,
        "degraded": operational_degraded,
        "reason": reason,
        "heartbeat_at": record.get("heartbeat_at"),
        "heartbeat_age_seconds": round(age, 3) if age is not None else None,
        "stale_after_seconds": _worker_stale_seconds(),
        "worker": record,
    }


def _cached_worker_runtime_status() -> dict[str, Any]:
    """Avoid a runtime-table query for every browser page read."""

    global _snapshot_worker_status_cache
    observed = time.monotonic()
    with _snapshot_worker_status_cache_lock:
        cached = _snapshot_worker_status_cache
        if cached and observed - cached[0] < _SNAPSHOT_WORKER_STATUS_CACHE_SECONDS:
            return copy.deepcopy(cached[1])
    value = worker_runtime_status()
    with _snapshot_worker_status_cache_lock:
        _snapshot_worker_status_cache = (observed, copy.deepcopy(value))
    return value


def _with_refresh_runtime(
    response: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach bounded refresh availability without changing page data shape."""

    worker = _cached_worker_runtime_status()
    worker_record = worker.get("worker") if isinstance(worker.get("worker"), dict) else {}
    # Refresh remains possible while a live worker is retrying a degraded
    # build.  Use liveness for this gate; operational health is reported
    # separately to the API/monitoring callers.
    available = bool(worker.get("heartbeat_healthy", worker.get("live", worker.get("healthy"))))
    refresh_error = ""
    if not available:
        refresh_error = str(worker.get("reason") or "snapshot_worker_unavailable")
    elif bool(worker.get("degraded")):
        refresh_error = str(worker.get("reason") or "snapshot_worker_degraded")
    elif bool(response.get("refreshing")):
        refresh_error = str((metadata or {}).get("last_error_code") or "")
    response["refresh_available"] = available
    response["snapshot_worker_status"] = str(worker.get("status") or "worker_unavailable")
    response["worker_status"] = str(worker_record.get("status") or worker.get("status") or "worker_unavailable")
    response["refresh_error_code"] = refresh_error
    response["refresh_retry_after_seconds"] = _failure_retry_seconds()
    return response


def _cached_diagnostic(
    key: str,
    reader: Callable[..., Any],
    /,
    **kwargs: Any,
) -> Any:
    """Reuse immutable historical audit summaries across page documents.

    A control-worker cycle builds several pages that expose the same OOS and
    sealed summaries.  Re-running each formal audit per page scans the full
    immutable history several times.  The callable identity is part of the
    cache contract so tests, hot reloads and monkey-patched readers cannot
    inherit another implementation's value.
    """

    now = time.monotonic()
    with _diagnostic_cache_lock:
        generation = _diagnostic_cache_generation
        cached = _diagnostic_cache.get(key)
        if cached and cached[1] is reader and now - cached[0] < _DIAGNOSTIC_CACHE_SECONDS:
            return copy.deepcopy(cached[2])
    value = reader(**kwargs)
    with _diagnostic_cache_lock:
        # An invalidation may run while the formal audit is outside the lock.
        # Never let that old result repopulate the cache for a newer snapshot
        # generation; the in-flight page build itself is rejected by its
        # durable refresh-generation compare-and-swap.
        if generation == _diagnostic_cache_generation:
            _diagnostic_cache[key] = (time.monotonic(), reader, copy.deepcopy(value))
    return value


def _persisted_diagnostic(name: str) -> dict[str, Any]:
    """Reuse a compact durable audit instead of transferring raw PIT history."""

    section_name = {
        "progressive_oos": "oos",
        "sealed_holdout": "sealed",
    }.get(name, name)
    for page_key in ("data", "operations", "overview"):
        payload = _load_durable(page_key)
        data = payload.get("data") if isinstance(payload, dict) else None
        value = data.get(section_name) if isinstance(data, dict) else None
        if isinstance(value, dict) and str(value.get("status") or "") not in {
            "unavailable", "deferred",
        }:
            return copy.deepcopy(value)
    return {
        "ok": True,
        "status": "deferred",
        "deferred": True,
        "reason": "traffic_budget_protection",
        "summary": "历史审计沿用上一份持久化摘要，当前未跨网络重新扫描。",
    }


def _historical_diagnostic(
    name: str,
    reader: Callable[..., Any],
    /,
    **kwargs: Any,
) -> Any:
    mode = str(
        os.getenv("ALIS_CONSOLE_SNAPSHOT_HISTORICAL_AUDIT_MODE", "refresh")
    ).strip().lower()
    if mode == "persisted":
        return _persisted_diagnostic(name)
    return _cached_diagnostic(name, reader, **kwargs)


def _clear_diagnostic_cache() -> None:
    """Synchronously retire cached and in-flight diagnostic generations."""

    global _diagnostic_cache_generation
    with _diagnostic_cache_lock:
        _diagnostic_cache.clear()
        _diagnostic_cache_generation += 1


def _ensure_table() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with _conn() as conn:
            if bool(getattr(conn, "_is_postgres", False)):
                # Production schema changes belong to the explicit Alembic
                # release migration.  The API and control worker run with
                # the restricted application role and must never attempt DDL
                # (which previously caused an endless InsufficientPrivilege
                # retry loop after the snapshot worker was enabled).
                row = conn.execute(
                    "SELECT to_regclass(?) AS table_name",
                    ("public.console_page_snapshots",),
                ).fetchone()
                table_name = str((dict(row) if row else {}).get("table_name") or "")
                if not table_name:
                    raise RuntimeError(
                        "console_page_snapshots schema is missing; run sim migrations before starting the worker"
                    )
            else:
                # Keep the explicit SQLite compatibility path for isolated
                # unit fixtures.  Production never reaches this branch.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS console_page_snapshots (
                        page_key VARCHAR(32) PRIMARY KEY,
                        version VARCHAR(64) NOT NULL,
                        generated_at VARCHAR(64) NOT NULL,
                        as_of VARCHAR(64) NOT NULL,
                        status VARCHAR(32) NOT NULL,
                        invalidated_at VARCHAR(64),
                        payload_json TEXT NOT NULL,
                        payload_gzip BLOB,
                        content_hash VARCHAR(64) NOT NULL DEFAULT '',
                        payload_size_bytes BIGINT NOT NULL DEFAULT 0,
                        compressed_size_bytes BIGINT NOT NULL DEFAULT 0,
                        updated_at VARCHAR(64) NOT NULL,
                        refresh_generation BIGINT NOT NULL DEFAULT 0,
                        last_attempt_generation BIGINT NOT NULL DEFAULT -1,
                        last_attempt_at VARCHAR(64),
                        last_error_code VARCHAR(64) NOT NULL DEFAULT ''
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_console_page_snapshots_updated "
                    "ON console_page_snapshots(updated_at DESC)"
                )
        _schema_ready = True


def _as_json(value: Any) -> Any:
    if isinstance(value, JSONResponse):
        value = json.loads(value.body.decode("utf-8"))
    return jsonable_encoder(value)


def _http_exception_detail(exc: HTTPException) -> dict[str, Any]:
    detail = exc.detail
    if isinstance(detail, dict):
        # Route details are already structured.  Keep only JSON-compatible
        # fields so exception objects/tracebacks never enter a durable page.
        encoded = _as_json(detail)
        return encoded if isinstance(encoded, dict) else {"message": str(encoded)}
    return {"message": str(detail or "")}


def _is_business_data_block(exc: HTTPException) -> bool:
    if int(getattr(exc, "status_code", 0) or 0) != 503:
        return False
    detail = _http_exception_detail(exc)
    code = str(detail.get("code") or "").strip().lower()
    if code in _BUSINESS_DATA_BLOCK_CODES:
        return True
    # A few legacy readers expose a more specific readiness code.  Restrict
    # this fallback to explicit data/readiness wording; a generic 503 remains
    # a hard section error and therefore protects the last-known-good page.
    return any(token in code for token in ("data_not_ready", "market_data", "snapshot_trade_date"))


def _degraded_business_section(
    page_key: str,
    name: str,
    exc: HTTPException,
) -> tuple[dict[str, Any], dict[str, Any]]:
    detail = _http_exception_detail(exc)
    code = str(detail.get("code") or "snapshot_business_data_not_ready")[:128]
    message = str(detail.get("message") or "目标交易日数据尚未就绪，页面其余分区仍可用。")[:500]
    error = {
        **detail,
        "code": code,
        "message": message,
        "status_code": int(getattr(exc, "status_code", 503) or 503),
        "retryable": True,
        "business_blocked": True,
    }
    value = {
        "ok": False,
        "status": "degraded",
        "data_available": False,
        "component": name,
        "retry_after_seconds": _failure_retry_seconds(),
        "error": error,
    }
    logger.info(
        "Console snapshot section blocked by expected business data readiness page=%s section=%s code=%s",
        page_key,
        name,
        code,
    )
    return value, {"status": "degraded", "error": error, "kind": "business_data_block"}


def _section(page_key: str, name: str, reader: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
    try:
        value = _as_json(reader())
        # Route payloads can correctly describe a blocked model, historical
        # failure, or partial research evidence while still carrying a complete
        # readable document.  A page snapshot is degraded only when the read
        # itself failed or the endpoint explicitly says no data is available.
        unavailable = isinstance(value, dict) and value.get("data_available") is False and not _has_readable_evidence(value)
        return value, {"status": "degraded" if unavailable else "ready", "error": ""}
    except HTTPException as exc:
        if _is_business_data_block(exc):
            return _degraded_business_section(page_key, name, exc)
        logger.warning(
            "Console snapshot section HTTP failure page=%s section=%s status=%s",
            page_key,
            name,
            getattr(exc, "status_code", ""),
        )
        error = {
            "code": "snapshot_section_http_error",
            "message": "该页面分区暂时不可用，系统将自动重试。",
            "status_code": int(getattr(exc, "status_code", 500) or 500),
        }
        return {"ok": False, "status": "unavailable", "error": error}, {
            "status": "error", "error": error,
        }
    except Exception as exc:  # a page can surface partial evidence safely
        logger.warning(
            "Console snapshot section unavailable page=%s section=%s error_type=%s",
            page_key,
            name,
            type(exc).__name__,
        )
        error = {
            "code": "snapshot_section_unavailable",
            "message": "该页面分区暂时不可用，系统将自动重试。",
        }
        return {"ok": False, "status": "unavailable", "error": error}, {
            "status": "error", "error": error,
        }


def _has_readable_evidence(value: dict[str, Any]) -> bool:
    """Keep partial business documents visible when they contain evidence."""
    if any(key in value and value.get(key) not in (None, "", [], {}) for key in (
        "items", "gates", "capabilities", "market_state", "observations",
        "decisions", "reviews", "quality", "counts", "results",
    )):
        return True
    # A degraded-read envelope still contains bookkeeping such as component,
    # checked_at and count=0.  Those fields prove that the fallback ran, not
    # that business evidence was returned.  Treating them as evidence made a
    # failed audit section look ready while the UI rendered empty fields.
    envelope_keys = {
        "ok", "status", "data_available", "available", "error", "errors",
        "component", "checked_at", "count", "id", "reason",
    }
    return any(
        key not in envelope_keys and item not in (None, "", [], {})
        for key, item in value.items()
    )


def _readers(page_key: str) -> dict[str, Callable[[], Any]]:
    # Imports are intentionally lazy: this service is used from startup paths
    # and must not create a main.py import cycle.
    from app.api.routes import health, model_strategy, quant_runs, research, system_versions
    from app.api.routes.decision import decision_latest_plan, decision_summary
    from app.api.routes.positions import get_positions
    from app.data_platform.routes import data_center
    from app.services.application_runtime_projection import read_health

    def control_trading_ready() -> dict[str, Any]:
        # Full trading readiness performs strict immutable raw evidence scans
        # and is owned by the API's live gate. The control worker only builds
        # durable read snapshots; repeating that gate here can hold a data DB
        # transaction for minutes and starve normal requests.
        return {
            "schema_version": "alis_trading_readiness_projection_v1",
            "status": "waiting",
            "trading_ready": False,
            "summary": "控制节点快照不执行实时交易就绪检查；请以应用节点实时门为准。",
            "hard_blockers": [],
            "gates": [],
            "policy": {"read_only": True, "source": "control_snapshot_projection"},
        }

    def application_data_streams() -> dict[str, Any]:
        health = read_health()
        redis = (health.get("dependencies") or {}).get("redis") or {}
        current = health.get("projection_status") == "current"
        available = current and bool(redis.get("available"))
        return {
            "ok": available,
            "available": available,
            "status": "ready" if available else "unknown" if not current else "unavailable",
            "source_role": "application",
            "observed_at": health.get("observed_at"),
            "expires_at": health.get("expires_at"),
            "reason": "" if available else (health.get("issues") or ["application_redis_unavailable"])[0],
        }

    latest_decision_bundle: dict[str, Any] = {}
    research_inputs: dict[str, tuple[bool, Any]] = {}

    def research_input(name: str, reader: Callable[[], Any]) -> Any:
        """Read each research selector once so list and detail stay coherent."""
        cached = research_inputs.get(name)
        if cached:
            succeeded, value = cached
            if succeeded:
                return value
            raise value
        try:
            value = reader()
        except Exception as exc:
            research_inputs[name] = (False, exc)
            raise
        research_inputs[name] = (True, value)
        return value

    def research_pools() -> Any:
        return research_input("pools", research.research_pools)

    def research_industries() -> Any:
        return research_input(
            "industries",
            lambda: research.research_industries(compact=True),
        )

    def latest_decision_evidence() -> dict[str, Any]:
        """Freeze the first visible decision's full document beside its list.

        The evidence page must remain complete when the application database
        is busy.  Previously it snapshotted only compact rows and immediately
        issued two live requests for the selected row, so a transient timeout
        rendered valid nested evidence as ``undefined``/``未记录``.
        """

        if latest_decision_bundle:
            return latest_decision_bundle
        summaries = research.research_decisions(limit=1, compact=True)
        items = summaries.get("items") if isinstance(summaries, dict) else []
        first = items[0] if isinstance(items, list) and items else None
        decision_id = int(first.get("id") or 0) if isinstance(first, dict) else 0
        if not decision_id:
            latest_decision_bundle.update({"detail": {}, "verification": {}})
            return latest_decision_bundle
        latest_decision_bundle.update({
            "detail": research.research_decision_detail(decision_id),
            "verification": research.research_evidence_verify(decision_id),
        })
        return latest_decision_bundle

    common = {
        # The control worker builds durable PostgreSQL snapshots. It must not
        # inherit the API host's loopback Redis requirement: browser reads and
        # stream consumption remain checked by the API's normal health route.
        "health": read_health,
        "tradingReady": control_trading_ready,
    }

    def bounded_oos() -> dict[str, Any]:
        return _historical_diagnostic(
            "progressive_oos",
            data_center.data_center_oos_hard_data,
            include_dates=False,
        )

    def bounded_sealed() -> dict[str, Any]:
        return _historical_diagnostic(
            "sealed_holdout",
            data_center.data_center_sealed_strategy_holdout,
            include_dates=False,
        )

    pages: dict[str, dict[str, Callable[[], Any]]] = {
        "overview": {
            "oos": bounded_oos,
            "sealed": bounded_sealed,
            "owner": model_strategy.model_strategy_owner_summary,
            "ledger": quant_runs.quant_run_ledger_summary,
            "portfolio": lambda: research.research_portfolio_candles(limit=90),
            "latestPlan": lambda: decision_latest_plan(compact=True),
            "dailyUpdateStatus": lambda: data_center.data_center_daily_update_status(compact=True),
            # A stale watermark can request its own catalog rebuild, which in
            # turn invalidates this page. Snapshot assembly must only read the
            # current projection or it can continuously invalidate itself.
            "watermarks": lambda: data_center.data_center_asset_watermarks(
                trigger_refresh_if_stale=False,
            ),
            "ledgerAudit": model_strategy.model_strategy_ledger_audit,
        },
        "decision": {
            **common,
            "owner": model_strategy.model_strategy_owner_summary,
            "decision": lambda: decision_summary(limit_days=30),
            "latestPlan": lambda: decision_latest_plan(compact=True),
        },
        "portfolio": {
            "owner": model_strategy.model_strategy_owner_summary,
            "ledger": quant_runs.quant_run_ledger_summary,
            "positions": get_positions,
            "portfolio": lambda: research.research_portfolio_candles(limit=90),
            "risk": lambda: research.research_live_portfolio_risk(lookback=120),
            "research": lambda: research.research_profit_loop(compact=True),
            "latestPlan": lambda: decision_latest_plan(compact=True),
        },
        "operations": {
            "oos": bounded_oos,
            "sealed": bounded_sealed,
            "owner": model_strategy.model_strategy_owner_summary,
            "operationsPage": lambda: __import__("app.api.routes.positions", fromlist=["get_operations_page"]).get_operations_page(execution_id=None, limit=20, offset=0),
            "executionsPage": lambda: __import__("app.api.routes.positions", fromlist=["get_executions_page"]).get_executions_page(limit=20, offset=0),
            "ledgerAudit": model_strategy.model_strategy_ledger_audit,
        },
        "runtime": {
            "health": common["health"],
            "tradingReady": common["tradingReady"],
            "adminTasksLive": lambda: model_strategy.model_strategy_task_center(limit=16, log_limit=5, compact=True),
            "tasks": lambda: model_strategy.model_strategy_task_center(limit=16, log_limit=5, compact=True),
            "evolutionTasks": lambda: model_strategy.model_strategy_evolution_tasks(limit=20),
            "evolutionSchedule": model_strategy.model_strategy_evolution_schedule,
            "quantTasks": lambda: quant_runs.quant_run_tasks(limit=10),
            "quantStatus": quant_runs.quant_run_status,
            "quantRecent": lambda: quant_runs.quant_run_recent(limit=5),
        },
        "versions": {
            "systemVersionStatus": system_versions.system_version_status,
            "systemReleases": lambda: system_versions.system_version_releases(limit=30),
            "systemContracts": system_versions.system_version_contracts,
        },
        "data": {
            "oos": bounded_oos,
            "sealed": bounded_sealed,
            "watermarks": lambda: data_center.data_center_asset_watermarks(
                trigger_refresh_if_stale=False,
            ),
            "failedRequests": lambda: data_center.data_center_failed_tushare_requests(request_status="pending", limit=100),
            "dataOperations": lambda: data_center.data_center_llm_data_operations(limit=20),
            "runtimeLogs": lambda: data_center.data_center_runtime_logs(after_id=0, limit=100, wait_seconds=0.0),
            "dataObservability": lambda: data_center.data_center_observability(limit_logs=50, compact=True),
            "dataStreams": application_data_streams,
            "dailyUpdateStatus": lambda: data_center.data_center_daily_update_status(compact=True),
            "backfillStatus": data_center.data_center_backfill_status,
            "backfillPlan": lambda: data_center.data_center_backfill_plan(
                market_batch_days=30,
                financial_batch_size=100,
                rebuild=False,
            ),
            "tasks": lambda: model_strategy.model_strategy_task_center(limit=16, log_limit=40, compact=True),
        },
        "models": {
            "planModels": lambda: model_strategy.model_strategy_plan_models(console=True),
            "modelOperations": model_strategy.model_strategy_plan_model_operations,
            "models": model_strategy.model_strategy_status,
            "modelMarketLive": model_strategy.model_strategy_live_market_context,
            "modelGovernance": model_strategy.model_strategy_market_context_governance,
            "modelReports": model_strategy.model_strategy_performance_reports,
            "modelProduction": model_strategy.model_strategy_production_strategy,
            "modelController": model_strategy.model_strategy_strategy_controller,
            "modelOutputContracts": model_strategy.model_strategy_model_output_contracts,
            "modelEvidenceBindings": model_strategy.model_strategy_evidence_bindings,
            "modelValidation": model_strategy.model_strategy_multi_period_validation,
            "modelMarketHistory": lambda: model_strategy.model_strategy_market_context_history(limit=120),
            "factorResearchCompact": lambda: __import__("app.api.routes.factors", fromlist=["factor_research_status"]).factor_research_status(compact=True, console=False),
            "evolutionTasks": lambda: model_strategy.model_strategy_evolution_tasks(limit=30),
            "modelTasksLive": lambda: model_strategy.model_strategy_task_center(limit=24, log_limit=40, compact=True),
        },
        "research": {
            "researchContracts": research.research_capabilities,
            "researchGovernance": research.research_governance_overview,
            "researchPositionState": research.research_position_states,
            "researchDataAssets": lambda: model_strategy.model_strategy_evolution_research_data_assets() if hasattr(model_strategy, "model_strategy_evolution_research_data_assets") else {},
            "analystConsensus": lambda: model_strategy.model_strategy_analyst_consensus_batch(limit=100),
            "theses": lambda: research.research_thesis_list(limit=100),
            "industries": research_industries,
            "industryAllocation": lambda: research.research_industry_allocation(
                as_of_time="", market_state="", equity_budget=-1.0, compact=True,
            ),
            # Route handlers use FastAPI Query defaults.  The control worker
            # calls them directly, so provide concrete bounded values rather
            # than passing Query instances into service logic.
            "themes": lambda: research.research_themes(
                as_of_time="", promotable_only=False, limit=100, compact=True
            ),
            "events": lambda: research.research_events(ts_code="", limit=100),
            "pools": research_pools,
            "modelMarketLive": model_strategy.model_strategy_live_market_context,
            # Detail evidence is materialized with the research page so the
            # browser never turns the first render into a live fan-out.
            "researchStockEvidence": lambda: _research_detail_evidence(
                research, "stock", research_pools
            ),
            "researchIndustryEvidence": lambda: _research_detail_evidence(
                research, "industry", research_industries
            ),
        },
        "factors": {
            "owner": model_strategy.model_strategy_owner_summary,
            "strategy": model_strategy.model_strategy_strategy_controller,
            "factors": lambda: __import__("app.api.routes.factors", fromlist=["factor_registry"]).factor_registry(),
            "factorStatus": lambda: __import__("app.api.routes.factors", fromlist=["factor_research_status"]).factor_research_status(),
            # Evolution pages need the bounded v10 expert/matrix projection;
            # compact=True intentionally returns a deferred placeholder.
            "factorResearch": lambda: __import__("app.api.routes.factors", fromlist=["factor_research_status"]).factor_research_status(console=True, compact=False),
            "factorLineage": lambda: __import__("app.api.routes.factors", fromlist=["factor_strategy_lineage"]).factor_strategy_lineage(),
            "researchAnalytics": lambda: __import__("app.api.routes.factors", fromlist=["factor_research_analytics"]).factor_research_analytics(),
            "researchFactorAnalytics": lambda: __import__("app.api.routes.factors", fromlist=["factor_research_analytics_factors"]).factor_research_analytics_factors(limit=200),
            "researchTimeseries": lambda: __import__("app.api.routes.factors", fromlist=["factor_research_analytics_timeseries"]).factor_research_analytics_timeseries(limit=500),
            "evolutionTasks": lambda: model_strategy.model_strategy_evolution_tasks(limit=30),
            "candidateObservations": lambda: model_strategy.model_strategy_candidate_observations(limit=200),
            "ledger": quant_runs.quant_run_ledger_summary,
        },
        "waves": {
            "researchAnalyticsConsole": lambda: __import__("app.api.routes.factors", fromlist=["factor_research_analytics_console"]).factor_research_analytics_console(),
            "researchExpertHistory": lambda: __import__("app.api.routes.factors", fromlist=["factor_research_analytics_experts"]).factor_research_analytics_experts(limit=100),
            "researchEnvironmentSimilarity": lambda: __import__("app.api.routes.factors", fromlist=["factor_research_analytics_environment_similarity"]).factor_research_analytics_environment_similarity(),
            "researchArchive": lambda: __import__("app.api.routes.factors", fromlist=["factor_research_analytics_archive"]).factor_research_analytics_archive(),
        },
        "evidence": {
            # Direct route calls must replace FastAPI Query defaults with
            # concrete values; Query instances are request metadata, not
            # valid service-layer limits or filters.
            "decisionAudit": lambda: research.research_decision_audit(limit=100, compact=False),
            # EvidenceChain expects the frozen decision rows (id, action,
            # timestamps and point-in-time evidence), not the dashboard
            # summary object whose nested fields would render as undefined.
            "decisions": lambda: research.research_decisions(limit=100, compact=True),
            "decisionDetail": lambda: latest_decision_evidence()["detail"],
            "evidenceVerification": lambda: latest_decision_evidence()["verification"],
            "reviews": lambda: research.research_reviews(limit=100),
            "reviewLearning": research.research_review_learning_overview,
            "llmBudget": research.research_llm_token_budget,
            "llmProposals": lambda: research.research_llm_proposals(limit=50),
            "performanceReports": lambda: quant_runs.quant_run_performance_reports(
                task_id=None, limit=30
            ),
            "ledgerDetail": lambda: quant_runs.quant_run_simulation_ledger(execution_id=None, limit=20),
            "reportCompact": lambda: decision_summary(limit_days=30),
            "backtests": lambda: quant_runs.quant_run_performance_reports(
                task_id=None, limit=30
            ),
            "ledgerAudit": model_strategy.model_strategy_ledger_audit,
        },
    }
    return pages[page_key]


def _research_detail_evidence(
    research: Any,
    kind: str,
    source_reader: Callable[[], Any],
) -> dict[str, Any]:
    """Return one optional detail without invalidating the research document."""
    component = (
        "researchStockEvidence" if kind == "stock"
        else "researchIndustryEvidence"
    )
    try:
        source = source_reader()
        items = source.get("items", []) if isinstance(source, dict) else []
        if kind == "stock":
            code = (
                str(items[0].get("ts_code") or items[0].get("symbol") or "")
                if items else ""
            )
            value = research.research_console_stock_evidence(code) if code else None
        else:
            code = ""
            for item in items:
                candidate = str(
                    item.get("industry_code")
                    or item.get("code")
                    or item.get("name")
                    or ""
                )
                if candidate and candidate.lower() != "nan":
                    code = candidate
                    break
            value = research.research_industry_detail(code) if code else None
        if value is not None:
            return value
        return {
            "ok": False,
            "status": "partial",
            "data_available": False,
            "component": component,
            "items": [],
            "error": {
                "code": "research_detail_not_selected",
                "message": "当前列表没有可用于预载的详情项。",
            },
        }
    except Exception as exc:
        logger.warning(
            "Optional research snapshot detail unavailable component=%s error_type=%s",
            component,
            type(exc).__name__,
        )
        return {
            "ok": False,
            "status": "degraded",
            "data_available": False,
            "component": component,
            "items": [],
            "error": {
                "code": "research_detail_unavailable",
                "message": "可选研究详情暂不可读；研究列表与其他快照分区仍然有效。",
            },
        }


def _serialize(payload: dict[str, Any]) -> str:
    return json.dumps(jsonable_encoder(payload), ensure_ascii=False, separators=(",", ":"))


def _semantic_payload_hash(payload: dict[str, Any]) -> str:
    stable = {
        "page": payload.get("page"),
        "status": payload.get("status"),
        "sections": payload.get("sections", {}),
        "data": payload.get("data", {}),
    }
    return hashlib.sha256(_serialize(stable).encode("utf-8")).hexdigest()


def _business_metadata(data: dict[str, Any], sections: dict[str, dict[str, Any]]) -> dict[str, Any]:
    dates: list[str] = []
    business_failures: list[str] = []

    def visit(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                child = f"{path}.{key}" if path else key
                if key in {"trade_date", "latest_trade_date", "data_date", "data_as_of"}:
                    candidate = str(item or "").replace("-", "")[:8]
                    if len(candidate) == 8 and candidate.isdigit():
                        dates.append(candidate)
                if key == "status" and str(item or "") in {"failed", "partial", "blocked", "unavailable", "error"}:
                    business_failures.append(child)
                visit(item, child)
        elif isinstance(value, list):
            for index, item in enumerate(value[:200]):
                visit(item, f"{path}[{index}]")

    visit(data)
    document_errors = any(item.get("status") == "error" for item in sections.values())
    return {
        "document_status": "error" if document_errors else "ready",
        "business_status": "partial" if business_failures else "ready",
        "data_as_of": max(dates) if dates else None,
        "business_issues": business_failures[:20],
    }


def _encode_payload(payload: dict[str, Any]) -> tuple[str, bytes, str, int, int]:
    serialized = _serialize(payload)
    raw = serialized.encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=6, mtime=0)
    return serialized, compressed, _semantic_payload_hash(payload), len(raw), len(compressed)


def _decode_payload(record: dict[str, Any]) -> dict[str, Any] | None:
    compressed = record.get("payload_gzip")
    if compressed:
        try:
            raw = bytes(compressed) if isinstance(compressed, memoryview) else compressed
            payload = json.loads(gzip.decompress(raw).decode("utf-8"))
        except (OSError, TypeError, UnicodeDecodeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            return payload
    try:
        payload = json.loads(record.get("payload_json") or "")
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _app_cache_ttl() -> int:
    raw = os.getenv("ALIS_CONSOLE_SNAPSHOT_APP_CACHE_TTL_SECONDS", "")
    try:
        return max(60, int(raw)) if raw else _APP_CACHE_TTL_SECONDS
    except ValueError:
        return _APP_CACHE_TTL_SECONDS


def _generation(record: dict[str, Any] | None) -> int:
    try:
        return int((record or {}).get("refresh_generation") or 0)
    except (TypeError, ValueError):
        return 0


def _has_committed_snapshot(record: dict[str, Any] | None) -> bool:
    if not record:
        return False
    return bool(record.get("version")) and str(record.get("status") or "") in {"ready", "degraded"}


def _warming_payload(page_key: str, generation: int = 0) -> dict[str, Any]:
    return {
        "page": page_key,
        "version": "",
        "generated_at": "",
        "as_of": "",
        "status": "warming_up",
        "stale": False,
        "refreshing": True,
        "refresh_generation": generation,
        "sections": {},
        "data": {},
        "error": {
            "code": "snapshot_warming_up",
            "message": "页面快照正在由控制节点生成，请稍后自动重试。",
        },
    }


def _unavailable_payload(page_key: str, generation: int, sections: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "page": page_key,
        "version": "",
        "generated_at": "",
        "as_of": "",
        "status": "unavailable",
        "stale": False,
        "refreshing": True,
        "refresh_generation": generation,
        "sections": sections,
        "data": {},
        "error": {
            "code": "snapshot_build_unavailable",
            "message": "页面快照暂时未能生成，控制节点将自动重试。",
        },
    }


def _snapshot_metadata(page_key: str) -> dict[str, Any] | None:
    _ensure_table()
    with _read_conn() as conn:
        row = conn.execute(
            "SELECT page_key,version,generated_at,as_of,status,invalidated_at,updated_at,"
            "refresh_generation,last_attempt_generation,last_attempt_at,last_error_code,"
            "content_hash,payload_size_bytes,compressed_size_bytes "
            "FROM console_page_snapshots WHERE page_key=?",
            (page_key,),
        ).fetchone()
    return dict(row) if row else None


def _load_durable(page_key: str) -> dict[str, Any] | None:
    _ensure_table()
    with _read_conn() as conn:
        row = conn.execute(
            "SELECT page_key,version,generated_at,as_of,status,invalidated_at,updated_at,"
            "refresh_generation,last_attempt_generation,last_attempt_at,last_error_code,payload_json,"
            "payload_gzip,content_hash,payload_size_bytes,compressed_size_bytes "
            "FROM console_page_snapshots WHERE page_key=?",
            (page_key,),
        ).fetchone()
    if not row:
        return None
    record = dict(row)
    payload = _decode_payload(record)
    if payload is None:
        return None
    payload["generated_at"] = str(record.get("generated_at") or payload.get("generated_at") or "")
    payload["as_of"] = str(record.get("as_of") or payload.get("as_of") or "")
    payload["status"] = str(record.get("status") or payload.get("status") or "")
    payload["refresh_generation"] = _generation(record)
    return payload


def _ensure_snapshot_record(page_key: str) -> dict[str, Any]:
    """Create a durable cold-start sentinel before observing a generation."""
    _ensure_table()
    now = _now()
    payload = _serialize(_warming_payload(page_key))
    with _conn() as conn:
        conn.execute(
            """INSERT INTO console_page_snapshots
                (page_key,version,generated_at,as_of,status,invalidated_at,payload_json,updated_at,
                 refresh_generation,last_attempt_generation,last_attempt_at,last_error_code)
               VALUES (?, '', '', '', 'warming_up', ?, ?, ?, 0, -1, NULL, '')
               ON CONFLICT (page_key) DO NOTHING""",
            (page_key, now, payload, now),
        )
        row = conn.execute(
            "SELECT page_key,version,status,invalidated_at,refresh_generation,content_hash "
            "FROM console_page_snapshots WHERE page_key=?",
            (page_key,),
        ).fetchone()
    if not row:
        raise RuntimeError("console_snapshot_record_unavailable")
    return dict(row)


def _save_durable(page_key: str, payload: dict[str, Any], expected_generation: int) -> bool:
    """Commit only if no invalidation happened while the readers were running."""
    _ensure_table()
    _serialized, compressed, content_hash, payload_size, compressed_size = _encode_payload(payload)
    with _conn() as conn:
        existing = conn.execute(
            "SELECT version,status,content_hash FROM console_page_snapshots "
            "WHERE page_key=? AND refresh_generation=?",
            (page_key, expected_generation),
        ).fetchone()
        existing_record = dict(existing) if existing else None
        if _has_committed_snapshot(existing_record) and str(existing_record.get("content_hash") or "") == content_hash:
            row = conn.execute(
                """UPDATE console_page_snapshots SET
                        generated_at=?, as_of=?, status=?, invalidated_at=NULL, updated_at=?,
                        last_attempt_generation=-1,
                        last_attempt_at=NULL, last_error_code=''
                    WHERE page_key=? AND refresh_generation=?
                    RETURNING refresh_generation""",
                (
                    payload["generated_at"], payload["as_of"], payload["status"],
                    payload["generated_at"], page_key, expected_generation,
                ),
            ).fetchone()
            return row is not None
        row = conn.execute(
            """UPDATE console_page_snapshots SET
                    version=?, generated_at=?, as_of=?, status=?, invalidated_at=NULL,
                    payload_json=?, payload_gzip=?, content_hash=?, payload_size_bytes=?,
                    compressed_size_bytes=?, updated_at=?, last_attempt_generation=-1,
                    last_attempt_at=NULL, last_error_code=''
                WHERE page_key=? AND refresh_generation=?
                RETURNING refresh_generation""",
            (
                payload["version"],
                payload["generated_at"],
                payload["as_of"],
                payload["status"],
                "{}",
                compressed,
                content_hash,
                payload_size,
                compressed_size,
                payload["generated_at"],
                page_key,
                expected_generation,
            ),
        ).fetchone()
    return row is not None


def _record_failed_build(
    page_key: str,
    expected_generation: int,
    candidate: dict[str, Any],
) -> bool:
    """Record bounded retry state without replacing a last-known-good page."""
    _ensure_table()
    now = _now()
    with _conn() as conn:
        row = conn.execute(
            """UPDATE console_page_snapshots SET
                    last_attempt_generation=?, last_attempt_at=?,
                    last_error_code='snapshot_section_unavailable', updated_at=?
                WHERE page_key=? AND refresh_generation=?
                RETURNING version,status""",
            (expected_generation, now, now, page_key, expected_generation),
        ).fetchone()
        if not row:
            return False
        current = dict(row)
        if _has_committed_snapshot(current):
            return True
        unavailable = _unavailable_payload(page_key, expected_generation, candidate["sections"])
        persisted = conn.execute(
            """UPDATE console_page_snapshots SET
                    status='error', payload_json=?, updated_at=?
                WHERE page_key=? AND refresh_generation=? AND version=''
                  AND status IN ('warming_up','error')
                RETURNING refresh_generation""",
            (_serialize(unavailable), now, page_key, expected_generation),
        ).fetchone()
    return persisted is not None


def invalidate(page_keys: list[str] | tuple[str, ...] = PAGE_KEYS, *, reason: str = "source_changed") -> None:
    """Durably advance a page generation before invalidating the app cache."""
    del reason  # The durable generation is the cross-node invalidation evidence.
    valid_page_keys = tuple(page_key for page_key in page_keys if page_key in PAGE_KEYS)
    # These pages materialize progressive OOS and sealed-holdout diagnostics.
    # Retire their shared 300-second cache before exposing a newer durable
    # generation, so the next builder cannot stamp old evidence with a fresh
    # generated_at.  The generation also prevents an audit already in flight
    # from restoring its pre-invalidation value after this clear.
    if _DIAGNOSTIC_PAGE_KEYS.intersection(valid_page_keys):
        _clear_diagnostic_cache()
    _ensure_table()
    now = _now()
    invalidated: list[str] = []
    with _conn() as conn:
        for page_key in valid_page_keys:
            sentinel = _serialize(_warming_payload(page_key, 1))
            conn.execute(
                """INSERT INTO console_page_snapshots
                    (page_key,version,generated_at,as_of,status,invalidated_at,payload_json,updated_at,
                     refresh_generation,last_attempt_generation,last_attempt_at,last_error_code)
                   VALUES (?, '', '', '', 'warming_up', ?, ?, ?, 1, -1, NULL, '')
                   ON CONFLICT (page_key) DO UPDATE SET
                     invalidated_at=EXCLUDED.invalidated_at,
                     refresh_generation=console_page_snapshots.refresh_generation + 1,
                     updated_at=EXCLUDED.updated_at""",
                (page_key, now, sentinel, now),
            )
            invalidated.append(page_key)
    # Redis belongs to the application host; only remove it after PostgreSQL
    # commits the newer generation. A delayed cache deletion cannot override
    # the metadata check in read().
    for page_key in invalidated:
        redis_delete(_redis_key(page_key))


def request_refresh(page_key: str) -> bool:
    """Queue one browser-requested rebuild without starving its writer.

    A manual refresh is advisory: once a page is already invalidated, further
    clicks must not keep advancing its generation while the control worker is
    assembling the replacement document.  Source mutations keep using
    ``invalidate()`` and therefore retain the stricter compare-and-swap rule.
    """
    if page_key not in PAGE_KEYS:
        raise ValueError(f"unsupported console page: {page_key}")
    _ensure_table()
    now = _now()
    sentinel = _serialize(_warming_payload(page_key, 1))
    with _conn() as conn:
        row = conn.execute(
            """INSERT INTO console_page_snapshots
                (page_key,version,generated_at,as_of,status,invalidated_at,payload_json,updated_at,
                 refresh_generation,last_attempt_generation,last_attempt_at,last_error_code)
               VALUES (?, '', '', '', 'warming_up', ?, ?, ?, 1, -1, NULL, '')
               ON CONFLICT (page_key) DO UPDATE SET
                 invalidated_at=EXCLUDED.invalidated_at,
                 refresh_generation=console_page_snapshots.refresh_generation + 1,
                 updated_at=EXCLUDED.updated_at
               WHERE console_page_snapshots.invalidated_at IS NULL
               RETURNING refresh_generation""",
            (page_key, now, sentinel, now),
        ).fetchone()
    if row is not None:
        redis_delete(_redis_key(page_key))
        return True
    return False


def _parse_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def snapshot_health(*, now: datetime | None = None) -> dict[str, Any]:
    """Assess the end-to-end durable snapshot chain.

    ``ready`` means the worker heartbeat is current and every page remains
    within its bounded stale allowance. ``fresh`` on each page separately
    records the browser TTL state. ``degraded`` means a committed document has
    an explicitly blocked business-data section. ``stale`` means a
    last-known-good document exceeded its allowed stale age. A missing/expired
    worker heartbeat is reported separately as ``worker_unavailable`` so an
    operator can distinguish a data gate from a stopped service.
    """

    checked_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    worker = worker_runtime_status(now=checked_at)
    try:
        _ensure_table()
        with _read_conn() as conn:
            rows = conn.execute(
                """SELECT page_key,version,generated_at,as_of,status,invalidated_at,
                          updated_at,refresh_generation,last_attempt_generation,
                          last_attempt_at,last_error_code
                     FROM console_page_snapshots"""
            ).fetchall()
    except Exception as exc:
        logger.warning("Console snapshot page health unavailable error_type=%s", type(exc).__name__)
        worker_live = bool(worker.get("heartbeat_healthy", worker.get("live", worker.get("healthy"))))
        return {
            "ok": False,
            "healthy": False,
            "degraded": False,
            "stale": worker_live,
            "status": "worker_unavailable" if not worker_live else "stale",
            "reason": (
                str(worker.get("reason") or "snapshot_worker_unavailable")
                if not worker_live
                else "snapshot_page_health_unavailable"
            ),
            "checked_at": checked_at.isoformat(),
            "worker": worker,
            "pages": {},
            "summary": {"ready": 0, "degraded": 0, "stale": 0, "unavailable": len(PAGE_KEYS)},
            "error": {"code": "snapshot_page_health_unavailable"},
        }

    by_page = {str(dict(row).get("page_key") or ""): dict(row) for row in rows}
    pages: dict[str, dict[str, Any]] = {}
    counts = {"ready": 0, "degraded": 0, "stale": 0, "unavailable": 0}
    for page_key in PAGE_KEYS:
        record = by_page.get(page_key)
        if not record:
            item = {
                "status": "unavailable",
                "healthy": False,
                "current": False,
                "fresh": False,
                "refresh_due": True,
                "usable": False,
                "last_known_good": False,
                "reason": "snapshot_missing",
                "generated_at": None,
                "age_seconds": None,
                "ttl_seconds": _ttl(page_key),
                "allowed_stale_seconds": _worker_page_stale_seconds(page_key),
                "version": "",
                "last_error_code": "",
            }
            pages[page_key] = item
            counts["unavailable"] += 1
            continue
        generated = _parse_timestamp(record.get("generated_at"))
        age = None if generated is None else max(0.0, (checked_at - generated).total_seconds())
        committed = _has_committed_snapshot(record)
        invalidated = bool(record.get("invalidated_at"))
        invalidated_at = _parse_timestamp(record.get("invalidated_at"))
        invalidated_age = (
            None if invalidated_at is None else max(0.0, (checked_at - invalidated_at).total_seconds())
        )
        fresh = bool(committed and not invalidated and age is not None and age <= _ttl(page_key))
        refresh_due = not fresh
        stale_age_exceeded = age is None or age > _worker_page_stale_seconds(page_key)
        invalidation_exceeded = invalidated and (
            invalidated_age is None or invalidated_age > _worker_page_stale_seconds(page_key)
        )
        if not committed:
            state = "unavailable"
            reason = "snapshot_not_committed"
        elif stale_age_exceeded or invalidation_exceeded:
            state = "stale"
            reason = "source_invalidation_age_exceeded" if invalidated else "snapshot_age_exceeded"
        elif str(record.get("status") or "") == "degraded":
            state = "degraded"
            reason = "snapshot_business_data_degraded"
        else:
            state = "ready"
            reason = "source_invalidated" if invalidated else "snapshot_ttl_expired" if refresh_due else ""
        counts[state] += 1
        pages[page_key] = {
            "status": state,
            "healthy": state == "ready",
            "current": state in {"ready", "degraded"},
            "fresh": fresh,
            "refresh_due": refresh_due,
            "usable": state in {"ready", "degraded", "stale"},
            "last_known_good": committed,
            "reason": reason,
            "generated_at": record.get("generated_at"),
            "as_of": record.get("as_of"),
            "age_seconds": round(age, 3) if age is not None else None,
            "ttl_seconds": _ttl(page_key),
            "allowed_stale_seconds": _worker_page_stale_seconds(page_key),
            "version": record.get("version") or "",
            "refresh_generation": _generation(record),
            "invalidated_at": record.get("invalidated_at"),
            "invalidation_age_seconds": round(invalidated_age, 3) if invalidated_age is not None else None,
            "last_attempt_at": record.get("last_attempt_at"),
            "last_error_code": record.get("last_error_code") or "",
        }

    # ``healthy`` is the operational signal.  Older/mocked worker payloads
    # only expose that field, so retain a compatibility fallback while using
    # the explicit heartbeat signal for liveness decisions.
    worker_live = bool(worker.get("heartbeat_healthy", worker.get("live", worker.get("healthy"))))
    worker_degraded = bool(
        worker.get("degraded")
        or str(worker.get("operational_status") or "").lower() == "degraded"
        or str(worker.get("status") or "").lower() == "degraded"
    )
    if not worker_live:
        overall_status = "worker_unavailable"
        overall_reason = str(worker.get("reason") or "snapshot_worker_unavailable")
    elif counts["unavailable"] or counts["stale"]:
        overall_status = "stale"
        overall_reason = "snapshot_page_stale"
    elif counts["degraded"] or worker_degraded:
        overall_status = "degraded"
        overall_reason = (
            str(worker.get("reason") or "snapshot_worker_degraded")
            if worker_degraded and not counts["degraded"]
            else "snapshot_page_degraded"
        )
    else:
        overall_status = "ready"
        overall_reason = ""
    return {
        "ok": overall_status in {"ready", "degraded"},
        "healthy": overall_status == "ready",
        "degraded": overall_status == "degraded",
        "stale": overall_status == "stale",
        "status": overall_status,
        "reason": overall_reason,
        "checked_at": checked_at.isoformat(),
        "worker": worker,
        "pages": pages,
        "summary": counts,
        "required_pages": list(PAGE_KEYS),
    }


def _retry_is_deferred(record: dict[str, Any], now: datetime) -> bool:
    try:
        attempted_generation = int(record.get("last_attempt_generation"))
    except (TypeError, ValueError):
        return False
    if attempted_generation != _generation(record):
        return False
    attempted_at = _parse_timestamp(record.get("last_attempt_at"))
    if attempted_at is None:
        return False
    return (now - attempted_at).total_seconds() < _failure_retry_seconds()


def _pages_due_for_refresh() -> list[str]:
    """Use PostgreSQL state as the cross-node refresh protocol."""
    _ensure_table()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT page_key,version,generated_at,status,invalidated_at,refresh_generation,"
            "last_attempt_generation,last_attempt_at FROM console_page_snapshots"
        ).fetchall()
    by_page = {str(dict(row).get("page_key")): dict(row) for row in rows}
    now = datetime.now(timezone.utc)
    due: list[str] = []
    # A browser- or data-triggered invalidation must not wait behind a slow
    # periodic refresh of an unrelated page.  This keeps user-visible pages
    # on their latest known-good snapshot while the control worker is serial.
    ordered_page_keys = tuple(sorted(
        PAGE_KEYS,
        key=lambda page_key: (
            0 if page_key == "runtime" else 1,
            0 if (by_page.get(page_key) or {}).get("invalidated_at") else 1,
        ),
    ))
    for page_key in ordered_page_keys:
        record = by_page.get(page_key)
        if not record:
            due.append(page_key)
            continue
        if _retry_is_deferred(record, now):
            continue
        if record.get("invalidated_at") or not _has_committed_snapshot(record):
            due.append(page_key)
            continue
        generated_at = _parse_timestamp(record.get("generated_at"))
        if generated_at is None or (now - generated_at).total_seconds() >= _ttl(page_key):
            due.append(page_key)
    return due


def _wait_or_stop(stopper: Any | None, seconds: float) -> bool:
    if stopper is None:
        time.sleep(seconds)
        return False
    return bool(stopper.wait(seconds))


def _build_with_heartbeat_pulse(page_key: str) -> dict[str, Any]:
    """Keep a durable liveness signal while one serial page build is slow.

    A page reader can legitimately perform a bounded historical aggregation.
    Its liveness must not depend on returning before the worker heartbeat
    threshold, otherwise a healthy busy process looks identical to a stopped
    systemd unit.  The pulse only writes the small runtime row; it never runs
    another page reader or expands aggregation concurrency.
    """

    finished = Event()

    def pulse() -> None:
        while not finished.wait(_SNAPSHOT_WORKER_HEARTBEAT_INTERVAL_SECONDS):
            record_worker_heartbeat(
                status="running",
                current_page=page_key,
                details={"event": "page_build_heartbeat", "page": page_key},
            )

    thread = Thread(target=pulse, name=f"console-snapshot-heartbeat-{page_key}", daemon=True)
    thread.start()
    try:
        return build(page_key)
    finally:
        finished.set()
        thread.join(timeout=1.0)


def _control_work_active(*, include_research: bool = True) -> bool:
    """Do not make page aggregation compete with database-heavy autonomy."""

    with _read_conn() as conn:
        placeholders = ",".join("?" for _ in _DB_HEAVY_TASK_TYPES)
        row = conn.execute(
            f"""SELECT 1 AS active
                 FROM evolution_tasks
                WHERE status='running'
                  AND (
                    COALESCE(worker_pool,'control')='control'
                    OR (? AND task_type IN ({placeholders}))
                  )
                LIMIT 1""",
            (include_research, *sorted(_DB_HEAVY_TASK_TYPES)),
        ).fetchone()
    return bool(row)


def _data_ingestion_active() -> bool:
    """Yield snapshots while a live data-platform ingestion owns the DB."""

    from app.data_platform.db import _read_conn as data_read_conn

    with data_read_conn() as conn:
        row = conn.execute(
            """SELECT started_at
                 FROM data_ingestion_runs
                WHERE status='running' AND finished_at IS NULL
                ORDER BY id DESC LIMIT 1"""
        ).fetchone()
    if not row:
        return False
    started_at = str(dict(row).get("started_at") or "")
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - started.astimezone(timezone.utc) <= timedelta(hours=6)
    except ValueError:
        # A malformed live row is not trustworthy enough to let a costly
        # snapshot audit start beside it.  The ingestion recovery path will
        # reconcile the row; fail closed here meanwhile.
        return True


def _snapshot_build_should_yield(page_key: str = "") -> bool:
    if not optional_network_work_allowed() or _data_ingestion_active():
        return True
    if not _control_work_active():
        return False
    # On the DB-local, resource-limited snapshot worker, a continuous research
    # backlog must not starve the console forever. Allow one serial build once
    # its last successful document is 30 minutes old. Control/ingestion work
    # retains absolute priority; other deployment topologies keep yielding.
    if os.getenv("ALIS_CONSOLE_SNAPSHOT_BOUNDED_RESEARCH_YIELD", "0") != "1" or not page_key:
        return True
    metadata = _snapshot_metadata(page_key) or {}
    generated_at = _parse_timestamp(metadata.get("generated_at"))
    if generated_at is None or (
        datetime.now(timezone.utc) - generated_at
    ).total_seconds() >= _SNAPSHOT_FORCE_REFRESH_SECONDS:
        # A bounded control/research workload must not turn a stale console
        # into a permanent outage.  Allow one serial page build once its
        # maximum staleness is exceeded; ingestion still has absolute priority.
        return False
    if _control_work_active(include_research=False):
        return True
    return True


def run_snapshot_worker_forever(stop_event: Any | None = None) -> None:
    """Run only on the control worker; serialize expensive page aggregation."""
    stopper = stop_event
    record_worker_heartbeat(status="starting", force=True)
    try:
        while stopper is None or not stopper.is_set():
            record_worker_heartbeat(
                status="running",
                details={"event": "cycle_started"},
            )
            try:
                due = _pages_due_for_refresh()
            except Exception as exc:  # database failures must not involve the API host
                logger.warning("Console snapshot due scan unavailable error_type=%s", type(exc).__name__)
                record_worker_heartbeat(
                    status="degraded",
                    last_error_code="snapshot_due_scan_unavailable",
                    details={"error_type": type(exc).__name__},
                    force=True,
                )
                if _wait_or_stop(stopper, 5.0):
                    return
                continue
            for page_key in due:
                if stopper is not None and stopper.is_set():
                    return
                try:
                    if page_key != "runtime" and _snapshot_build_should_yield(page_key):
                        record_worker_heartbeat(
                            status="running",
                            current_page="",
                            details={"last_skipped_page": page_key, "reason": "workload_gate"},
                            force=True,
                        )
                        continue
                except Exception as exc:
                    logger.warning(
                        "Console snapshot workload gate unavailable error_type=%s",
                        type(exc).__name__,
                    )
                    record_worker_heartbeat(
                        status="degraded",
                        last_error_code="snapshot_workload_gate_unavailable",
                        details={"error_type": type(exc).__name__, "page": page_key},
                        force=True,
                    )
                    break
                record_worker_heartbeat(
                    status="running",
                    current_page=page_key,
                    details={"event": "page_started", "page": page_key},
                    force=True,
                )
                try:
                    result = _build_with_heartbeat_pulse(page_key)
                    result_status = str((result or {}).get("status") or "error")
                    if result_status in {"ready", "degraded"}:
                        record_worker_heartbeat(
                            status="running" if result_status == "ready" else "degraded",
                            current_page="",
                            last_success_page=page_key,
                            last_error_code="" if result_status == "ready" else "snapshot_business_data_degraded",
                            details={"event": "page_completed", "page": page_key, "snapshot_status": result_status},
                            force=True,
                        )
                    else:
                        record_worker_heartbeat(
                            status="degraded",
                            current_page="",
                            last_error_code="snapshot_build_error",
                            details={"event": "page_failed", "page": page_key, "snapshot_status": result_status},
                            force=True,
                        )
                except Exception as exc:  # retain any LKG; the next scan retries safely
                    logger.warning(
                        "Console snapshot build unavailable page=%s error_type=%s",
                        page_key,
                        type(exc).__name__,
                    )
                    record_worker_heartbeat(
                        status="degraded",
                        current_page="",
                        last_error_code="snapshot_build_unavailable",
                        details={"event": "page_exception", "page": page_key, "error_type": type(exc).__name__},
                        force=True,
                    )
                # Never fan out expensive aggregations across the small DB.
                if _wait_or_stop(stopper, 1.0):
                    return
            # A workload-gate break leaves due pages in the queue. Without a wait
            # this spins on PostgreSQL continuously while research owns the DB.
            record_worker_heartbeat(
                status="running",
                current_page="",
                details={"event": "cycle_completed", "pages_due": len(due)},
            )
            if _wait_or_stop(stopper, 5.0 if due else 1.0):
                return
    finally:
        # A graceful stop is useful evidence: health should not treat an
        # intentionally stopped service as a live worker until it starts again.
        record_worker_heartbeat(status="stopped", current_page="", force=True)


def build(page_key: str) -> dict[str, Any]:
    if page_key not in PAGE_KEYS:
        raise ValueError(f"unsupported console page: {page_key}")
    record = _ensure_snapshot_record(page_key)
    expected_generation = _generation(record)
    data: dict[str, Any] = {}
    sections: dict[str, dict[str, Any]] = {}
    try:
        readers = _readers(page_key)
        for key, reader in readers.items():
            data[key], sections[key] = _section(page_key, key, reader)
    except Exception as exc:  # import/setup failures are a controlled cold-start error
        logger.warning(
            "Console snapshot reader setup unavailable page=%s error_type=%s",
            page_key,
            type(exc).__name__,
        )
        error = {
            "code": "snapshot_reader_setup_unavailable",
            "message": "页面读取组件暂时不可用，系统将自动重试。",
        }
        sections = {"snapshot": {"status": "error", "error": error}}
        data = {"snapshot": {"ok": False, "status": "unavailable", "error": error}}
    generated_at = _now()
    errors = [name for name, detail in sections.items() if detail["status"] == "error"]
    degraded = [name for name, detail in sections.items() if detail["status"] == "degraded"]
    payload = {
        "page": page_key,
        "version": f"{page_key}-{uuid.uuid4().hex[:16]}",
        "generated_at": generated_at,
        "as_of": generated_at,
        "status": "error" if errors else "degraded" if degraded else "ready",
        "stale": False,
        "refreshing": False,
        "refresh_generation": expected_generation,
        "sections": sections,
        "data": data,
        **_business_metadata(data, sections),
    }
    if errors:
        _record_failed_build(page_key, expected_generation, payload)
        return payload
    # The application host owns Redis. It mirrors this committed PostgreSQL
    # version only after its next read, so a worker never writes remote cache.
    _save_durable(page_key, payload, expected_generation)
    return payload


def _cache_matches_metadata(cached: dict[str, Any], metadata: dict[str, Any]) -> bool:
    cached_generated_at = str(cached.get("generated_at") or "")
    metadata_generated_at = str(metadata.get("generated_at") or "")
    # Version/generation alone is not sufficient across application hosts:
    # an old Redis value can retain both after a partial cache write or a
    # restored Redis snapshot.  When timestamps are present, the durable
    # PostgreSQL record is authoritative and the payload must be from that
    # exact build.  Keep compatibility with compact test/legacy cache values
    # that do not carry the optional fields.
    if cached_generated_at and metadata_generated_at and cached_generated_at != metadata_generated_at:
        return False
    cached_hash = str(cached.get("content_hash") or "")
    metadata_hash = str(metadata.get("content_hash") or "")
    if metadata_hash and (cached_hash or "data" in cached or "sections" in cached):
        # Most payloads predate the explicit content_hash wrapper field, so
        # derive the same semantic hash used at durable commit time.
        observed_hash = cached_hash or _semantic_payload_hash(cached)
        if observed_hash != metadata_hash:
            return False
    elif cached_hash and metadata_hash and cached_hash != metadata_hash:
        return False
    return (
        _has_committed_snapshot(metadata)
        and not metadata.get("invalidated_at")
        and str(cached.get("version") or "") == str(metadata.get("version") or "")
        and _generation(cached) == _generation(metadata)
    )


def _with_runtime_overlay(page_key: str, response: dict[str, Any]) -> dict[str, Any]:
    if page_key not in {"overview", "operations"}:
        return response
    runtime = read("runtime")
    runtime_data = runtime.get("data") if isinstance(runtime, dict) else None
    if not isinstance(runtime_data, dict) or not runtime_data:
        return response
    combined = copy.deepcopy(response)
    combined_data = combined.setdefault("data", {})
    for key in _RUNTIME_OVERLAY_KEYS:
        if key in runtime_data:
            combined_data[key] = runtime_data[key]
    combined["runtime_generated_at"] = runtime.get("generated_at", "")
    combined["runtime_status"] = runtime.get("status", "warming_up")
    return combined


def read(page_key: str, *, force: bool = False) -> dict[str, Any]:
    if page_key not in PAGE_KEYS:
        raise ValueError(f"unsupported console page: {page_key}")
    metadata = _snapshot_metadata(page_key)
    generated_at = _parse_timestamp((metadata or {}).get("generated_at"))
    expired = generated_at is None or (
        datetime.now(timezone.utc) - generated_at
    ).total_seconds() >= _ttl(page_key)
    cached = None if force else get_json(_redis_key(page_key))
    if isinstance(cached, dict) and metadata and _cache_matches_metadata(cached, metadata):
        response = copy.deepcopy(cached)
        response["stale"] = expired
        response["refreshing"] = expired
        response["stale_reason"] = "snapshot_ttl_expired" if expired else ""
        return _with_refresh_runtime(_with_runtime_overlay(page_key, response), metadata)
    if isinstance(cached, dict):
        redis_delete(_redis_key(page_key))
    generation = _generation(metadata)
    durable = _load_durable(page_key) if metadata else None
    if metadata and _has_committed_snapshot(metadata) and durable:
        response = copy.deepcopy(durable)
        response["stale"] = expired or bool(metadata.get("invalidated_at"))
        response["refreshing"] = response["stale"]
        response["stale_reason"] = (
            "source_invalidated" if metadata.get("invalidated_at")
            else "snapshot_ttl_expired" if expired else ""
        )
        # Expiry affects freshness, not content identity. Mirror the committed
        # version while waiting, avoiding repeated cross-node payload transfers.
        if not metadata.get("invalidated_at"):
            set_json(_redis_key(page_key), response, ttl=_app_cache_ttl())
        return _with_refresh_runtime(_with_runtime_overlay(page_key, response), metadata)
    if durable and str(durable.get("status") or "") == "unavailable":
        durable["stale"] = False
        durable["refreshing"] = True
        return _with_refresh_runtime(_with_runtime_overlay(page_key, durable), metadata)
    return _with_refresh_runtime(
        _with_runtime_overlay(page_key, _warming_payload(page_key, generation)),
        metadata,
    )


def status() -> dict[str, Any]:
    _ensure_table()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT page_key,version,generated_at,as_of,status,invalidated_at,updated_at,"
            "refresh_generation,last_attempt_generation,last_attempt_at,last_error_code,"
            "content_hash,payload_size_bytes,compressed_size_bytes "
            "FROM console_page_snapshots ORDER BY page_key"
        ).fetchall()
    items = [dict(row) for row in rows]
    health = snapshot_health()
    return {
        # The endpoint itself remains readable during an outage, while `ok`
        # describes whether its page/worker chain is currently usable.
        "ok": bool(health.get("ok")),
        "items": items,
        "pages": list(PAGE_KEYS),
        "health": health,
        "payload_size_bytes": sum(int(item.get("payload_size_bytes") or 0) for item in items),
        "compressed_size_bytes": sum(int(item.get("compressed_size_bytes") or 0) for item in items),
    }
