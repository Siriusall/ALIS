from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import logging
import os
import socket
import threading
import time
import uuid
from copy import deepcopy
from typing import Any, Callable

from app.data_platform.services.oos_hard_data import audit_oos_hard_data
from app.data_platform.db import _conn as data_conn
from app.core.trade_calendar import is_trade_day
from app.db.store import (
    _conn,
    get_model_version,
    get_strategy,
    list_model_versions,
    update_model_version_metrics,
    update_model_version_status,
    update_model_version_validation,
)
from app.services.production_strategy import resolve_production_strategy_champion
from app.services.walk_forward_validation import run_purged_walk_forward_validation
from app.services.strategy_replay import run_strategy_replay
from app.services.strategy_config import get_replay_required_factor_names
from app.services.backtest_data_source import BacktestDataSource
from app.services.strategy_lifecycle import advance_formal_candidate_lifecycle
from app.services.strategy_statistics import deflated_sharpe_ratio


logger = logging.getLogger("quant.evolution")
ACTIVE_STATES = ("queued", "running", "retry_wait")
TERMINAL_STATES = ("completed", "failed", "cancelled")
LEASE_MINUTES = max(10, min(int(os.getenv("EVOLUTION_LEASE_MINUTES", "30")), 180))
POLL_SECONDS = max(2, min(int(os.getenv("EVOLUTION_POLL_SECONDS", "5")), 60))
AUTO_ENABLED = str(os.getenv("EVOLUTION_AUTO_ENABLED", "1")).lower() in {"1", "true", "yes", "on"}
V10_RESEARCH_ENABLED = str(os.getenv("ALIS_V10_RESEARCH_ENABLED", "1")).lower() in {"1", "true", "yes", "on"}
LOW_MEMORY_WORKER = str(os.getenv("ALIS_WORKER_PROFILE", "")).lower() in {"2gb", "low_memory", "low-memory"}
WORKER_RESEARCH_BATCH_SIZE = 1 if LOW_MEMORY_WORKER else 4
AUTO_WEEKDAY = max(0, min(int(os.getenv("EVOLUTION_AUTO_WEEKDAY", "4")), 6))
AUTO_HOUR = max(0, min(int(os.getenv("EVOLUTION_AUTO_HOUR", "18")), 23))
MAX_CANDIDATES_PER_CYCLE = max(1, min(int(os.getenv("EVOLUTION_MAX_CANDIDATES", "6")), 20))
MAX_WINDOWS_PER_CANDIDATE = max(1, min(int(os.getenv("EVOLUTION_MAX_WINDOWS", "5")), 20))
REQUIRED_OPPORTUNITY_COST_BASELINES = ("multifactor_equal_weight", "simple_momentum")
TASK_TIMEOUT_MINUTES = max(30, min(int(os.getenv("EVOLUTION_TASK_TIMEOUT_MINUTES", "240")), 1440))
DECISION_DATA_RETRY_SECONDS = max(
    60, min(int(os.getenv("EVOLUTION_DECISION_DATA_RETRY_SECONDS", "300")), 3600)
)
RESOURCE_BUDGET_RETRY_SECONDS = max(
    2, min(int(os.getenv("EVOLUTION_RESOURCE_BUDGET_RETRY_SECONDS", "5")), 300)
)
RESOURCE_BUDGET_MAX_STALLED_RESUMES = max(
    1, min(int(os.getenv("EVOLUTION_RESOURCE_BUDGET_MAX_STALLED_RESUMES", "2")), 10)
)
LEASE_RECOVERY_INTERVAL_SECONDS = max(
    15, min(int(os.getenv("EVOLUTION_LEASE_RECOVERY_INTERVAL_SECONDS", "30")), 300)
)
HEARTBEAT_PULSE_SECONDS = max(
    1, min(int(os.getenv("EVOLUTION_HEARTBEAT_PULSE_SECONDS", "5")), 60)
)
HEARTBEAT_DB_RETRY_ATTEMPTS = max(
    1, min(int(os.getenv("EVOLUTION_HEARTBEAT_DB_RETRY_ATTEMPTS", "3")), 5)
)
HEARTBEAT_DB_RETRY_BACKOFF_SECONDS = max(
    0.1, min(float(os.getenv("EVOLUTION_HEARTBEAT_DB_RETRY_BACKOFF_SECONDS", "0.5")), 5.0)
)
LEGACY_TASK_RECONCILIATION_VERSION = "alis_evolution_task_reconciliation_v1"
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
SYSTEM_AUTONOMY_RECOVERY_POLICY_VERSION = "v3"
SYSTEM_AUTONOMY_MAX_RECOVERY_CONTINUATIONS = 2
_worker_started = False
_scheduler_started = False
_lease_recovery_started = False
_start_lock = threading.Lock()
_runtime_lock = threading.Lock()
_active_task_lock = threading.Lock()
_active_task_ids: set[int] = set()
_runtime_status: dict[str, dict[str, Any]] = {
    "worker": {"started": False, "alive": False, "heartbeat_at": None, "last_error": ""},
    "scheduler": {"started": False, "alive": False, "heartbeat_at": None, "last_error": ""},
}


class EvolutionTaskCancelled(RuntimeError):
    pass


class EvolutionTaskBudgetExceeded(EvolutionTaskCancelled):
    pass


def _now() -> datetime:
    return datetime.now().astimezone()


def _runtime_heartbeat(component: str, *, alive: bool = True, error: str = "") -> None:
    with _runtime_lock:
        state = _runtime_status.setdefault(component, {})
        state.update({"started": True, "alive": bool(alive), "heartbeat_at": _now().isoformat()})
        if error:
            state["last_error"] = str(error)[:500]
        elif alive:
            state["last_error"] = ""


def mark_evolution_component_disabled(component: str, *, reason: str) -> None:
    """Mark a component healthy-by-delegation when another process owns it."""

    if component not in {"worker", "scheduler"}:
        raise ValueError("unsupported evolution runtime component")
    with _runtime_lock:
        _runtime_status[component].update(
            {
                "started": False,
                "alive": False,
                "disabled": True,
                "disabled_reason": str(reason),
                "heartbeat_at": None,
                "last_error": "",
            }
        )


def evolution_runtime_status() -> dict[str, Any]:
    with _runtime_lock:
        items = {key: dict(value) for key, value in _runtime_status.items()}
    now = _now()
    for value in items.values():
        heartbeat = value.get("heartbeat_at")
        try:
            age = max(0.0, (now - datetime.fromisoformat(str(heartbeat))).total_seconds()) if heartbeat else None
        except (TypeError, ValueError):
            age = None
        value["heartbeat_age_seconds"] = age
        value["healthy"] = bool(value.get("disabled")) or (bool(value.get("alive")) and age is not None and age <= 900)
    return {"ok": all(item.get("healthy") for item in items.values()), "components": items}


def _is_scheduled_trade_day(value: datetime) -> bool:
    trade_date = value.date().strftime("%Y%m%d")
    try:
        with data_conn() as conn:
            row = conn.execute(
                "SELECT is_open FROM dc_trade_calendar WHERE exchange='SSE' AND cal_date=? LIMIT 1",
                (trade_date,),
            ).fetchone()
        if row is not None:
            return bool(int(row["is_open"] or 0))
    except Exception:
        pass
    return is_trade_day(value.date())


def _lease_owner_process_alive(owner: Any) -> bool | None:
    """Return local owner liveness; remote or legacy owner formats are unknown."""
    value = str(owner or "").strip()
    if not value:
        return False
    parts = value.rsplit(":", 2)
    if len(parts) != 3 or parts[0] != socket.gethostname():
        return None
    try:
        pid = int(parts[1])
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_task_execution_active(task_id: int) -> bool:
    with _active_task_lock:
        return int(task_id) in _active_task_ids


def _with_heartbeat_db_retry(operation: Callable[[], Any]) -> Any:
    """Retry only the small lease read/write boundary on transient DB failures."""
    for attempt in range(HEARTBEAT_DB_RETRY_ATTEMPTS):
        try:
            return operation()
        except EvolutionTaskCancelled:
            raise
        except Exception:
            if attempt + 1 >= HEARTBEAT_DB_RETRY_ATTEMPTS:
                raise
            time.sleep(HEARTBEAT_DB_RETRY_BACKOFF_SECONDS * (2 ** attempt))
    raise RuntimeError("heartbeat retry loop exhausted")


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except Exception:
        return fallback


def _row(row: Any) -> dict[str, Any]:
    item = dict(row)
    for source, target, fallback in (
        ("payload_json", "payload", {}),
        ("checkpoint_json", "checkpoint", {}),
        ("result_json", "result", {}),
    ):
        item[target] = _loads(item.pop(source, None), fallback)
    for key in ("id", "attempt", "max_attempts", "priority", "stop_requested"):
        item[key] = int(item.get(key) or 0)
    item["progress"] = float(item.get("progress") or 0.0)
    if item.get("started_at") and 0 < item["progress"] < 1:
        try:
            started = datetime.fromisoformat(str(item["started_at"]))
            elapsed = max(0.0, (_now() - started).total_seconds())
            item["estimated_remaining_seconds"] = int(elapsed * (1.0 - item["progress"]) / item["progress"])
        except Exception:
            item["estimated_remaining_seconds"] = None
    else:
        item["estimated_remaining_seconds"] = 0 if item["progress"] >= 1 else None
    return item


_TASK_LIST_COLUMNS = (
    "id,task_type,dedupe_key,status,phase,progress,message,error,attempt,max_attempts,priority,"
    "stop_requested,deadline_at,lease_owner,lease_expires_at,heartbeat_at,next_run_at,"
    "started_at,finished_at,created_at,updated_at"
)


def _get_evolution_task_summary(task_id: int) -> dict[str, Any] | None:
    """Read task metadata without transferring frozen JSON evidence."""
    with _conn() as conn:
        row = conn.execute(
            f"SELECT {_TASK_LIST_COLUMNS} FROM evolution_tasks WHERE id=?",
            (int(task_id),),
        ).fetchone()
    return _row(row) if row else None


def _task_payload(task: dict[str, Any]) -> dict[str, Any]:
    payload = task.get("payload")
    return payload if isinstance(payload, dict) else {}


def _superseded_task_reason(
    task: dict[str, Any],
    successor_edges: set[tuple[int, str]],
) -> str | None:
    """Return a reason for an active task that must never be executed.

    V10 research is event-driven. The former weekly generic strategy task and
    a predecessor branch that already produced a later research artifact are
    historical evidence, not work waiting for a worker.
    """
    task_type = str(task.get("task_type") or "")
    payload = _task_payload(task)
    trigger = str(payload.get("trigger") or "")
    if V10_RESEARCH_ENABLED and task_type == "strategy_evolution_cycle" and trigger == "weekly_scheduler":
        return "旧 weekly_scheduler 合同已由 V10 事件驱动研究替代，保留证据但禁止执行"
    if V10_RESEARCH_ENABLED and task_type == "strategy_candidate_generation":
        predecessor = int(payload.get("predecessor_task_id") or 0)
        if predecessor and any(edge[0] == predecessor for edge in successor_edges):
            return "同一研究前驱已有后继研究链，保留失败证据但禁止重复执行"
    return None


def reconcile_superseded_evolution_tasks(*, limit: int = 200) -> dict[str, Any]:
    """Atomically quarantine queued research branches superseded by V10.

    This is intentionally narrow: only queued/retry-wait tasks with a
    deterministic replacement are changed. Running tasks, including formal
    OOS with a live lease, are never touched.
    """
    now = _now().isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id,task_type,status,payload_json
               FROM evolution_tasks
               WHERE status IN ('queued','retry_wait')
               ORDER BY id ASC LIMIT ?""",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        all_rows = conn.execute(
            """SELECT id,task_type,status,payload_json
               FROM evolution_tasks
               WHERE status IN ('completed','running','queued','retry_wait')""",
        ).fetchall()
        successor_edges: set[tuple[int, str]] = set()
        successor_types = {
            "strategy_cluster_distillation",
            "strategy_portfolio_research",
            "strategy_portfolio_formal_queue",
            "strategy_evolution_cycle",
        }
        for row in all_rows:
            item = dict(row)
            payload = _loads(item.get("payload_json"), {})
            predecessor = int(payload.get("predecessor_task_id") or 0) if isinstance(payload, dict) else 0
            if predecessor and str(item.get("task_type") or "") in successor_types:
                successor_edges.add((predecessor, str(item.get("task_type") or "")))

        quarantined: list[dict[str, Any]] = []
        for row in rows:
            task = dict(row)
            task["payload"] = _loads(task.pop("payload_json", None), {})
            reason = _superseded_task_reason(task, successor_edges)
            if not reason:
                continue
            cursor = conn.execute(
                """UPDATE evolution_tasks
                   SET status='cancelled',phase='superseded',progress=1,
                       message=?,error=NULL,stop_requested=1,
                       finished_at=?,lease_owner=NULL,lease_expires_at=NULL,
                       heartbeat_at=?,updated_at=?
                   WHERE id=? AND status IN ('queued','retry_wait')""",
                (reason, now, now, now, int(task["id"])),
            )
            if int(cursor.rowcount or 0) > 0:
                quarantined.append({"id": int(task["id"]), "reason": reason})
    return {
        "ok": True,
        "schema_version": LEGACY_TASK_RECONCILIATION_VERSION,
        "quarantined": quarantined,
        "count": len(quarantined),
    }


def create_evolution_task(
    *,
    task_type: str = "strategy_evolution_cycle",
    payload: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    max_attempts: int = 3,
    priority: int = 100,
    force: bool = False,
    coalesce_active_type: bool = False,
) -> dict[str, Any]:
    now = _now().isoformat()
    payload = dict(payload or {})
    key = str(dedupe_key or f"{task_type}:{now[:10]}")
    if force:
        key = f"{key}:{uuid.uuid4().hex[:8]}"
    with _conn() as conn:
        if conn._is_postgres:
            lock_key = (
                f"active-task-type:{task_type}"
                if coalesce_active_type
                else key
            )
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (lock_key,))
        if coalesce_active_type:
            active = conn.execute(
                """SELECT id FROM evolution_tasks
                   WHERE task_type=?
                     AND status IN ('queued','running','retry_wait')
                   ORDER BY priority ASC,created_at ASC LIMIT 1""",
                (str(task_type),),
            ).fetchone()
            if active:
                return _get_evolution_task_summary(int(active["id"])) or {"id": int(active["id"])}
        existing = conn.execute(
            """SELECT id FROM evolution_tasks
               WHERE dedupe_key=?
               ORDER BY id DESC LIMIT 1""",
            (key,),
        ).fetchone()
        if existing:
            return _get_evolution_task_summary(int(existing["id"])) or {"id": int(existing["id"])}
        conn.execute(
            """INSERT INTO evolution_tasks(
                   task_type,dedupe_key,status,phase,progress,message,payload_json,
                   checkpoint_json,result_json,error,attempt,max_attempts,priority,
                   lease_owner,lease_expires_at,heartbeat_at,next_run_at,
                   started_at,finished_at,created_at,updated_at
               ) VALUES(?,?,'queued','queued',0,?,?,'{}','{}',NULL,0,?,?,NULL,NULL,NULL,?,NULL,NULL,?,?)""",
            (
                str(task_type), key, "任务已创建，等待后台执行", _json(payload),
                max(1, int(max_attempts)), int(priority), now, now, now,
            ),
        )
        task_id = conn.last_insert_id()
    return _get_evolution_task_summary(task_id) or {"id": task_id, "status": "queued"}


def enqueue_factor_strategy_research_event(
    *,
    trigger: str,
    evidence_type: str,
    evidence_id: str | int,
    evidence: dict[str, Any] | None = None,
    priority: int = 80,
) -> dict[str, Any]:
    """Wake the continuous loop only once for each new piece of research evidence."""
    identity = {
        "trigger": str(trigger),
        "evidence_type": str(evidence_type),
        "evidence_id": str(evidence_id),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    task_type = "strategy_candidate_generation" if V10_RESEARCH_ENABLED else "factor_research_cycle"
    return create_evolution_task(
        task_type=task_type,
        payload={
            **identity,
            "trigger": str(trigger),
            **({"batch_size": WORKER_RESEARCH_BATCH_SIZE} if V10_RESEARCH_ENABLED else {"candidates_per_task": 1}),
            "evidence": dict(evidence or {}),
        },
        dedupe_key=f"factor_strategy_research_event:v2:{task_type}:{digest}",
        priority=max(60, min(int(priority), 95)),
        coalesce_active_type=V10_RESEARCH_ENABLED,
    )


def get_evolution_task(task_id: int, *, compact: bool = False) -> dict[str, Any] | None:
    with _conn() as conn:
        if compact and getattr(conn, "_is_postgres", False):
            # Keep the task-center projection small while preserving the
            # step-level status needed by the management page.  Full result
            # evidence remains available from the task-detail endpoint.
            row = conn.execute(
                """SELECT id,task_type,dedupe_key,status,phase,progress,message,payload_json,checkpoint_json,
                          COALESCE((
                              SELECT jsonb_agg(jsonb_build_object(
                                  'name', step ->> 'name',
                                  'ok', COALESCE(NULLIF(step ->> 'ok', '')::boolean, false),
                                  'error', step ->> 'error',
                                  'started_at', step ->> 'started_at',
                                  'finished_at', step ->> 'finished_at',
                                  'duration_seconds', COALESCE(NULLIF(step ->> 'duration_seconds', '')::double precision, 0),
                                  'result', jsonb_build_object(
                                      'status', step -> 'result' ->> 'status',
                                      'reason', step -> 'result' ->> 'reason'
                                  ),
                                  'health', jsonb_build_object(
                                      'status', step -> 'health' ->> 'status',
                                      'reason', step -> 'health' ->> 'reason'
                                  )
                              ) ORDER BY step_order)
                              FROM jsonb_array_elements(
                                  COALESCE(result_json::jsonb -> 'steps', '[]'::jsonb)
                              ) WITH ORDINALITY AS step_data(step, step_order)
                          ), '[]'::jsonb)::text AS result_json,
                          error,attempt,max_attempts,priority,stop_requested,deadline_at,lease_owner,
                          lease_expires_at,heartbeat_at,next_run_at,started_at,finished_at,created_at,updated_at
                   FROM evolution_tasks WHERE id=?""",
                (int(task_id),),
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM evolution_tasks WHERE id=?", (int(task_id),)).fetchone()
    return _row(row) if row else None


def _read_evolution_task_for_worker(task_id: int) -> dict[str, Any] | None:
    """Keep a transient DB outage from escaping the worker loop."""
    try:
        return _with_heartbeat_db_retry(lambda: get_evolution_task(int(task_id)))
    except Exception as exc:  # noqa: BLE001 - lease recovery will retry later
        logger.warning("worker could not read evolution task id=%s: %s", task_id, exc)
        return None


def list_evolution_tasks(limit: int = 20, *, include_details: bool = False) -> list[dict[str, Any]]:
    # List/health views must not pull the two unbounded JSON documents.  The
    # task detail endpoint reads them on demand for a single task.
    columns = _TASK_LIST_COLUMNS
    if include_details:
        columns = columns + ",payload_json,checkpoint_json,result_json"
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT {columns} FROM evolution_tasks ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    return [_row(row) for row in rows]


def cancel_evolution_task(task_id: int) -> dict[str, Any] | None:
    task = get_evolution_task(int(task_id))
    if not task:
        return None
    if task["status"] in TERMINAL_STATES:
        return task
    now = _now().isoformat()
    # A cancellation is an operator safety boundary, not merely a hint.  The
    # conditional update also prevents a stale cancellation request from
    # overwriting a task that completed between the read above and this write.
    _update_active_task(
        int(task_id),
        statuses=ACTIVE_STATES,
        stop_requested=1,
        status="cancelled",
        phase="cancelled",
        progress=1.0,
        message="任务已取消",
        finished_at=now,
        lease_owner=None,
        lease_expires_at=None,
        heartbeat_at=now,
    )
    cancelled = get_evolution_task(int(task_id))
    if (
        cancelled
        and V10_RESEARCH_ENABLED
        and str(cancelled.get("task_type") or "") == "strategy_environment_backtest"
    ):
        candidate_id = str((cancelled.get("payload") or {}).get("candidate_id") or "")
        if candidate_id:
            from app.services.continuous_strategy_research import (
                cancel_candidate_research_event,
            )

            cancellation = cancel_candidate_research_event(candidate_id)
            if bool(cancellation.get("wave_ready_for_distillation")):
                _schedule_v10_research_continuation(
                    completed_task=cancelled,
                    result=cancellation,
                )
    return cancelled


def _next_weekday_at(now: datetime, weekday: int, hour: int) -> datetime:
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    days = (weekday - candidate.weekday()) % 7
    candidate += timedelta(days=days)
    if candidate <= now:
        candidate += timedelta(days=7)
    while not _is_scheduled_trade_day(candidate):
        candidate += timedelta(days=7)
    return candidate


def _next_business_day_at(now: datetime, hour: int) -> datetime:
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while not _is_scheduled_trade_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def evolution_schedule_status() -> dict[str, Any]:
    now = _now()
    return {
        "factor_strategy_operating_model": "continuous_research",
        "factor_strategy_operating_model_version": "alis_factor_strategy_continuous_research_v1",
        "auto_enabled": AUTO_ENABLED,
        "worker_id": WORKER_ID,
        "scheduler_tick_seconds": 300,
        "budgets": {
            "single_worker": True,
            "max_candidates_per_cycle": MAX_CANDIDATES_PER_CYCLE,
            "max_windows_per_candidate": MAX_WINDOWS_PER_CANDIDATE,
            "task_timeout_minutes": TASK_TIMEOUT_MINUTES,
            "hard_history_contract_sessions": 1537,
        },
        "factor_strategy_cadence_contract": {
            "runtime": {
                "cadence": "always_on_queue_worker",
                "poll_seconds": POLL_SECONDS,
                "requires_market_close": False,
            },
            "research": {
                "cadence": "continuous_budgeted_queue",
                "requires_market_close": False,
                "stops_on_same_snapshot_epoch_complete": True,
            },
            "formal_oos": {
                "cadence": "window_or_candidate_event",
                "requires_market_close": False,
            },
        },
        "next_factor_cycle_at": _next_business_day_at(now, 16).isoformat() if AUTO_ENABLED else None,
        "continuous_factor_research": {
            "enabled": AUTO_ENABLED,
            "cadence": "persistent_low_priority_continuation",
            "requires_market_close": False,
            "priority": 90,
        },
        "next_shadow_cycle_at": None,
        "shadow_cycle_trigger": "decision_data_ready",
        "next_system_cycle_at": _next_business_day_at(now, 17).isoformat() if AUTO_ENABLED else None,
        "next_formal_cycle_at": _next_weekday_at(now, AUTO_WEEKDAY, AUTO_HOUR).isoformat() if AUTO_ENABLED else None,
    }


def enqueue_system_autonomy_recovery(
    trade_date: str,
    data_contract: dict[str, Any],
    *,
    continuation_after: int | None = None,
    experiment_ready: bool = False,
) -> dict[str, Any]:
    """Create one same-date catch-up cycle for each newly ready data contract.

    The normal daily key may already belong to a cycle that safely stopped at
    the data gate.  A contract fingerprint makes recovery idempotent while
    still allowing a materially improved slice to trigger a fresh cycle.
    """
    if not trade_date or not bool(data_contract.get("ok")):
        return {"ok": False, "reason": "decision_data_not_ready"}
    fingerprint_payload = {
        "trade_date": trade_date,
        "counts": data_contract.get("counts") or {},
        "expected_rows": data_contract.get("expected_rows") or {},
        "coverage": data_contract.get("coverage") or {},
        "recovery_scope": (
            "decision_and_experiments" if experiment_ready else "production_decision"
        ),
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    continuation_suffix = (
        f":after-{int(continuation_after)}"
        if int(continuation_after or 0) > 0
        else ""
    )
    return create_evolution_task(
        task_type="system_autonomy_cycle",
        dedupe_key=(
            f"system_autonomy_recovery:{SYSTEM_AUTONOMY_RECOVERY_POLICY_VERSION}:"
            f"{trade_date}:{fingerprint}{continuation_suffix}"
        ),
        payload={
            "trigger": "decision_data_recovered",
            "trade_date": trade_date,
            "data_contract_fingerprint": fingerprint,
            "continuation_after_task_id": int(continuation_after or 0) or None,
            "recovery_scope": (
                "decision_and_experiments" if experiment_ready else "production_decision"
            ),
        },
        priority=85,
    )


def ensure_latest_ready_system_cycle() -> dict[str, Any]:
    """Recover a missed same-date system cycle, including after restarts/weekends."""
    from app.services.data_health import assess_latest_decision_data_health
    from app.services.profit_loop import (
        LEGACY_UNIFIED_DAILY_DECISION_SCHEMA_VERSION,
        UNIFIED_DAILY_DECISION_SCHEMA_VERSION,
    )

    health = assess_latest_decision_data_health()
    readiness = health.get("readiness") if isinstance(health.get("readiness"), dict) else {}
    trade_date = str(readiness.get("target_trade_date") or "")
    if not bool(health.get("decision_ready")):
        from app.data_platform.db import _conn as data_conn
        from app.data_platform.services.decision_data_contract import latest_decision_ready_trade_date

        with data_conn() as conn:
            recovered_trade_date = latest_decision_ready_trade_date(conn)
        if recovered_trade_date:
            health = assess_latest_decision_data_health(recovered_trade_date)
            readiness = health.get("readiness") if isinstance(health.get("readiness"), dict) else {}
            trade_date = str(readiness.get("target_trade_date") or recovered_trade_date)
    if not trade_date or not bool(health.get("decision_ready")):
        return {"ok": False, "reason": "decision_data_not_ready", "trade_date": trade_date}
    experiments_required = bool(health.get("experiment_ready"))
    required_recovery_scope = (
        "decision_and_experiments" if experiments_required else "production_decision"
    )
    tasks = [
            task for task in list_evolution_tasks(limit=200)
        if str(task.get("task_type") or "") == "system_autonomy_cycle"
    ]
    active = next(
        (
            task for task in tasks
            if str(task.get("status") or "") in {"queued", "running", "retry_wait"}
            and (
                str((task.get("payload") or {}).get("trade_date") or "").replace("-", "")[:8]
                == trade_date
                or f":{trade_date}" in str(task.get("dedupe_key") or "").replace("-", "")
            )
        ),
        None,
    )
    if active:
        return {
            "ok": True,
            "status": str(active.get("status") or "queued"),
            "trade_date": trade_date,
            "task_id": int(active.get("id") or 0),
        }
    latest_incomplete_task_id = 0
    continuation_count = 0
    for task in tasks:
        if str(task.get("status") or "") != "completed":
            continue
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        decision_ready_for_date = False
        experiment_ready_for_date = False
        hard_data_ready = False
        model_autonomy_completed = False
        strategy_shadow_completed = False
        plan_status = ""
        plan_is_unified_persisted = False
        for step in result.get("steps") or []:
            step_name = str(step.get("name") or "") if isinstance(step, dict) else ""
            if step_name != "decision_data_gate":
                if step_name == "hard_data_gate":
                    hard_result = step.get("result") if isinstance(step.get("result"), dict) else {}
                    hard_data_ready = bool(step.get("ok")) and str(hard_result.get("status") or "") == "ready"
                elif step_name == "plan_model_autonomy":
                    model_autonomy_completed = bool(step.get("ok"))
                elif step_name == "strategy_shadow_recovery":
                    strategy_shadow_completed = bool(step.get("ok"))
                elif step_name == "profit_and_portfolio":
                    profit = step.get("result") if isinstance(step.get("result"), dict) else {}
                    plan = profit.get("rebalance_plan") if isinstance(profit.get("rebalance_plan"), dict) else {}
                    plan_status = str(plan.get("status") or "")
                    plan_is_unified_persisted = bool(
                        plan.get("persisted", plan.get("id") is not None)
                        and str(plan.get("decision_contract_schema_version") or "")
                        in {
                            LEGACY_UNIFIED_DAILY_DECISION_SCHEMA_VERSION,
                            UNIFIED_DAILY_DECISION_SCHEMA_VERSION,
                        }
                    )
                continue
            gate = step.get("result") if isinstance(step.get("result"), dict) else {}
            gate_readiness = gate.get("readiness") if isinstance(gate.get("readiness"), dict) else {}
            decision_ready_for_date = bool(gate.get("decision_ready")) and str(gate_readiness.get("target_trade_date") or "") == trade_date
            experiment_ready_for_date = bool(gate.get("experiment_ready"))
        production_completed = (
            decision_ready_for_date
            and plan_is_unified_persisted
            and plan_status != "compatibility_preview_only"
        )
        experiments_completed = (
            experiment_ready_for_date
            and hard_data_ready
            and model_autonomy_completed
            and strategy_shadow_completed
        )
        if production_completed and (not experiments_required or experiments_completed):
            return {"ok": True, "status": "already_completed", "trade_date": trade_date, "task_id": task.get("id")}
        if decision_ready_for_date:
            latest_incomplete_task_id = max(
                latest_incomplete_task_id,
                int(task.get("id") or 0),
            )
            task_scope = str((task.get("payload") or {}).get("recovery_scope") or "production_decision")
            if (
                task_scope == required_recovery_scope
                and int((task.get("payload") or {}).get("continuation_after_task_id") or 0) > 0
            ):
                continuation_count += 1
    if continuation_count >= SYSTEM_AUTONOMY_MAX_RECOVERY_CONTINUATIONS:
        return {
            "ok": False,
            "status": "recovery_retry_exhausted",
            "trade_date": trade_date,
            "last_task_id": latest_incomplete_task_id or None,
            "maximum_continuations": SYSTEM_AUTONOMY_MAX_RECOVERY_CONTINUATIONS,
        }
    contract = readiness.get("decision_contract") if isinstance(readiness.get("decision_contract"), dict) else {}
    if not contract:
        from app.data_platform.services.decision_data_contract import decision_data_contract
        contract = decision_data_contract(trade_date)
    return enqueue_system_autonomy_recovery(
        trade_date,
        contract,
        continuation_after=latest_incomplete_task_id or None,
        experiment_ready=experiments_required,
    )


def _claim_next_task() -> dict[str, Any] | None:
    now = _now()
    lease_until = (now + timedelta(minutes=LEASE_MINUTES)).isoformat()
    with _conn() as conn:
        suffix = " FOR UPDATE SKIP LOCKED" if conn._is_postgres else ""
        row = conn.execute(
            f"""SELECT * FROM evolution_tasks
                WHERE status IN ('queued','retry_wait')
                  AND (next_run_at IS NULL OR next_run_at<=?)
                ORDER BY priority ASC,created_at ASC LIMIT 1{suffix}""",
            (now.isoformat(),),
        ).fetchone()
        if not row:
            return None
        task_id = int(row["id"])
        conn.execute(
            """UPDATE evolution_tasks
               SET status='running',phase='starting',message=?,attempt=attempt+1,
                   lease_owner=?,lease_expires_at=?,heartbeat_at=?,deadline_at=?,
                   started_at=COALESCE(started_at,?),updated_at=?
               WHERE id=? AND status IN ('queued','retry_wait')""",
            (
                "后台任务已领取", WORKER_ID, lease_until, now.isoformat(),
                (now + timedelta(minutes=TASK_TIMEOUT_MINUTES)).isoformat(),
                now.isoformat(), now.isoformat(), task_id,
            ),
        )
    return get_evolution_task(task_id)


def _update_task(task_id: int, **fields: Any) -> None:
    mapping = {
        "payload": "payload_json", "checkpoint": "checkpoint_json", "result": "result_json",
    }
    assignments: list[str] = []
    values: list[Any] = []
    for key, value in fields.items():
        column = mapping.get(key, key)
        assignments.append(f"{column}=?")
        values.append(_json(value) if key in mapping else value)
    assignments.append("updated_at=?")
    values.extend([_now().isoformat(), int(task_id)])
    with _conn() as conn:
        conn.execute(f"UPDATE evolution_tasks SET {','.join(assignments)} WHERE id=?", tuple(values))


def _update_active_task(
    task_id: int,
    *,
    statuses: tuple[str, ...] = ACTIVE_STATES,
    require_not_stopped: bool = False,
    expected_lease_owner: str | None = None,
    **fields: Any,
) -> bool:
    """Atomically update a non-terminal task without reviving a cancelled one."""
    if not fields or not statuses:
        return False
    mapping = {
        "payload": "payload_json", "checkpoint": "checkpoint_json", "result": "result_json",
    }
    assignments: list[str] = []
    values: list[Any] = []
    for key, value in fields.items():
        column = mapping.get(key, key)
        assignments.append(f"{column}=?")
        values.append(_json(value) if key in mapping else value)
    assignments.append("updated_at=?")
    values.append(_now().isoformat())
    placeholders = ",".join("?" for _ in statuses)
    predicate = f"id=? AND status IN ({placeholders})"
    values.extend([int(task_id), *statuses])
    if require_not_stopped:
        predicate += " AND stop_requested=0"
    if expected_lease_owner is not None:
        predicate += " AND lease_owner=?"
        values.append(expected_lease_owner)
    with _conn() as conn:
        cursor = conn.execute(
            f"UPDATE evolution_tasks SET {','.join(assignments)} WHERE {predicate}",
            tuple(values),
        )
        return int(cursor.rowcount or 0) > 0


def _heartbeat(task_id: int, *, phase: str, progress: float, message: str, checkpoint: dict[str, Any]) -> None:
    try:
        _with_heartbeat_db_retry(lambda: _ensure_task_active(task_id))
    except EvolutionTaskBudgetExceeded:
        # The validation callback may carry a just-completed window. Preserve
        # that business checkpoint before yielding the task at the budget edge.
        now = _now()
        _with_heartbeat_db_retry(
            lambda: _update_active_task(
                int(task_id),
                statuses=("running",),
                require_not_stopped=True,
                expected_lease_owner=WORKER_ID,
                phase=phase,
                progress=max(0.0, min(0.999, float(progress))),
                message=message,
                checkpoint=checkpoint,
                heartbeat_at=now.isoformat(),
                lease_expires_at=(now + timedelta(minutes=LEASE_MINUTES)).isoformat(),
            )
        )
        raise
    now = _now()
    _with_heartbeat_db_retry(
        lambda: _update_task(
            task_id,
            phase=phase,
            progress=max(0.0, min(0.999, float(progress))),
            message=message,
            checkpoint=checkpoint,
            heartbeat_at=now.isoformat(),
            lease_owner=WORKER_ID,
            lease_expires_at=(now + timedelta(minutes=LEASE_MINUTES)).isoformat(),
        )
    )


def _run_with_heartbeat_pulse(
    task_id: int,
    *,
    phase: str,
    message: str,
    runner: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Keep long in-process research work visible without faking sub-step progress."""
    stopped = threading.Event()
    started = time.monotonic()

    def pulse() -> None:
        while not stopped.wait(HEARTBEAT_PULSE_SECONDS):
            elapsed = max(1, int(time.monotonic() - started))
            try:
                _runtime_heartbeat("worker")
                _with_heartbeat_db_retry(lambda: _ensure_task_active(int(task_id)))
                now = _now()
                _with_heartbeat_db_retry(
                    lambda: _update_active_task(
                        int(task_id),
                        statuses=("running",),
                        require_not_stopped=True,
                        expected_lease_owner=WORKER_ID,
                        phase=phase,
                        message=(
                            f"{message}"
                            f" · 已运行 {elapsed} 秒"
                        ),
                        heartbeat_at=now.isoformat(),
                        lease_expires_at=(now + timedelta(minutes=LEASE_MINUTES)).isoformat(),
                    )
                )
            except EvolutionTaskCancelled:
                return
            except Exception as exc:
                logger.warning("V10 task heartbeat failed for %s: %s", task_id, exc)

    thread = threading.Thread(
        target=pulse,
        daemon=True,
        name=f"evolution-task-pulse-{task_id}",
    )
    thread.start()
    try:
        return runner()
    finally:
        stopped.set()
        thread.join(timeout=1)


def _wait_for_model_train_runs(
    task_id: int,
    run_ids: list[int],
    *,
    phase_prefix: str,
) -> list[dict[str, Any]]:
    from app.db.store import get_model_train_run

    completed_runs: list[dict[str, Any]] = []
    for run_id in run_ids:
        while True:
            run = get_model_train_run(int(run_id))
            if not run:
                raise RuntimeError(f"模型训练运行不存在：{run_id}")
            status = str(run.get("status") or "")
            progress = max(0.1, min(0.95, float(run.get("progress") or 0.0)))
            model_key = str((run.get("metrics") or {}).get("model_key") or "")
            _heartbeat(
                int(task_id),
                phase=f"{phase_prefix}_{str(run.get('phase') or 'running')}",
                progress=progress,
                message=(
                    f"单模型资源锁定中：{model_key}"
                    f" · {str(run.get('message') or status)}"
                ),
                checkpoint={
                    "model_train_run_id": int(run_id),
                    "model_train_status": status,
                },
            )
            if status in {"completed", "failed", "cancelled"}:
                completed_runs.append({
                    "run_id": int(run_id),
                    "status": status,
                    "model_version_id": run.get("model_version_id"),
                    "error": run.get("error"),
                })
                break
            time.sleep(2)
    return completed_runs


def _ensure_task_active(task_id: int) -> None:
    task = get_evolution_task(int(task_id))
    if not task:
        raise EvolutionTaskCancelled("任务记录不存在")
    if int(task.get("stop_requested") or 0):
        raise EvolutionTaskCancelled("任务已请求取消")
    if str(task.get("status") or "") != "running" or str(task.get("lease_owner") or "") != WORKER_ID:
        raise EvolutionTaskCancelled("任务租约已转移")
    deadline = task.get("deadline_at")
    if deadline:
        try:
            if _now() >= datetime.fromisoformat(str(deadline)):
                raise EvolutionTaskBudgetExceeded("任务超过资源预算时限")
        except ValueError:
            pass


def _experiment_data_gate() -> dict[str, Any]:
    """One current-data contract for every asynchronous research entry point."""
    from app.services.data_health import assess_latest_decision_data_health

    return assess_latest_decision_data_health()


def _blocked_by_decision_data(task: dict[str, Any], gate: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "schema_version": "alis_evolution_data_gate_v1",
        "status": "blocked_by_decision_data",
        "task_type": str(task.get("task_type") or ""),
        "reason": "latest_decision_data_not_safe_for_research",
        "data_gate": gate,
        "policy": "no_factor_evaluation_shadow_or_formal_oos_until_experiment_ready",
    }


def _latest_challenger_failure_attribution() -> dict[str, Any]:
    """Return only persisted formal-OOS evidence suitable for candidate routing."""
    for version in list_model_versions("alis_strategy_challenger", limit=20):
        formal = (version.get("metrics") or {}).get("formal_oos")
        attribution = formal.get("failure_attribution") if isinstance(formal, dict) else None
        if not isinstance(attribution, dict):
            continue
        primary_cause = str(attribution.get("primary_cause") or "unknown")
        confidence = str(attribution.get("confidence") or "low")
        if primary_cause != "unknown" and confidence in {"medium", "high"}:
            routed = deepcopy(attribution)
            routed["_routing_source"] = {
                "model_version_id": int(version["id"]),
                "strategy_id": int((version.get("config") or {}).get("strategy_id") or 0),
                "model_version": str(version.get("version") or ""),
            }
            return routed
    return {}


def reconcile_failed_controller_oos_candidates(*, limit: int = 50) -> dict[str, Any]:
    """Close stale controller candidates that a pre-fix worker left at the shadow gate."""
    reconciled: list[int] = []
    for version in list_model_versions("strategy_controller", limit=max(1, min(int(limit), 200))):
        validation = version.get("validation") if isinstance(version.get("validation"), dict) else {}
        formal = ((version.get("metrics") or {}).get("formal_oos") or {})
        formal = formal if isinstance(formal, dict) else {}
        failed = (
            str(validation.get("status") or "") == "formal_oos_failed"
            or str(formal.get("status") or "") == "failed"
        )
        if not failed:
            continue
        if (
            str(version.get("status") or "") == "rejected"
            and str(validation.get("next_gate") or "") == "formal_oos_rejected"
        ):
            continue
        next_validation = dict(validation)
        next_validation.update({
            "status": "formal_oos_failed",
            "production_eligible": False,
            "next_gate": "formal_oos_rejected",
            "blocking_reasons": list(validation.get("blocking_reasons") or formal.get("blocking_reasons") or []),
        })
        update_model_version_validation(int(version["id"]), validation=next_validation)
        update_model_version_status(int(version["id"]), status="rejected", is_active=False)
        reconciled.append(int(version["id"]))
    return {"schema_version": "strategy_controller_formal_oos_reconciliation_v1", "reconciled_version_ids": reconciled}


def _strategy_targets() -> list[dict[str, Any]]:
    reconcile_failed_controller_oos_candidates()
    champion = resolve_production_strategy_champion()
    challenger_versions = list_model_versions("alis_strategy_challenger", limit=100)
    attribution = _latest_challenger_failure_attribution()
    generated: dict[str, Any] | None = None
    if attribution.get("primary_cause") == "market_exposure":
        from app.services.strategy_candidate_generator import ensure_dynamic_exposure_challenger

        generated = ensure_dynamic_exposure_challenger()
    elif attribution.get("primary_cause") == "selection_or_allocation_residual":
        from app.services.strategy_candidate_generator import ensure_selection_allocation_challenger

        generated = ensure_selection_allocation_challenger(
            failure_attribution=attribution,
            source_model_version_id=int(((attribution.get("_routing_source") or {}).get("model_version_id") or 0)),
        )
    elif attribution.get("primary_cause") == "unknown":
        from app.services.strategy_candidate_generator import ensure_diversified_selection_challenger

        generated = ensure_diversified_selection_challenger(
            source_model_version_id=int(((attribution.get("_routing_source") or {}).get("model_version_id") or 0)),
        )
    from app.services.strategy_candidate_generator import ensure_autonomous_strategy_candidates

    lineage_cycle = ensure_autonomous_strategy_candidates()
    lineage_generated = lineage_cycle.get("generated") if isinstance(lineage_cycle.get("generated"), list) else []
    challenger_versions = list_model_versions("alis_strategy_challenger", limit=500)
    targets = [{
        "role": "champion",
        "strategy_id": int(champion["strategy_id"]),
        "model_version_id": int(champion["champion_id"]),
        "name": str((champion.get("strategy") or {}).get("name") or "Champion"),
    }]
    generated_names = {
        int(item["strategy_id"]): str(item["name"])
        for item in ([generated] if generated else []) + lineage_generated
        if int(item.get("strategy_id") or 0)
    }
    eligible_statuses = {
        "pending_oos", "quick_diagnostic_passed", "formal_oos_blocked_by_reference",
        "formal_oos_blocked_by_holdout_data",
    }
    current_oos_end = str((audit_oos_hard_data(include_dates=False) or {}).get("window_end") or "")
    pending_versions = []
    for version in challenger_versions:
        config = version.get("config") if isinstance(version.get("config"), dict) else {}
        validation = version.get("validation") if isinstance(version.get("validation"), dict) else {}
        validation_status = str(validation.get("status") or "")
        statistics_refresh_ready = (
            validation_status == "formal_oos_blocked_by_statistics"
            and current_oos_end > str(validation.get("formal_statistics_blocked_after") or "")
        )
        holdout_refresh_ready = (
            validation_status == "formal_oos_blocked_by_holdout"
            and current_oos_end > str(validation.get("holdout_blocked_after") or "")
        )
        if validation_status not in eligible_statuses and not statistics_refresh_ready and not holdout_refresh_ready:
            continue
        pending_versions.append((version, config, validation_status))
    pending_versions.sort(
        key=lambda item: (
            str(item[2]) != "pending_oos",
            -int(item[0].get("id") or 0),
        )
    )
    for version, config, validation_status in pending_versions:
        strategy_id = int(config.get("strategy_id") or 0)
        if strategy_id and all(item["strategy_id"] != strategy_id for item in targets):
            targets.append({
                "role": "challenger",
                "strategy_id": strategy_id,
                "model_version_id": int(version["id"]),
                "name": generated_names.get(strategy_id, str(version.get("version") or "challenger")),
                "generation": str(config.get("generation") or ""),
                "validation_status": validation_status,
                "factor_bindings": (
                    config.get("factor_bindings")
                    if isinstance(config.get("factor_bindings"), dict) else None
                ),
                "model_bindings": (
                    config.get("model_bindings")
                    if isinstance(config.get("model_bindings"), dict) else None
                ),
                "market_state_binding": config.get("market_state_binding") or {},
                "controller_version_id": config.get("strategy_controller_version_id"),
                "research_package_hash": config.get("research_package_hash"),
            })
    for item in targets:
        if generated and int(item["strategy_id"]) == int(generated["strategy_id"]):
            item["name"] = generated["name"]
    return targets


def _strategy_cycle_factor_contract_issues(
    targets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Find challenger packages that cannot be replayed from frozen inputs."""

    champion = resolve_production_strategy_champion()
    champion_model_bindings = (
        champion.get("model_bindings")
        if isinstance(champion.get("model_bindings"), dict)
        else None
    )
    issues: list[dict[str, Any]] = []
    for target in targets:
        if str(target.get("role") or "") != "challenger":
            continue
        strategy_id = int(target.get("strategy_id") or 0)
        strategy = get_strategy(strategy_id) if strategy_id else None
        if not strategy:
            issues.append({
                "strategy_id": strategy_id or None,
                "model_version_id": int(target.get("model_version_id") or 0) or None,
                "missing_factor_bindings": [],
                "reason": "strategy_not_found",
            })
            continue
        model_bindings = (
            target.get("model_bindings")
            if isinstance(target.get("model_bindings"), dict)
            else champion_model_bindings
        )
        required_factors = get_replay_required_factor_names(
            strategy.get("content"),
            str(strategy.get("type") or "formula"),
            model_bindings=model_bindings,
        )
        frozen_bindings = (
            target.get("factor_bindings")
            if isinstance(target.get("factor_bindings"), dict)
            else {}
        )
        missing = sorted(
            factor for factor in required_factors
            if not isinstance(frozen_bindings.get(factor), dict)
        )
        if missing:
            issues.append({
                "strategy_id": strategy_id or None,
                "model_version_id": int(target.get("model_version_id") or 0) or None,
                "required_factors": sorted(required_factors),
                "missing_factor_bindings": missing,
                "reason": "incomplete_frozen_factor_bindings",
            })
    return issues


def _record_factor_contract_block(issues: list[dict[str, Any]]) -> None:
    """Quarantine invalid candidates without changing production permissions."""

    for issue in issues:
        version_id = int(issue.get("model_version_id") or 0)
        if not version_id:
            continue
        version = get_model_version(version_id)
        if not version:
            continue
        validation = dict(version.get("validation") or {})
        reasons = list(validation.get("blocking_reasons") or [])
        reason = {
            "code": "blocked_incomplete_factor_bindings",
            "required_factors": issue.get("required_factors") or [],
            "missing_factor_bindings": issue.get("missing_factor_bindings") or [],
        }
        validation.update({
            "status": "blocked_incomplete_factor_bindings",
            "production_eligible": False,
            "next_gate": "candidate_rebuild",
            "blocking_reasons": [*reasons, reason],
        })
        update_model_version_validation(version_id, validation=validation)


def _finish_contract_block(task: dict[str, Any], result: dict[str, Any]) -> None:
    """Persist a deterministic contract block as terminal, without retrying."""

    task_id = int(task["id"])
    message = "冻结策略合同不完整，任务已阻断且不会自动重试"
    _update_active_task(
        task_id,
        statuses=("running",),
        require_not_stopped=True,
        expected_lease_owner=WORKER_ID,
        status="failed",
        phase="blocked_contract",
        progress=1.0,
        message=message,
        result=result,
        error=message,
        lease_owner=None,
        lease_expires_at=None,
        heartbeat_at=_now().isoformat(),
        finished_at=_now().isoformat(),
    )


def _formal_trial_cohort(
    candidate: dict[str, Any],
    versions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Count only trials exposed to the same selection process.

    Unrelated strategy families must not make every future experiment
    progressively impossible, while siblings from the same search lineage still
    receive the full multiple-testing penalty.
    """
    config = candidate.get("config") if isinstance(candidate.get("config"), dict) else {}
    lineage = config.get("lineage") if isinstance(config.get("lineage"), dict) else {}
    identity = {
        "champion_reference": str(config.get("champion_reference") or ""),
        "root_model_version_id": int(lineage.get("root_model_version_id") or 0),
        "hypothesis_family": str(
            lineage.get("hypothesis_family")
            or config.get("generation")
            or "unclassified"
        ),
    }
    matching: list[int] = []
    for version in versions:
        metrics = version.get("metrics") if isinstance(version.get("metrics"), dict) else {}
        if not isinstance(metrics.get("formal_oos"), dict):
            continue
        other_config = version.get("config") if isinstance(version.get("config"), dict) else {}
        other_lineage = (
            other_config.get("lineage")
            if isinstance(other_config.get("lineage"), dict)
            else {}
        )
        other_identity = {
            "champion_reference": str(other_config.get("champion_reference") or ""),
            "root_model_version_id": int(other_lineage.get("root_model_version_id") or 0),
            "hypothesis_family": str(
                other_lineage.get("hypothesis_family")
                or other_config.get("generation")
                or "unclassified"
            ),
        }
        if other_identity == identity:
            matching.append(int(version.get("id") or 0))
    return {
        "schema_version": "alis_formal_trial_cohort_v1",
        "identity": identity,
        "trial_count": max(1, len(matching)),
        "model_version_ids": matching,
    }


def _opportunity_cost_baseline_targets() -> list[dict[str, Any]]:
    """Resolve registered baselines by stable key rather than database ID."""
    resolved: dict[str, dict[str, Any]] = {}
    for version in list_model_versions("alis_strategy_baseline", limit=100):
        config = version.get("config") if isinstance(version.get("config"), dict) else {}
        baseline_key = str(config.get("baseline_key") or "")
        strategy_id = int(config.get("strategy_id") or 0)
        if baseline_key not in REQUIRED_OPPORTUNITY_COST_BASELINES or strategy_id <= 0:
            continue
        resolved.setdefault(baseline_key, {
            "role": "baseline",
            "baseline_key": baseline_key,
            "strategy_id": strategy_id,
            "model_version_id": int(version.get("id") or 0),
            "name": str(version.get("version") or baseline_key),
        })
    return [resolved[key] for key in REQUIRED_OPPORTUNITY_COST_BASELINES if key in resolved]


def create_strategy_controller_oos_task(
    strategy_controller_version_id: int, *, max_windows: int = 5, force: bool = False,
) -> dict[str, Any]:
    """Queue formal OOS for one controller candidate without rebinding production."""
    version = get_model_version(int(strategy_controller_version_id))
    if not version or version.get("model_key") != "strategy_controller" or version.get("status") != "candidate":
        raise ValueError("strategy_controller_version_id 不存在或不是待验证候选")
    champion = resolve_production_strategy_champion()
    target = {
        "role": "strategy_controller_candidate",
        "strategy_id": int(champion["strategy_id"]),
        "model_version_id": int(version["id"]),
        "controller_version_id": int(version["id"]),
        "name": str(version.get("version") or f"策略控制器候选 {version['id']}"),
    }
    return create_evolution_task(
        task_type="strategy_controller_oos",
        payload={"targets": [target], "max_windows": max(1, min(int(max_windows), MAX_WINDOWS_PER_CANDIDATE)), "trigger": "api"},
        dedupe_key=f"strategy_controller_oos:{version['id']}:{champion['champion_id']}",
        priority=50,
        force=force,
    )


def _run_quick_gate(target: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    dates = BacktestDataSource().get_available_trade_dates(
        str(audit["window_start"]), str(audit["window_end"]),
    )
    quick_dates = dates[-40:]
    if len(quick_dates) < 20:
        raise RuntimeError("快速诊断交易日不足")
    replay = run_strategy_replay(
        strategy_id=int(target["strategy_id"]),
        start_date=quick_dates[0],
        end_date=quick_dates[-1],
        factor_bindings=target.get("factor_bindings"),
        model_bindings=target.get("model_bindings"),
        market_state_binding=target.get("market_state_binding"),
        strategy_controller_version_id=target.get("controller_version_id"),
        research_package_hash=target.get("research_package_hash"),
    )
    metrics = replay.get("metrics") or {}
    candidate_return = float(metrics.get("total_return") or 0.0)
    candidate_excess = float(metrics.get("excess_return") or 0.0)
    candidate_drawdown = float(metrics.get("max_drawdown") or 0.0)
    gates = {
        "not_worse_than_v1_return": candidate_return >= -0.062034,
        "not_worse_than_v1_drawdown": candidate_drawdown >= -0.092114,
        "not_worse_than_v1_excess": candidate_excess >= -0.040355,
        "point_in_time_passed": bool(((replay.get("data_quality") or {}).get("point_in_time") or {}).get("passed")),
    }
    result = {
        "status": "passed" if all(gates.values()) else "failed",
        "range": [quick_dates[0], quick_dates[-1]],
        "metrics": metrics,
        "summary": replay.get("summary") or {},
        "gates": gates,
        "blocking_reasons": [key for key, passed in gates.items() if not passed],
    }
    update_model_version_metrics(int(target["model_version_id"]), metrics={"quick_diagnostic": result})
    if result["status"] == "failed":
        update_model_version_validation(int(target["model_version_id"]), validation={
            "status": "quick_diagnostic_failed",
            "production_eligible": False,
            "blocking_reasons": result["blocking_reasons"],
            "next_gate": "automatic_candidate_regeneration",
        })
    else:
        update_model_version_validation(int(target["model_version_id"]), validation={
            "status": "quick_diagnostic_passed",
            "production_eligible": False,
            "passed_gates": list(gates),
            "next_gate": "formal_oos",
        })
    return result


def _formal_metrics(result: dict[str, Any]) -> dict[str, Any]:
    summary = result.get("summary") or {}
    windows = result.get("windows") or []
    return {
        "status": result.get("status"),
        "valid_windows": int(summary.get("valid_window_count") or 0),
        "outperform_windows": int(summary.get("outperform_window_count") or 0),
        "outperform_ratio": summary.get("outperform_ratio"),
        "aggregate_cost_after_return": summary.get("aggregate_cost_after_return"),
        "aggregate_benchmark_return": summary.get("aggregate_benchmark_return"),
        "aggregate_excess_return": summary.get("aggregate_excess_return"),
        "annualized_relative_excess_return": summary.get("annualized_relative_excess_return"),
        "minimum_annualized_relative_excess_return": summary.get("minimum_annualized_relative_excess_return"),
        "effective_test_sessions": summary.get("effective_test_sessions"),
        "worst_window_drawdown": summary.get("worst_window_drawdown"),
        "total_fees": summary.get("total_fees"),
        "failure_attribution": result.get("failure_attribution") or {},
        "statistics": result.get("statistics") or {},
        "window_returns": [float((window.get("metrics") or {}).get("total_return") or 0.0) for window in windows],
    }


def _formal_reference_contract(
    result: dict[str, Any],
    *,
    task_id: int,
    window_start: str,
    window_end: str,
) -> dict[str, Any]:
    windows = result.get("windows") if isinstance(result.get("windows"), list) else []
    return {
        "schema_version": "alis_formal_oos_reference_v1",
        "cycle_task_id": int(task_id),
        "hard_data_window": {"start": str(window_start), "end": str(window_end)},
        "validation_schema_version": str(result.get("schema_version") or ""),
        "protocol": result.get("protocol") if isinstance(result.get("protocol"), dict) else {},
        "production_strategy_version_id": int(result.get("production_strategy_version_id") or 0),
        "production_strategy_version": str(result.get("production_strategy_version") or ""),
        "strategy_controller_version_id": int(
            result.get("strategy_controller_version_id") or 0
        ),
        "strategy_controller_version": str(result.get("strategy_controller_version") or ""),
        "test_windows": [
            {
                "index": int(window.get("index") or 0),
                "start": str(((window.get("test") or {}).get("start") or "")),
                "end": str(((window.get("test") or {}).get("end") or "")),
                "sessions": int(((window.get("test") or {}).get("sessions") or 0)),
            }
            for window in windows
            if isinstance(window, dict)
        ],
    }


def _persist_candidate_result(target: dict[str, Any], result: dict[str, Any]) -> None:
    if target["role"] == "champion":
        return
    version = get_model_version(int(target["model_version_id"])) or {}
    metrics = dict(version.get("metrics") or {})
    metrics["formal_oos"] = _formal_metrics(result)
    update_model_version_metrics(target["model_version_id"], metrics=metrics)
    validation = dict(version.get("validation") or {})
    is_controller_candidate = target["role"] == "strategy_controller_candidate"
    controller_shadow_ready = (
        is_controller_candidate
        and str(metrics.get("evaluation_scope") or "") == "candidate_specific_shadow_20d"
        and int(metrics.get("shadow_sessions") or 0) >= 20
        and float(metrics.get("net_lift") or 0.0) > 0.0
        and float(metrics.get("walk_forward_lift") or 0.0) > 0.0
        and float(metrics.get("worst_window_net_lift") or 0.0) > 0.0
    )
    promotion_gates = result.get("promotion_gates") or {}
    formal_oos_ready = str(result.get("status") or "") == "passed" and bool(promotion_gates) and all(
        bool(value) for value in promotion_gates.values()
    )
    # Controller candidates must enter the same portfolio-level future shadow
    # and cooling lifecycle as selection challengers. Their candidate-specific
    # decision proxy is insufficient evidence for a production rebind.
    automatically_promoted = False
    next_gate = "activated_and_champion_rebound" if automatically_promoted else "automatic_evolution_diagnosis"
    if is_controller_candidate:
        next_gate = "portfolio_shadow_and_statistics_gate" if formal_oos_ready else "formal_oos_rejected"
    validation.update({
        "status": "passed" if automatically_promoted else f"formal_oos_{result.get('status')}",
        "production_eligible": automatically_promoted,
        "passed_gates": [key for key, value in (result.get("promotion_gates") or {}).items() if value],
        "blocking_reasons": result.get("blocking_reasons") or [],
        "comparison_role": "strategy_controller_candidate" if is_controller_candidate else ("diagnostic_baseline_not_promotable" if target["role"] == "baseline" else "challenger"),
        "next_gate": next_gate,
    })
    if str(result.get("status") or "") == "failed":
        from app.services.experiment_memory import record_model_experiment_outcome

        record_model_experiment_outcome(
            version,
            stage="formal_oos",
            rejected=True,
            evidence={"formal_oos": _formal_metrics(result), "blocking_reasons": result.get("blocking_reasons") or []},
        )
    if is_controller_candidate:
        validation["blocking_reasons"] = list(dict.fromkeys(
            list(validation.get("blocking_reasons") or [])
            + (["portfolio_shadow_and_statistics_gate"] if formal_oos_ready else [])
        ))
    update_model_version_validation(target["model_version_id"], validation=validation)
    if is_controller_candidate and not formal_oos_ready:
        update_model_version_status(int(target["model_version_id"]), status="rejected", is_active=False)
    if automatically_promoted:
        from app.db.store import set_active_model_version
        from app.services.production_strategy import ensure_production_strategy_champion

        set_active_model_version(int(target["model_version_id"]))
        ensure_production_strategy_champion(force_rebind=True)


def _execute_strategy_cycle(
    task: dict[str, Any],
    *,
    validation_runner: Callable[..., dict[str, Any]] = run_purged_walk_forward_validation,
) -> dict[str, Any]:
    audit = audit_oos_hard_data(include_dates=False)
    if audit.get("status") != "ready":
        raise RuntimeError("OOS 永久硬数据未就绪")
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    checkpoint = task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {}
    checkpoint_targets = (
        checkpoint.get("targets") if isinstance(checkpoint.get("targets"), list) else None
    )
    targets = (
        checkpoint_targets
        if checkpoint_targets is not None
        else payload.get("targets")
        if isinstance(payload.get("targets"), list)
        else _strategy_targets()
    )
    if any(item.get("role") == "challenger" for item in targets) and not any(item.get("role") == "champion" for item in targets):
        champion = resolve_production_strategy_champion()
        targets = [{
            "role": "champion",
            "strategy_id": int(champion["strategy_id"]),
            "model_version_id": int(champion["champion_id"]),
            "name": str((champion.get("strategy") or {}).get("name") or "Champion"),
        }, *targets]
    if any(item.get("role") == "challenger" for item in targets):
        existing_keys = {str(item.get("baseline_key") or "") for item in targets if item.get("role") == "baseline"}
        targets.extend(
            item for item in _opportunity_cost_baseline_targets()
            if str(item.get("baseline_key") or "") not in existing_keys
        )
    max_candidates = max(1, min(int(payload.get("max_candidates") or MAX_CANDIDATES_PER_CYCLE), MAX_CANDIDATES_PER_CYCLE))
    champion_targets = [item for item in targets if item.get("role") == "champion"][:1]
    baseline_targets = [item for item in targets if item.get("role") == "baseline"]
    research_targets = [item for item in targets if item.get("role") not in {"champion", "baseline"}][:max_candidates]
    targets = champion_targets + baseline_targets + research_targets
    contract_issues = _strategy_cycle_factor_contract_issues(targets)
    if contract_issues:
        _record_factor_contract_block(contract_issues)
        return {
            "schema_version": "alis_replay_factor_contract_gate_v1",
            "status": "blocked_incomplete_factor_bindings",
            "production_change_forbidden": True,
            "contract_issues": contract_issues,
        }
    max_windows = max(1, min(int(payload.get("max_windows") or MAX_WINDOWS_PER_CANDIDATE), MAX_WINDOWS_PER_CANDIDATE))
    completed = checkpoint.get("completed") if isinstance(checkpoint.get("completed"), list) else []
    frozen_window = (
        checkpoint.get("hard_data_window")
        if isinstance(checkpoint.get("hard_data_window"), dict)
        else payload.get("hard_data_window")
        if isinstance(payload.get("hard_data_window"), dict)
        else {}
    )
    window_start = str(frozen_window.get("start") or audit["window_start"])
    window_end = str(frozen_window.get("end") or audit["window_end"])
    resume_state = (
        checkpoint.get("resource_budget_resume")
        if isinstance(checkpoint.get("resource_budget_resume"), dict)
        else None
    )

    def target_identity(item: dict[str, Any]) -> str:
        controller_id = int(item.get("controller_version_id") or 0)
        return f"controller:{controller_id}" if controller_id else f"strategy:{int(item.get('strategy_id') or 0)}"

    def checkpoint_state(
        *,
        current_target: dict[str, Any],
    ) -> dict[str, Any]:
        state = {
            "schema_version": "alis_formal_oos_checkpoint_v2",
            "targets": targets,
            "completed": completed,
            "current": current_target,
            "hard_data_window": {"start": window_start, "end": window_end},
        }
        if resume_state:
            state["resource_budget_resume"] = resume_state
        return state

    completed_ids = {target_identity(item) for item in completed}
    current = checkpoint.get("current") if isinstance(checkpoint.get("current"), dict) else {}
    total_units = max(1, len(targets) * max_windows)

    for target_index, target in enumerate(targets):
        _ensure_task_active(int(task["id"]))
        strategy_id = int(target["strategy_id"])
        identity = target_identity(target)
        if identity in completed_ids:
            continue
        if (
            target.get("role") == "challenger"
            and str(target.get("generation") or "").startswith("automatic_")
            and target.get("validation_status") == "pending_oos"
        ):
            _heartbeat(
                int(task["id"]), phase="quick_diagnostic",
                progress=(target_index * max_windows) / total_units,
                message=f"{target.get('name')}：运行快速淘汰门",
                checkpoint=checkpoint_state(current_target={}),
            )
            quick = _run_quick_gate(
                target,
                {**audit, "window_start": window_start, "window_end": window_end},
            )
            if quick["status"] != "passed":
                completed.append({
                    "strategy_id": strategy_id,
                    "model_version_id": int(target.get("model_version_id") or 0),
                    "role": target.get("role"),
                    "name": target.get("name"),
                    "status": "quick_diagnostic_failed",
                    "summary": quick.get("metrics") or {},
                    "blocking_reasons": quick.get("blocking_reasons") or [],
                })
                completed_ids.add(identity)
                _heartbeat(
                    int(task["id"]), phase="quick_diagnostic",
                    progress=(target_index + 1) / max(1, len(targets)),
                    message=f"{target.get('name')} 快速诊断未通过，已自动淘汰",
                    checkpoint=checkpoint_state(current_target={}),
                )
                continue
        prior_windows = (
            current.get("windows") if (
                str(current.get("target_identity") or "") == identity
                or (not current.get("target_identity") and int(current.get("strategy_id") or 0) == strategy_id)
            )
            and isinstance(current.get("windows"), list) else []
        )

        def progress(event: dict[str, Any]) -> None:
            windows = event.get("checkpoint_results") or []
            overall_units = target_index * max_windows + len(windows)
            next_checkpoint = checkpoint_state(current_target={
                "target_identity": identity,
                "strategy_id": strategy_id,
                "controller_version_id": target.get("controller_version_id"),
                "windows": windows,
            })
            _heartbeat(
                int(task["id"]),
                phase="formal_oos",
                progress=overall_units / total_units,
                message=f"{target.get('name')}：{event.get('message')}",
                checkpoint=next_checkpoint,
            )

        result = validation_runner(
            strategy_id=strategy_id,
            start_date=window_start,
            end_date=window_end,
            max_windows=max_windows,
            strategy_controller_version_id=target.get("controller_version_id"),
            factor_bindings=target.get("factor_bindings"),
            model_bindings=target.get("model_bindings"),
            market_state_binding=target.get("market_state_binding"),
            research_package_hash=target.get("research_package_hash"),
            execute=True,
            progress_callback=progress,
            checkpoint_results=prior_windows,
        )
        if target.get("role") == "challenger":
            challenger_versions = list_model_versions(
                "alis_strategy_challenger", limit=500
            )
            candidate_version = get_model_version(
                int(target.get("model_version_id") or 0)
            ) or {}
            formal_cohort = _formal_trial_cohort(
                candidate_version,
                challenger_versions,
            )
            statistics = dict(result.get("statistics") or {})
            statistics["multiple_testing"] = deflated_sharpe_ratio(
                result.get("windows") if isinstance(result.get("windows"), list) else [],
                trial_count=int(formal_cohort["trial_count"]),
            )
            statistics["formal_trial_cohort"] = formal_cohort
            result["statistics"] = statistics
        _persist_candidate_result(target, result)
        reference_contract = _formal_reference_contract(
            result,
            task_id=int(task["id"]),
            window_start=window_start,
            window_end=window_end,
        )
        completed.append({
            "strategy_id": strategy_id,
            "model_version_id": int(target.get("model_version_id") or 0),
            "controller_version_id": target.get("controller_version_id"),
            "role": target.get("role"),
            "baseline_key": target.get("baseline_key"),
            "name": target.get("name"),
            "status": result.get("status"),
            "summary": result.get("summary"),
            "statistics": result.get("statistics") or {},
            "reference_contract": reference_contract,
            "failure_attribution": result.get("failure_attribution") or {},
            "blocking_reasons": result.get("blocking_reasons"),
        })
        completed_ids.add(identity)
        current = {}
        _heartbeat(
            int(task["id"]),
            phase="formal_oos",
            progress=len(completed) / max(1, len(targets)),
            message=f"{target.get('name')} 正式验证完成",
            checkpoint=checkpoint_state(current_target={}),
        )
    lifecycle_targets = [item for item in completed if item.get("role") != "strategy_controller_candidate"]
    lifecycle = advance_formal_candidate_lifecycle(
        lifecycle_targets, shadow_start_date=window_end
    ) if lifecycle_targets else []
    lifecycle_by_id = {int(item.get("model_version_id") or 0): item for item in lifecycle}
    for item in completed:
        lifecycle_item = lifecycle_by_id.get(int(item.get("model_version_id") or 0))
        if lifecycle_item and isinstance(lifecycle_item.get("sealed_holdout"), dict):
            item["sealed_holdout"] = lifecycle_item["sealed_holdout"]
    return {
        "schema_version": "alis_evolution_cycle_v1",
        "status": "completed",
        "hard_data_window": {"start": window_start, "end": window_end},
        "strategies": completed,
        "diagnosis": diagnose_strategy_results(completed),
        "lifecycle": lifecycle,
    }


def diagnose_strategy_results(items: list[dict[str, Any]]) -> dict[str, Any]:
    baseline_items = [item for item in items if item.get("role") == "baseline"]
    challengers = [item for item in items if item.get("role") == "challenger"]
    best_baseline = max(
        baseline_items,
        key=lambda item: float((item.get("summary") or {}).get("aggregate_cost_after_return") or -999),
        default={},
    )
    baseline_return = float((best_baseline.get("summary") or {}).get("aggregate_cost_after_return") or 0.0)
    confidence_score = {"high": 3, "medium": 2, "low": 1}
    diagnoses: list[dict[str, Any]] = []
    for challenger in challengers:
        summary = challenger.get("summary") if isinstance(challenger.get("summary"), dict) else {}
        attribution = challenger.get("failure_attribution") if isinstance(challenger.get("failure_attribution"), dict) else {}
        challenger_return = float(summary.get("aggregate_cost_after_return") or 0.0)
        benchmark_return = float(summary.get("aggregate_benchmark_return") or 0.0)
        primary_cause = str(attribution.get("primary_cause") or "unknown")
        confidence = str(attribution.get("confidence") or "low")
        diagnoses.append({
            "model_version_id": int(challenger.get("model_version_id") or 0),
            "strategy_id": int(challenger.get("strategy_id") or 0),
            "name": str(challenger.get("name") or ""),
            "status": str(challenger.get("status") or ""),
            "primary_gap": primary_cause,
            "confidence": confidence,
            "challenger_return": challenger_return,
            "benchmark_return": benchmark_return,
            "benchmark_shortfall": benchmark_return - challenger_return,
            "failure_attribution": attribution,
        })
    selected = max(
        diagnoses,
        key=lambda item: (
            item["primary_gap"] != "unknown",
            confidence_score.get(item["confidence"], 0),
            item["benchmark_shortfall"],
            item["model_version_id"],
            item["strategy_id"],
        ),
        default={
            "primary_gap": "unknown", "challenger_return": 0.0,
            "benchmark_return": 0.0, "failure_attribution": {},
        },
    )
    primary_gap = str(selected.get("primary_gap") or "unknown")
    recommendations = {
        "market_exposure": "dynamic_market_exposure_with_etf_fallback_v1",
        "transaction_costs": "cost_and_turnover_constrained_candidate_required",
        "execution_constraints": "execution_constraint_replay_and_capacity_candidate_required",
        "selection_or_allocation_residual": "china_value_lowvol_reversal_rank_buffer_candidate_v1",
        "unknown": "no_automatic_candidate_without_replay_evidence",
    }
    return {
        "schema_version": "alis_strategy_diagnosis_v2",
        "primary_gap": primary_gap,
        "recommended_next_candidate": recommendations.get(primary_gap, recommendations["unknown"]),
        "selection_rule": "known_attribution_then_confidence_then_shortfall_then_newest_v1",
        "selected_model_version_id": int(selected.get("model_version_id") or 0) or None,
        "challenger_diagnoses": diagnoses,
        "evidence": {
            "challenger_return": float(selected.get("challenger_return") or 0.0),
            "best_baseline_return": baseline_return,
            "benchmark_return": float(selected.get("benchmark_return") or 0.0),
            "failure_attribution": selected.get("failure_attribution") or {},
        },
    }


def execute_evolution_task(
    task: dict[str, Any],
    *,
    validation_runner: Callable[..., dict[str, Any]] = run_purged_walk_forward_validation,
) -> dict[str, Any]:
    task_type = str(task.get("task_type") or "")
    if task_type == "system_release_governance":
        from app.services.system_release_center import run_release_governance_cycle

        _heartbeat(
            int(task["id"]), phase="system_release_governance", progress=0.1,
            message="正在发现系统版本变化并执行发布前硬门",
            checkpoint=task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {},
        )
        return run_release_governance_cycle(actor="evolution_system_release_governance")
    if task_type in {
        "strategy_evolution_cycle",
        "strategy_controller_oos",
        "strategy_shadow_cycle",
        "factor_daily_cycle",
        "model_shadow_observation",
    }:
        gate = _experiment_data_gate()
        if not bool(gate.get("experiment_ready")):
            return _blocked_by_decision_data(task, gate)
    if task_type == "strategy_evolution_cycle":
        return _run_with_heartbeat_pulse(
            int(task["id"]),
            phase="formal_oos",
            message="正在运行逐窗口正式 OOS",
            runner=lambda: _execute_strategy_cycle(
                task,
                validation_runner=validation_runner,
            ),
        )
    if task_type == "strategy_controller_oos":
        return _run_with_heartbeat_pulse(
            int(task["id"]),
            phase="formal_oos",
            message="正在运行 StrategyController 逐窗口正式 OOS",
            runner=lambda: _execute_strategy_cycle(
                task,
                validation_runner=validation_runner,
            ),
        )
    if task_type == "strategy_shadow_cycle":
        from app.services.strategy_lifecycle import run_strategy_shadow_cycle

        _heartbeat(
            int(task["id"]), phase="shadow_observation", progress=0.05,
            message="正在记录候选与 Champion 的影子对照",
            checkpoint=task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {},
        )
        return run_strategy_shadow_cycle(
            max_new_sessions_per_candidate=max(
                1, min(int((task.get("payload") or {}).get("max_new_sessions") or 5), 20)
            ),
        )
    if task_type == "factor_daily_cycle":
        from app.services.factor_platform import run_factor_daily_cycle

        _heartbeat(
            int(task["id"]), phase="factor_materialization", progress=0.05,
            message="正在生产统一版本因子截面",
            checkpoint=task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {},
        )
        return _run_with_heartbeat_pulse(
            int(task["id"]),
            phase="factor_materialization",
            message="正在生产统一版本因子截面",
            runner=run_factor_daily_cycle,
        )
    if task_type == "factor_research_cycle":
        from app.services.continuous_factor_research import (
            run_continuous_factor_research_cycle,
        )

        _heartbeat(
            int(task["id"]), phase="continuous_factor_research", progress=0.05,
            message="正在冻结完整研究包并搜索因子、模型与策略组合",
            checkpoint=task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {},
        )
        return run_continuous_factor_research_cycle(
            candidates_per_task=max(
                1, min(int((task.get("payload") or {}).get("candidates_per_task") or 1), 3)
            ),
            refresh_evidence=bool((task.get("payload") or {}).get("evidence_type")),
            progress_callback=lambda progress: _heartbeat(
                int(task["id"]),
                phase="continuous_factor_research",
                progress=min(
                    0.9,
                    0.1
                    + 0.8
                    * float(progress.get("completed_folds") or 0)
                    / max(1, int(progress.get("total_folds") or 1)),
                ),
                message=(
                    "连续因子模型策略回测 "
                    f"{int(progress.get('completed_folds') or 0)}/"
                    f"{int(progress.get('total_folds') or 1)}"
                ),
                checkpoint=progress,
            ),
        )
    if task_type == "strategy_candidate_generation":
        from app.services.continuous_strategy_research import run_active_candidate_generation_line

        _heartbeat(
            int(task["id"]), phase="strategy_candidate_generation", progress=0.1,
            message="正在持续生成动态规模因子策略候选",
            checkpoint=task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {},
        )
        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
        return run_active_candidate_generation_line(
            batch_size=max(1, min(int(payload.get("batch_size") or WORKER_RESEARCH_BATCH_SIZE), WORKER_RESEARCH_BATCH_SIZE if LOW_MEMORY_WORKER else 20)),
            model_version_id=(
                int(payload.get("evidence_id"))
                if str(payload.get("evidence_type") or "") == "model_version"
                and str(payload.get("evidence_id") or "").isdigit()
                else None
            ),
            model_key=str(evidence.get("model_key") or "") or None,
        )
    if task_type == "strategy_environment_backtest":
        from app.services.continuous_strategy_research import run_next_environment_backtest

        candidate_id = str((task.get("payload") or {}).get("candidate_id") or "")
        _heartbeat(
            int(task["id"]), phase="strategy_environment_backtest", progress=0.1,
            message=(
                f"正在执行候选 {candidate_id[-8:]} 的动态市场环境矩阵"
                if candidate_id else "环境回测任务缺少候选绑定"
            ),
            checkpoint={
                **(task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {}),
                "candidate_id": candidate_id or None,
            },
        )
        return _run_with_heartbeat_pulse(
            int(task["id"]),
            phase="strategy_environment_backtest",
            message=(
                f"正在执行候选 {candidate_id[-8:]} 的动态市场环境矩阵"
                if candidate_id else "环境回测任务缺少候选绑定"
            ),
            runner=lambda: run_next_environment_backtest(
                candidate_id=candidate_id or None
            ),
        )
    if task_type == "strategy_cluster_distillation":
        from app.services.continuous_strategy_research import run_cluster_distillation_line

        _heartbeat(
            int(task["id"]), phase="strategy_cluster_distillation", progress=0.1,
            message="正在蒸馏完整策略簇原型",
            checkpoint=task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {},
        )
        return run_cluster_distillation_line(
            snapshot_id=(task.get("payload") or {}).get("snapshot_id"),
            research_wave_id=(task.get("payload") or {}).get("research_wave_id"),
        )
    if task_type == "strategy_portfolio_research":
        from app.services.continuous_strategy_research import run_strategy_portfolio_line

        _heartbeat(
            int(task["id"]), phase="strategy_portfolio_research", progress=0.1,
            message="正在组合互补策略簇并准备二次回测",
            checkpoint=task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {},
        )
        return run_strategy_portfolio_line(
            maximum_members=max(2, min(int((task.get("payload") or {}).get("maximum_members") or 4), 12))
        )
    if task_type == "strategy_failure_model_training":
        from app.services.continuous_strategy_research import dispatch_targeted_model_training

        _heartbeat(
            int(task["id"]), phase="strategy_failure_model_training", progress=0.1,
            message="正在按策略失败归因派发定向模型训练",
            checkpoint=task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {},
        )
        return dispatch_targeted_model_training(
            limit=max(1, min(int((task.get("payload") or {}).get("limit") or 3), 10))
        )
    if task_type == "model_candidate_research":
        from app.services.model_candidate_research import run_model_candidate_search

        model_version_id = int((task.get("payload") or {}).get("model_version_id") or 0)
        _heartbeat(
            int(task["id"]), phase="model_candidate_quick_diagnostic", progress=0.1,
            message="正在归因最差正式窗口并快速淘汰模型候选",
            checkpoint={
                **(task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {}),
                "model_version_id": model_version_id,
            },
        )
        if model_version_id <= 0:
            raise ValueError("模型候选研究任务缺少 model_version_id")
        result = _run_with_heartbeat_pulse(
            int(task["id"]),
            phase="model_candidate_quick_diagnostic",
            message="正在运行受约束模型候选的两窗口快速诊断",
            runner=lambda: run_model_candidate_search(
                model_version_id=model_version_id,
                maximum_formal_winners=1,
            ),
        )
        run_ids = [
            int(item.get("run_id") or 0)
            for item in result.get("formal_dispatches") or []
            if int(item.get("run_id") or 0) > 0
        ]
        result["completed_training_runs"] = _wait_for_model_train_runs(
            int(task["id"]),
            run_ids,
            phase_prefix="model_candidate_formal_training",
        )
        result["resource_lock_contract"] = (
            "single_worker_held_until_model_train_terminal_v1"
        )
        return result
    if task_type == "model_strategy_impact_validation":
        from app.services.model_strategy_impact import run_controlled_model_strategy_ab

        model_version_id = int((task.get("payload") or {}).get("model_version_id") or 0)
        if model_version_id <= 0:
            raise ValueError("模型策略影响验证任务缺少 model_version_id")
        checkpoint = task.get("checkpoint") if isinstance(
            task.get("checkpoint"), dict
        ) else {}
        _heartbeat(
            int(task["id"]), phase="model_strategy_impact_validation", progress=0.1,
            message="正在固定 Champion 全部变量并仅替换模型版本执行五窗口 A/B",
            checkpoint={
                **checkpoint,
                "model_version_id": model_version_id,
            },
        )
        return _run_with_heartbeat_pulse(
            int(task["id"]),
            phase="model_strategy_impact_validation",
            message="正在执行新模型版本完整策略影响验证",
            runner=lambda: run_controlled_model_strategy_ab(
                model_version_id=model_version_id,
                checkpoint_results=(
                    checkpoint.get("completed_windows")
                    if isinstance(checkpoint.get("completed_windows"), list)
                    else []
                ),
                checkpoint_contract_hash=str(
                    checkpoint.get("contract_hash") or ""
                ),
                progress_callback=lambda progress: _heartbeat(
                    int(task["id"]),
                    phase="model_strategy_impact_validation",
                    progress=max(
                        0.1,
                        min(0.98, float(progress.get("progress") or 0.1)),
                    ),
                    message=str(
                        progress.get("message")
                        or "正在执行新模型版本完整策略影响验证"
                    ),
                    checkpoint={
                        "model_version_id": model_version_id,
                        "contract_hash": progress.get("contract_hash"),
                        "completed_windows": progress.get(
                            "completed_windows"
                        ) or [],
                    },
                ),
            ),
        )
    if task_type == "hsmm_shadow_training":
        from app.services.hsmm_shadow import train_hsmm_shadow

        payload = task.get("payload") if isinstance(
            task.get("payload"), dict
        ) else {}
        _heartbeat(
            int(task["id"]),
            phase="hsmm_shadow_training",
            progress=0.1,
            message="正在按显式时长当前合同运行三窗 HSMM/HMM 对照",
            checkpoint={
                **(
                    task.get("checkpoint")
                    if isinstance(task.get("checkpoint"), dict)
                    else {}
                ),
                "training_contract": "explicit_duration_forward_backward_em_v3",
                "production_change_forbidden": True,
            },
        )
        return _run_with_heartbeat_pulse(
            int(task["id"]),
            phase="hsmm_shadow_training",
            message="正在执行 HSMM 三窗任务原生评价",
            runner=lambda: train_hsmm_shadow(
                refresh_api=bool(payload.get("refresh_api", False)),
                force=bool(payload.get("force", False)),
            ),
        )
    if task_type == "model_shadow_observation":
        from app.services.model_shadow_observation import (
            advance_model_shadow_candidate,
        )

        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        model_version_id = int(payload.get("model_version_id") or 0)
        through_trade_date = str(payload.get("through_trade_date") or "")
        if model_version_id <= 0 or not through_trade_date:
            raise ValueError("模型未来影子任务缺少版本或截止交易日")
        checkpoint = task.get("checkpoint") if isinstance(
            task.get("checkpoint"), dict
        ) else {}
        _heartbeat(
            int(task["id"]),
            phase="model_shadow_observation",
            progress=0.05,
            message="正在重放冻结参考模型与候选模型的未来组合表现",
            checkpoint={
                **checkpoint,
                "model_version_id": model_version_id,
                "through_trade_date": through_trade_date,
            },
        )
        return _run_with_heartbeat_pulse(
            int(task["id"]),
            phase="model_shadow_observation",
            message="正在累计模型真实未来 Shadow 证据",
            runner=lambda: advance_model_shadow_candidate(
                model_version_id,
                through_trade_date=through_trade_date,
                progress_callback=lambda progress: _heartbeat(
                    int(task["id"]),
                    phase=str(
                        progress.get("phase") or "model_shadow_observation"
                    ),
                    progress=max(
                        0.05,
                        min(0.98, float(progress.get("progress") or 0.05)),
                    ),
                    message=str(
                        progress.get("message")
                        or "正在累计模型真实未来 Shadow 证据"
                    ),
                    checkpoint={
                        "model_version_id": model_version_id,
                        "through_trade_date": through_trade_date,
                        "side": progress.get("side"),
                        "completed_days": progress.get("completed_days"),
                        "total_days": progress.get("total_days"),
                        "observed_return_sessions": progress.get(
                            "observed_return_sessions"
                        ),
                    },
                ),
            ),
        )
    if task_type == "plan_model_autonomy":
        from app.services.plan_model_auto_optimizer import run_auto_optimization_once
        from app.services.plan_model_control import request_plan_model_training

        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        target_model_key = str(payload.get("target_model_key") or "")
        model_candidate_spec = (
            dict(payload.get("model_candidate_spec") or {})
            if isinstance(payload.get("model_candidate_spec"), dict)
            else {}
        )
        failure_recovery = payload.get("training_failure_recovery") if isinstance(
            payload.get("training_failure_recovery"), dict
        ) else None
        _heartbeat(
            int(task["id"]), phase="plan_model_autonomy", progress=0.1,
            message=(
                "正在分类并自动处理模型训练故障"
                if failure_recovery
                else "正在按单模型资源预算继续任务原生模型重训"
            ),
            checkpoint=task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {},
        )
        if failure_recovery:
            def model_runner() -> dict[str, Any]:
                return run_auto_optimization_once(
                    force=False,
                    training_failure_recovery=failure_recovery,
                )
        elif target_model_key and model_candidate_spec:
            def model_runner() -> dict[str, Any]:
                training = request_plan_model_training(
                    target_model_key,
                    training_context={
                        "requested_by": "evolution_targeted_model_candidate",
                        "trigger": str(payload.get("trigger") or "targeted_candidate"),
                        "source_model_version_id": int(
                            payload.get("source_model_version_id") or 0
                        ) or None,
                        "source_audit_contract": str(
                            payload.get("source_audit_contract") or ""
                        ),
                        "production_change_forbidden": True,
                        "model_candidate_spec": model_candidate_spec,
                    },
                )
                return {
                    "ok": bool(training.get("ok")),
                    "mode": "targeted_model_candidate",
                    "started": [training] if training.get("started") else [],
                    "started_count": 1 if training.get("started") else 0,
                    "skipped": [] if training.get("started") else [training],
                    "skipped_count": 0 if training.get("started") else 1,
                    "production_change_forbidden": True,
                }
        else:
            def model_runner() -> dict[str, Any]:
                return run_auto_optimization_once(force=False)
        result = _run_with_heartbeat_pulse(
            int(task["id"]),
            phase="plan_model_autonomy",
            message="正在执行任务原生模型自治",
            runner=model_runner,
        )
        run_ids = [
            int(item.get("run_id") or 0)
            for item in result.get("started") or []
            if int(item.get("run_id") or 0) > 0
        ]
        result["completed_training_runs"] = _wait_for_model_train_runs(
            int(task["id"]),
            run_ids,
            phase_prefix="plan_model_training",
        )
        result["resource_lock_contract"] = "single_worker_held_until_model_train_terminal_v1"
        return result
    if task_type == "strategy_portfolio_formal_queue":
        from app.services.continuous_strategy_research import materialize_next_portfolio_challenger

        _heartbeat(
            int(task["id"]), phase="strategy_portfolio_formal_queue", progress=0.1,
            message="正在冻结多策略组合并送入正式验证队列",
            checkpoint=task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {},
        )
        return materialize_next_portfolio_challenger()
    if task_type == "sealed_holdout_adj_factor_backfill":
        from app.services.sealed_strategy_holdout import run_sealed_holdout_adj_factor_backfill

        return run_sealed_holdout_adj_factor_backfill(
            task_id=int(task["id"]),
            batch_size=max(5, min(int((task.get("payload") or {}).get("batch_size") or 20), 30)),
            heartbeat=_heartbeat,
            ensure_active=_ensure_task_active,
            checkpoint=task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {},
        )
    if task_type == "system_autonomy_cycle":
        from app.services.system_autonomy import run_system_autonomy_cycle

        task_trade_date = str((task.get("payload") or {}).get("trade_date") or "").replace("-", "")[:8]

        _heartbeat(
            int(task["id"]), phase="system_autonomy", progress=0.05,
            message="正在运行模型、策略、组合、复盘与研究自治周期",
            checkpoint=task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {},
        )
        return _run_with_heartbeat_pulse(
            int(task["id"]),
            phase="system_autonomy",
            message="正在运行模型、策略、组合、复盘与研究自治周期",
            runner=lambda: run_system_autonomy_cycle(trade_date=task_trade_date),
        )
    raise ValueError(f"unsupported evolution task type: {task.get('task_type')}")


def _finish_success(task_id: int, result: dict[str, Any]) -> None:
    now = _now().isoformat()
    task = get_evolution_task(int(task_id)) or {}
    task_type = str(task.get("task_type") or "")
    message = {
        "system_release_governance": "系统版本变更发现与发布前硬门完成",
        "sealed_holdout_adj_factor_backfill": "密封验证集批量补全并复核完成",
        "factor_daily_cycle": "因子日物化与评价完成",
        "factor_research_cycle": "连续因子、模型与策略研究完成",
        "strategy_candidate_generation": "动态规模策略候选生成完成",
        "strategy_environment_backtest": "动态市场环境矩阵回测完成",
        "strategy_cluster_distillation": "完整策略簇蒸馏完成",
        "strategy_portfolio_research": "多策略组合研究完成",
        "strategy_failure_model_training": "策略失败归因模型训练派发完成",
        "model_candidate_research": "模型最差窗口归因与候选分层筛选完成",
        "model_strategy_impact_validation": "新模型版本完整策略影响验证完成",
        "hsmm_shadow_training": "HSMM 三窗任务原生评价完成",
        "model_shadow_observation": "模型未来影子观察完成",
        "plan_model_autonomy": "任务原生模型单模型自治调度完成",
        "strategy_portfolio_formal_queue": "多策略组合正式验证入队完成",
        "strategy_shadow_cycle": "策略影子观察完成",
        "strategy_evolution_cycle": "策略进化周期完成",
        "strategy_controller_oos": "策略控制器 OOS 完成",
        "system_autonomy_cycle": "系统自治周期完成",
    }.get(task_type, "自治任务完成")
    _update_active_task(
        int(task_id), statuses=("running",), require_not_stopped=True,
        expected_lease_owner=WORKER_ID,
        status="completed", phase="completed", progress=1.0,
        message=message, result=result, error=None,
        lease_owner=None, lease_expires_at=None, heartbeat_at=now,
        finished_at=now,
    )


def _defer_for_decision_data(
    task: dict[str, Any], result: dict[str, Any]
) -> bool:
    """Keep one blocked task pending instead of creating an empty lineage chain."""
    now = _now()
    return _update_active_task(
        int(task["id"]),
        statuses=("running",),
        require_not_stopped=True,
        expected_lease_owner=WORKER_ID,
        status="retry_wait",
        phase="waiting_for_decision_data",
        message=(
            "决策数据尚未达到研究门，保留原任务并等待数据恢复后续跑"
        ),
        result=result,
        error=None,
        attempt=max(0, int(task.get("attempt") or 1) - 1),
        next_run_at=(
            now + timedelta(seconds=DECISION_DATA_RETRY_SECONDS)
        ).isoformat(),
        lease_owner=None,
        lease_expires_at=None,
        deadline_at=None,
        heartbeat_at=now.isoformat(),
    )


def _checkpoint_progress_marker(checkpoint: dict[str, Any]) -> str:
    completed = checkpoint.get("completed") if isinstance(
        checkpoint.get("completed"), list
    ) else []
    current = checkpoint.get("current") if isinstance(
        checkpoint.get("current"), dict
    ) else {}
    windows = current.get("windows") if isinstance(
        current.get("windows"), list
    ) else []
    evidence = {
        "completed": [
            {
                "strategy_id": int(item.get("strategy_id") or 0),
                "controller_version_id": int(item.get("controller_version_id") or 0),
                "status": str(item.get("status") or ""),
            }
            for item in completed
            if isinstance(item, dict)
        ],
        "current_target": str(
            current.get("target_identity")
            or current.get("strategy_id")
            or ""
        ),
        "completed_windows": sorted(
            int(item.get("index") or 0)
            for item in windows
            if isinstance(item, dict)
        ),
        "hard_data_window": checkpoint.get("hard_data_window") or {},
    }
    return hashlib.sha256(_json(evidence).encode("utf-8")).hexdigest()[:20]


def _defer_for_resource_budget(
    task: dict[str, Any], exc: EvolutionTaskBudgetExceeded
) -> bool:
    """Resume formal OOS in the same task while detecting a genuinely stuck chunk."""
    checkpoint = (
        deepcopy(task.get("checkpoint"))
        if isinstance(task.get("checkpoint"), dict)
        else {}
    )
    previous = checkpoint.get("resource_budget_resume") if isinstance(
        checkpoint.get("resource_budget_resume"), dict
    ) else {}
    marker = _checkpoint_progress_marker(checkpoint)
    prior_marker = str(previous.get("progress_marker") or "")
    stalled_resumes = (
        int(previous.get("stalled_resumes") or 0) + 1
        if prior_marker and prior_marker == marker
        else 0
    )
    resume_count = int(previous.get("resume_count") or 0) + 1
    now = _now()
    resume_evidence = {
        "schema_version": "alis_formal_oos_resource_resume_v1",
        "resume_count": resume_count,
        "stalled_resumes": stalled_resumes,
        "progress_marker": marker,
        "deferred_at": now.isoformat(),
    }
    checkpoint["resource_budget_resume"] = resume_evidence
    if stalled_resumes >= RESOURCE_BUDGET_MAX_STALLED_RESUMES:
        return _update_active_task(
            int(task["id"]),
            statuses=("running",),
            require_not_stopped=True,
            expected_lease_owner=WORKER_ID,
            status="failed",
            phase="failed",
            progress=1.0,
            message="正式 OOS 连续耗尽资源预算且检查点没有进展",
            checkpoint=checkpoint,
            error=str(exc),
            lease_owner=None,
            lease_expires_at=None,
            deadline_at=None,
            heartbeat_at=now.isoformat(),
            finished_at=now.isoformat(),
        )
    return _update_active_task(
        int(task["id"]),
        statuses=("running",),
        require_not_stopped=True,
        expected_lease_owner=WORKER_ID,
        status="retry_wait",
        phase="resource_budget_checkpoint",
        message=(
            "本段资源预算已用完，已保存正式 OOS 检查点并等待续跑"
        ),
        checkpoint=checkpoint,
        error=None,
        attempt=max(0, int(task.get("attempt") or 1) - 1),
        next_run_at=(
            now + timedelta(seconds=RESOURCE_BUDGET_RETRY_SECONDS)
        ).isoformat(),
        lease_owner=None,
        lease_expires_at=None,
        deadline_at=None,
        heartbeat_at=now.isoformat(),
    )


def _schedule_strategy_continuation(*, completed_task_id: int) -> dict[str, Any] | None:
    """Keep draining and extending the candidate lineage without waiting for next Friday."""
    if V10_RESEARCH_ENABLED:
        return None
    targets = _strategy_targets()
    challengers = [item for item in targets if item.get("role") == "challenger"]
    if not challengers:
        return None
    candidate_ids = sorted(int(item.get("model_version_id") or 0) for item in challengers if int(item.get("model_version_id") or 0))
    if not candidate_ids:
        return None
    signature = "-".join(str(value) for value in candidate_ids[:MAX_CANDIDATES_PER_CYCLE])
    return create_evolution_task(
        task_type="strategy_evolution_cycle",
        payload={
            "max_windows": MAX_WINDOWS_PER_CANDIDATE,
            "max_candidates": MAX_CANDIDATES_PER_CYCLE,
            "trigger": "automatic_lineage_continuation",
            "predecessor_task_id": int(completed_task_id),
        },
        dedupe_key=f"strategy_evolution_continuation:v2:{signature}:after-{int(completed_task_id)}",
        priority=55,
    )


def _schedule_factor_research_continuation(
    *,
    completed_task_id: int,
    result: dict[str, Any],
) -> dict[str, Any] | None:
    if V10_RESEARCH_ENABLED:
        return None
    if not bool(result.get("continue_research")):
        return None
    snapshot = result.get("snapshot") if isinstance(result.get("snapshot"), dict) else {}
    snapshot_id = str(snapshot.get("snapshot_id") or "unknown")
    research_progress = {
        "experiment_count": int(result.get("experiment_count") or 0),
        "quick_screen_completed": int(result.get("quick_screen_completed") or 0),
        "confirmation_completed": int(result.get("confirmation_completed") or 0),
        "current_stage": str(result.get("current_stage") or result.get("status") or "unknown"),
        "rollover_epoch": bool(result.get("rollover_epoch")),
    }
    progress_signature = hashlib.sha256(
        _json(research_progress).encode("utf-8")
    ).hexdigest()[:16]
    task = create_evolution_task(
        task_type="factor_research_cycle",
        payload={
            "trigger": "continuous_research_continuation",
            "candidates_per_task": 1,
            "predecessor_task_id": int(completed_task_id),
            "research_progress": research_progress,
        },
        dedupe_key=(
            f"factor_research_continuation:v2:{snapshot_id}:"
            f"progress-{progress_signature}"
        ),
        priority=90,
    )
    if task.get("id") and str(task.get("status") or "") == "queued":
        _update_task(
            int(task["id"]),
            next_run_at=(_now() + timedelta(seconds=5)).isoformat(),
        )
    return get_evolution_task(int(task["id"])) if task.get("id") else task


def _schedule_v10_research_continuation(
    *,
    completed_task: dict[str, Any],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    """Connect the V10 research lines when the default engine is enabled."""
    if not V10_RESEARCH_ENABLED:
        return []
    task_type = str(completed_task.get("task_type") or "")
    source_id = int(completed_task.get("id") or 0)
    created: list[dict[str, Any]] = []

    def enqueue(kind: str, payload: dict[str, Any], suffix: str, priority: int) -> None:
        created.append(create_evolution_task(
            task_type=kind,
            payload={**payload, "predecessor_task_id": source_id},
            dedupe_key=f"v10:{kind}:{suffix}",
            priority=priority,
            coalesce_active_type=kind in {
                "strategy_candidate_generation",
                "strategy_portfolio_research",
                "strategy_failure_model_training",
            },
        ))

    if task_type == "strategy_candidate_generation":
        for item in result.get("generated") or []:
            enqueue(
                "strategy_environment_backtest",
                {
                    "trigger": "candidate_generated",
                    "candidate_id": item.get("candidate_id"),
                    "research_wave_id": item.get("research_wave_id"),
                },
                str(item.get("candidate_id") or source_id),
                88,
            )
    elif task_type == "strategy_environment_backtest":
        research_wave_id = str(
            result.get("research_wave_id")
            or (completed_task.get("payload") or {}).get("research_wave_id")
            or ""
        )
        if research_wave_id and not bool(result.get("wave_ready_for_distillation")):
            return created
        generation_task_id = int(
            (completed_task.get("payload") or {}).get("predecessor_task_id") or source_id
        )
        wave_suffix = research_wave_id or f"legacy-{generation_task_id}"
        enqueue(
            "strategy_cluster_distillation",
            {
                "trigger": "environment_wave_completed",
                "research_wave_id": research_wave_id or None,
            },
            wave_suffix,
            90,
        )
        enqueue(
            "strategy_failure_model_training",
            {
                "trigger": "strategy_failure_attribution",
                "research_wave_id": research_wave_id or None,
                "limit": 3,
            },
            wave_suffix,
            91,
        )
    elif task_type == "strategy_cluster_distillation":
        generated_ids = sorted(str(item.get("prototype_id") or "") for item in (result.get("generated") or []))
        if generated_ids:
            signature = hashlib.sha256(_json(generated_ids).encode("utf-8")).hexdigest()[:16]
            enqueue(
                "strategy_portfolio_research",
                {"trigger": "strategy_prototypes_distilled", "maximum_members": 4},
                signature,
                92,
            )
        enqueue(
            "strategy_candidate_generation",
            {
                "trigger": (
                    "distilled_prototypes_available"
                    if generated_ids else "cluster_needs_more_evidence"
                ),
                "batch_size": WORKER_RESEARCH_BATCH_SIZE,
            },
            f"after-cluster-{source_id}",
            94,
        )
    elif task_type == "strategy_portfolio_research":
        if str(result.get("status") or "") == "formal_validation_queued":
            enqueue(
                "strategy_portfolio_formal_queue",
                {"trigger": "portfolio_secondary_backtest_passed"},
                str(result.get("portfolio_id") or source_id),
                86,
            )
    elif task_type == "strategy_portfolio_formal_queue" and result.get("model_version_id"):
        enqueue(
            "strategy_evolution_cycle",
            {"trigger": "v10_portfolio_challenger", "max_candidates": 1},
            str(result["model_version_id"]),
            90,
        )
    return created


def _finish_failure(task: dict[str, Any], exc: Exception) -> None:
    task_id = int(task["id"])
    latest = _read_evolution_task_for_worker(task_id)
    if not latest or str(latest.get("status") or "") in TERMINAL_STATES:
        return
    task = latest
    if (
        isinstance(exc, EvolutionTaskBudgetExceeded)
        and str(task.get("task_type") or "") in {
            "strategy_evolution_cycle", "strategy_controller_oos"
        }
    ):
        _defer_for_resource_budget(task, exc)
        return
    if isinstance(exc, EvolutionTaskCancelled):
        _update_active_task(
            task_id, statuses=ACTIVE_STATES, expected_lease_owner=WORKER_ID,
            status="cancelled", phase="cancelled", progress=1.0,
            message=str(exc), error=None, lease_owner=None, lease_expires_at=None,
            finished_at=_now().isoformat(),
        )
        return
    if int(task.get("stop_requested") or 0):
        _update_active_task(
            task_id, statuses=ACTIVE_STATES, expected_lease_owner=WORKER_ID,
            status="cancelled", phase="cancelled", progress=1.0,
            message="任务已取消", error=None, lease_owner=None, lease_expires_at=None,
            finished_at=_now().isoformat(),
        )
        return
    attempt = int(task.get("attempt") or 1)
    maximum = int(task.get("max_attempts") or 3)
    from app.services.plan_model_control import classify_plan_model_training_failure

    classification = classify_plan_model_training_failure(exc)
    retryable = bool(classification.get("retryable"))
    failure_result = {
        "schema_version": "alis_evolution_failure_classification_v1",
        "classification": classification,
        "task_type": str(task.get("task_type") or ""),
    }
    if retryable and attempt < maximum:
        delay = min(900, 30 * (2 ** max(0, attempt - 1)))
        _update_active_task(
            task_id, statuses=("running",), require_not_stopped=True,
            expected_lease_owner=WORKER_ID,
            status="retry_wait", phase="retry_wait",
            message=f"执行失败，{delay} 秒后自动重试", error=str(exc)[:2000],
            result=failure_result,
            next_run_at=(_now() + timedelta(seconds=delay)).isoformat(),
            lease_owner=None, lease_expires_at=None,
        )
    else:
        _update_active_task(
            task_id, statuses=("running",), require_not_stopped=True,
            expected_lease_owner=WORKER_ID,
            status="failed", phase="failed", progress=1.0,
            message=(
                "超过自动重试次数"
                if retryable else "确定性或未知故障已停止重试，等待所有者异常复核"
            ),
            error=str(exc)[:2000], result=failure_result,
            lease_owner=None, lease_expires_at=None, finished_at=_now().isoformat(),
        )


def _run_claimed_task(task: dict[str, Any]) -> dict[str, Any]:
    task_id = int(task["id"])
    try:
        result = execute_evolution_task(task)
        if str(result.get("status") or "") == "blocked_by_decision_data":
            _defer_for_decision_data(task, result)
            return get_evolution_task(task_id) or task
        if str(result.get("status") or "") == "blocked_incomplete_factor_bindings":
            _finish_contract_block(task, result)
            return get_evolution_task(task_id) or task
        _finish_success(task_id, result)
        if str(task.get("task_type") or "").startswith("strategy_"):
            try:
                _schedule_v10_research_continuation(completed_task=task, result=result)
            except Exception:  # noqa: BLE001 - completed evidence remains immutable
                logger.exception("failed to schedule V10 research continuation task_id=%s", task.get("id"))
        if str(task.get("task_type") or "") == "strategy_evolution_cycle":
            try:
                _schedule_strategy_continuation(completed_task_id=int(task["id"]))
            except Exception:  # noqa: BLE001 - continuation failure must not rewrite completed result
                logger.exception("failed to schedule strategy lineage continuation task_id=%s", task.get("id"))
        if str(task.get("task_type") or "") == "factor_research_cycle":
            try:
                _schedule_factor_research_continuation(
                    completed_task_id=int(task["id"]),
                    result=result,
                )
                distillation = (
                    result.get("distillation")
                    if isinstance(result.get("distillation"), dict)
                    else {}
                )
                distilled = (
                    distillation.get("distillation")
                    if isinstance(distillation.get("distillation"), dict)
                    else {}
                )
                if (distilled.get("generated") or distilled.get("reused")):
                    _schedule_strategy_continuation(
                        completed_task_id=int(task["id"])
                    )
            except Exception:  # noqa: BLE001 - completed research evidence remains valid
                logger.exception("failed to schedule factor research continuation task_id=%s", task.get("id"))
        if str(task.get("task_type") or "") in {"factor_daily_cycle", "system_autonomy_cycle"}:
            try:
                evidence_type = (
                    "factor_evaluation"
                    if str(task.get("task_type") or "") == "factor_daily_cycle"
                    else "market_state_window"
                )
                enqueue_factor_strategy_research_event(
                    trigger=f"{evidence_type}_completed",
                    evidence_type=evidence_type,
                    evidence_id=int(task["id"]),
                    evidence={"source_task_id": int(task["id"])},
                    priority=85,
                )
            except Exception:  # noqa: BLE001 - source task remains completed
                logger.exception(
                    "failed to schedule research evidence task source_task_id=%s",
                    task.get("id"),
                )
    except Exception as exc:
        logger.exception("evolution task failed id=%s", task_id)
        _finish_failure(_read_evolution_task_for_worker(task_id) or task, exc)
    return _read_evolution_task_for_worker(task_id) or task


def run_worker_once() -> dict[str, Any] | None:
    reconciliation = reconcile_superseded_evolution_tasks()
    if reconciliation.get("count"):
        logger.warning(
            "quarantined %s superseded evolution task(s): %s",
            reconciliation["count"],
            [item.get("id") for item in reconciliation.get("quarantined") or []],
        )
    task = _claim_next_task()
    if not task:
        return None
    task_id = int(task["id"])
    with _active_task_lock:
        _active_task_ids.add(task_id)
    try:
        return _run_claimed_task(task)
    finally:
        with _active_task_lock:
            _active_task_ids.discard(task_id)


def recover_evolution_tasks() -> int:
    now = _now().isoformat()
    with _conn() as conn:
        cancelled = conn.execute(
            """SELECT id FROM evolution_tasks
               WHERE status='running' AND stop_requested=1"""
        ).fetchall()
        for row in cancelled:
            conn.execute(
                """UPDATE evolution_tasks SET status='cancelled',phase='cancelled',progress=1,
                   message='服务恢复时确认取消',finished_at=?,lease_owner=NULL,
                   lease_expires_at=NULL,heartbeat_at=?,updated_at=?
                   WHERE id=? AND status='running' AND stop_requested=1""",
                (now, now, now, int(row["id"])),
            )
        rows = conn.execute(
            """SELECT id,lease_owner,lease_expires_at FROM evolution_tasks
               WHERE status='running' AND stop_requested=0""",
        ).fetchall()
        recovered: list[Any] = []
        for row in rows:
            task_id = int(row["id"])
            lease_expires_at = row.get("lease_expires_at") if isinstance(row, dict) else row["lease_expires_at"]
            expired = not lease_expires_at
            if lease_expires_at:
                try:
                    expired = datetime.fromisoformat(str(lease_expires_at)) <= _now()
                except (TypeError, ValueError):
                    expired = True
            owner = row.get("lease_owner") if isinstance(row, dict) else row["lease_owner"]
            owner_alive = _lease_owner_process_alive(owner)
            if owner_alive is True:
                # A live owner may still be inside a long-running step. Only
                # the same process can distinguish an abandoned claim from
                # an active task; never requeue another live process's lease.
                if str(owner) != WORKER_ID or _is_task_execution_active(task_id) or not expired:
                    continue
            if not expired and owner_alive is not False:
                continue
            owner_predicate = "lease_owner=?" if owner else "lease_owner IS NULL"
            params: tuple[Any, ...] = (
                (now, now, now, task_id, owner)
                if owner else (now, now, now, task_id)
            )
            cursor = conn.execute(
                f"""UPDATE evolution_tasks SET status='queued',phase='recovered',
                    message='服务重启后从检查点恢复',lease_owner=NULL,lease_expires_at=NULL,
                    heartbeat_at=?,next_run_at=?,updated_at=?
                    WHERE id=? AND status='running' AND stop_requested=0 AND {owner_predicate}""",
                params,
            )
            if int(cursor.rowcount or 0) > 0:
                recovered.append(row)
    return len(cancelled) + len(recovered)


def start_evolution_worker() -> bool:
    global _worker_started, _lease_recovery_started
    with _start_lock:
        if _worker_started:
            return False
        _worker_started = True

    def loop() -> None:
        while True:
            try:
                _runtime_heartbeat("worker")
                if run_worker_once() is None:
                    time.sleep(POLL_SECONDS)
            except Exception as exc:  # noqa: BLE001 - worker must survive transient DB/runtime faults
                logger.exception("evolution worker loop recovered from error")
                _runtime_heartbeat("worker", error=str(exc))
                time.sleep(POLL_SECONDS)

    threading.Thread(target=loop, daemon=True, name="evolution-worker").start()
    with _start_lock:
        if not _lease_recovery_started:
            _lease_recovery_started = True

            def recovery_loop() -> None:
                while True:
                    try:
                        recovered = _with_heartbeat_db_retry(recover_evolution_tasks)
                        if recovered:
                            logger.warning("recovered %s expired evolution task lease(s)", recovered)
                    except Exception:  # noqa: BLE001 - recovery must not stop task execution
                        logger.exception("evolution lease recovery loop failed")
                    time.sleep(LEASE_RECOVERY_INTERVAL_SECONDS)

            threading.Thread(
                target=recovery_loop,
                daemon=True,
                name="evolution-lease-recovery",
            ).start()
    return True


def start_evolution_scheduler() -> bool:
    global _scheduler_started
    if not AUTO_ENABLED:
        with _runtime_lock:
            _runtime_status["scheduler"].update({"started": False, "alive": False, "disabled": True, "last_error": ""})
        return False
    with _start_lock:
        if _scheduler_started:
            return False
        _scheduler_started = True

    def loop() -> None:
        while True:
            try:
                _runtime_heartbeat("scheduler")
                now = _now()
                ensure_system_release_governance_task(trigger="continuous_scheduler")
                research_task_type = (
                    "strategy_candidate_generation"
                    if V10_RESEARCH_ENABLED else "factor_research_cycle"
                )
                create_evolution_task(
                    task_type=research_task_type,
                    dedupe_key=f"research_seed:v2:{research_task_type}:{now.date().isoformat()}",
                    payload={
                        "trigger": "continuous_scheduler",
                        **({"batch_size": WORKER_RESEARCH_BATCH_SIZE} if V10_RESEARCH_ENABLED else {"candidates_per_task": 1}),
                    },
                    priority=90,
                )
                ensure_latest_ready_system_cycle()
                if _is_scheduled_trade_day(now) and now.hour >= 16:
                    create_evolution_task(
                        task_type="factor_daily_cycle",
                        dedupe_key=f"factor_daily_cycle:{now.date().isoformat()}",
                        payload={"trigger": "daily_scheduler"},
                        priority=20,
                    )
                if _is_scheduled_trade_day(now) and now.hour >= 17:
                    create_evolution_task(
                        task_type="system_autonomy_cycle",
                        dedupe_key=f"system_autonomy_cycle:{now.date().isoformat()}",
                        payload={
                            "trigger": "daily_scheduler",
                            "trade_date": now.date().strftime("%Y%m%d"),
                        },
                        priority=70,
                    )
                if (
                    not V10_RESEARCH_ENABLED
                    and _is_scheduled_trade_day(now)
                    and now.weekday() == AUTO_WEEKDAY
                    and now.hour >= AUTO_HOUR
                ):
                    create_evolution_task(
                        task_type="strategy_evolution_cycle",
                        dedupe_key=f"strategy_evolution_cycle:{now.date().isoformat()}",
                        payload={"max_windows": 5, "trigger": "weekly_scheduler"},
                    )
            except Exception as exc:  # noqa: BLE001 - scheduler must remain alive
                logger.exception("evolution scheduler loop recovered from error")
                _runtime_heartbeat("scheduler", error=str(exc))
            time.sleep(300)

    threading.Thread(target=loop, daemon=True, name="evolution-scheduler").start()
    return True


def ensure_system_release_governance_task(*, trigger: str = "startup") -> dict[str, Any]:
    """Queue release governance before research workers can consume changed bindings."""
    from app.services.system_release_center import discover_release_change

    release_change = discover_release_change()
    if not (release_change.get("changed") or release_change.get("restart_required")):
        return {"ok": True, "status": "unchanged", "discovery": release_change}
    identity = hashlib.sha256(json.dumps({
        "runtime": release_change.get("runtime_commit"),
        "repository": release_change.get("repository_commit"),
        "observed": release_change.get("observed_manifest_hash"),
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:20]
    task = create_evolution_task(
        task_type="system_release_governance",
        dedupe_key=f"system_release_governance:v1:{identity}",
        payload={"trigger": str(trigger), "discovery": release_change},
        max_attempts=1,
        priority=5,
    )
    return {"ok": True, "status": str(task.get("status") or "queued"), "task": task, "discovery": release_change}
