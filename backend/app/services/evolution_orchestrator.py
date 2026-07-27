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
    list_model_versions,
    update_model_version_metrics,
    update_model_version_status,
    update_model_version_validation,
)
from app.services.production_strategy import resolve_production_strategy_champion
from app.services.walk_forward_validation import run_purged_walk_forward_validation
from app.services.strategy_replay import run_strategy_replay
from app.services.backtest_data_source import BacktestDataSource
from app.services.strategy_lifecycle import advance_formal_candidate_lifecycle
from app.services.strategy_statistics import deflated_sharpe_ratio


logger = logging.getLogger("quant.evolution")
ACTIVE_STATES = ("queued", "running", "retry_wait")
TERMINAL_STATES = ("completed", "failed", "cancelled")
LEASE_MINUTES = max(10, min(int(os.getenv("EVOLUTION_LEASE_MINUTES", "30")), 180))
POLL_SECONDS = max(2, min(int(os.getenv("EVOLUTION_POLL_SECONDS", "5")), 60))
AUTO_ENABLED = str(os.getenv("EVOLUTION_AUTO_ENABLED", "1")).lower() in {"1", "true", "yes", "on"}
AUTO_WEEKDAY = max(0, min(int(os.getenv("EVOLUTION_AUTO_WEEKDAY", "4")), 6))
AUTO_HOUR = max(0, min(int(os.getenv("EVOLUTION_AUTO_HOUR", "18")), 23))
MAX_CANDIDATES_PER_CYCLE = max(1, min(int(os.getenv("EVOLUTION_MAX_CANDIDATES", "6")), 20))
MAX_WINDOWS_PER_CANDIDATE = max(1, min(int(os.getenv("EVOLUTION_MAX_WINDOWS", "5")), 20))
REQUIRED_OPPORTUNITY_COST_BASELINES = ("multifactor_equal_weight", "simple_momentum")
TASK_TIMEOUT_MINUTES = max(30, min(int(os.getenv("EVOLUTION_TASK_TIMEOUT_MINUTES", "240")), 1440))
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
SYSTEM_AUTONOMY_RECOVERY_POLICY_VERSION = "v2"
_worker_started = False
_scheduler_started = False
_start_lock = threading.Lock()
_runtime_lock = threading.Lock()
_runtime_status: dict[str, dict[str, Any]] = {
    "worker": {"started": False, "alive": False, "heartbeat_at": None, "last_error": ""},
    "scheduler": {"started": False, "alive": False, "heartbeat_at": None, "last_error": ""},
}


class EvolutionTaskCancelled(RuntimeError):
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


def create_evolution_task(
    *,
    task_type: str = "strategy_evolution_cycle",
    payload: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    max_attempts: int = 3,
    priority: int = 100,
    force: bool = False,
) -> dict[str, Any]:
    now = _now().isoformat()
    payload = dict(payload or {})
    key = str(dedupe_key or f"{task_type}:{now[:10]}")
    if force:
        key = f"{key}:{uuid.uuid4().hex[:8]}"
    with _conn() as conn:
        if conn._is_postgres:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (key,))
        existing = conn.execute(
            """SELECT * FROM evolution_tasks
               WHERE dedupe_key=?
               ORDER BY id DESC LIMIT 1""",
            (key,),
        ).fetchone()
        if existing:
            return _row(existing)
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
    return get_evolution_task(task_id) or {"id": task_id, "status": "queued"}


def get_evolution_task(task_id: int) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM evolution_tasks WHERE id=?", (int(task_id),)).fetchone()
    return _row(row) if row else None


def list_evolution_tasks(limit: int = 20) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM evolution_tasks ORDER BY id DESC LIMIT ?",
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
    return get_evolution_task(int(task_id))


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
        "auto_enabled": AUTO_ENABLED,
        "worker_id": WORKER_ID,
        "budgets": {
            "single_worker": True,
            "max_candidates_per_cycle": MAX_CANDIDATES_PER_CYCLE,
            "max_windows_per_candidate": MAX_WINDOWS_PER_CANDIDATE,
            "task_timeout_minutes": TASK_TIMEOUT_MINUTES,
            "hard_history_contract_sessions": 1537,
        },
        "next_factor_cycle_at": _next_business_day_at(now, 16).isoformat() if AUTO_ENABLED else None,
        "continuous_factor_research": {
            "enabled": AUTO_ENABLED,
            "cadence": "persistent_low_priority_continuation",
            "requires_market_close": False,
            "priority": 90,
        },
        "next_shadow_cycle_at": _next_business_day_at(now, 16).isoformat() if AUTO_ENABLED else None,
        "next_system_cycle_at": _next_business_day_at(now, 17).isoformat() if AUTO_ENABLED else None,
        "next_formal_cycle_at": _next_weekday_at(now, AUTO_WEEKDAY, AUTO_HOUR).isoformat() if AUTO_ENABLED else None,
    }


def enqueue_system_autonomy_recovery(trade_date: str, data_contract: dict[str, Any]) -> dict[str, Any]:
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
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return create_evolution_task(
        task_type="system_autonomy_cycle",
        dedupe_key=f"system_autonomy_recovery:{SYSTEM_AUTONOMY_RECOVERY_POLICY_VERSION}:{trade_date}:{fingerprint}",
        payload={
            "trigger": "decision_data_recovered",
            "trade_date": trade_date,
            "data_contract_fingerprint": fingerprint,
        },
        priority=85,
    )


def ensure_latest_ready_system_cycle() -> dict[str, Any]:
    """Recover a missed same-date system cycle, including after restarts/weekends."""
    from app.services.data_health import assess_latest_decision_data_health

    health = assess_latest_decision_data_health()
    readiness = health.get("readiness") if isinstance(health.get("readiness"), dict) else {}
    trade_date = str(readiness.get("target_trade_date") or "")
    if not trade_date or not bool(health.get("decision_ready")):
        return {"ok": False, "reason": "decision_data_not_ready", "trade_date": trade_date}
    for task in list_evolution_tasks(limit=30):
        if str(task.get("task_type") or "") != "system_autonomy_cycle" or str(task.get("status") or "") != "completed":
            continue
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        decision_ready_for_date = False
        plan_status = ""
        for step in result.get("steps") or []:
            if not isinstance(step, dict) or str(step.get("name") or "") != "decision_data_gate":
                if isinstance(step, dict) and str(step.get("name") or "") == "profit_and_portfolio":
                    profit = step.get("result") if isinstance(step.get("result"), dict) else {}
                    plan = profit.get("rebalance_plan") if isinstance(profit.get("rebalance_plan"), dict) else {}
                    plan_status = str(plan.get("status") or "")
                continue
            gate = step.get("result") if isinstance(step.get("result"), dict) else {}
            gate_readiness = gate.get("readiness") if isinstance(gate.get("readiness"), dict) else {}
            decision_ready_for_date = bool(gate.get("decision_ready")) and str(gate_readiness.get("target_trade_date") or "") == trade_date
        if decision_ready_for_date and plan_status != "blocked_by_decision_data":
            return {"ok": True, "status": "already_completed", "trade_date": trade_date, "task_id": task.get("id")}
    contract = readiness.get("decision_contract") if isinstance(readiness.get("decision_contract"), dict) else {}
    if not contract:
        from app.data_platform.services.decision_data_contract import decision_data_contract
        contract = decision_data_contract(trade_date)
    return enqueue_system_autonomy_recovery(trade_date, contract)


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
    _ensure_task_active(task_id)
    now = _now()
    _update_task(
        task_id,
        phase=phase,
        progress=max(0.0, min(0.999, float(progress))),
        message=message,
        checkpoint=checkpoint,
        heartbeat_at=now.isoformat(),
        lease_owner=WORKER_ID,
        lease_expires_at=(now + timedelta(minutes=LEASE_MINUTES)).isoformat(),
    )


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
                raise EvolutionTaskCancelled("任务超过资源预算时限")
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
            })
    for item in targets:
        if generated and int(item["strategy_id"]) == int(generated["strategy_id"]):
            item["name"] = generated["name"]
    return targets


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
    targets = payload.get("targets") if isinstance(payload.get("targets"), list) else _strategy_targets()
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
    max_windows = max(1, min(int(payload.get("max_windows") or MAX_WINDOWS_PER_CANDIDATE), MAX_WINDOWS_PER_CANDIDATE))
    checkpoint = task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {}
    completed = checkpoint.get("completed") if isinstance(checkpoint.get("completed"), list) else []
    def target_identity(item: dict[str, Any]) -> str:
        controller_id = int(item.get("controller_version_id") or 0)
        return f"controller:{controller_id}" if controller_id else f"strategy:{int(item.get('strategy_id') or 0)}"

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
                checkpoint={"completed": completed, "current": {}, "hard_data_window": {"start": audit["window_start"], "end": audit["window_end"]}},
            )
            quick = _run_quick_gate(target, audit)
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
                    checkpoint={"completed": completed, "current": {}, "hard_data_window": {"start": audit["window_start"], "end": audit["window_end"]}},
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
            next_checkpoint = {
                "completed": completed,
                "current": {"target_identity": identity, "strategy_id": strategy_id, "controller_version_id": target.get("controller_version_id"), "windows": windows},
                "hard_data_window": {"start": audit["window_start"], "end": audit["window_end"]},
            }
            _heartbeat(
                int(task["id"]),
                phase="formal_oos",
                progress=overall_units / total_units,
                message=f"{target.get('name')}：{event.get('message')}",
                checkpoint=next_checkpoint,
            )

        result = validation_runner(
            strategy_id=strategy_id,
            start_date=str(audit["window_start"]),
            end_date=str(audit["window_end"]),
            max_windows=max_windows,
            strategy_controller_version_id=target.get("controller_version_id"),
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
            window_start=str(audit["window_start"]),
            window_end=str(audit["window_end"]),
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
            checkpoint={"completed": completed, "current": {}, "hard_data_window": {"start": audit["window_start"], "end": audit["window_end"]}},
        )
    lifecycle_targets = [item for item in completed if item.get("role") != "strategy_controller_candidate"]
    lifecycle = advance_formal_candidate_lifecycle(lifecycle_targets, shadow_start_date=str(audit["window_end"])) if lifecycle_targets else []
    lifecycle_by_id = {int(item.get("model_version_id") or 0): item for item in lifecycle}
    for item in completed:
        lifecycle_item = lifecycle_by_id.get(int(item.get("model_version_id") or 0))
        if lifecycle_item and isinstance(lifecycle_item.get("sealed_holdout"), dict):
            item["sealed_holdout"] = lifecycle_item["sealed_holdout"]
    return {
        "schema_version": "alis_evolution_cycle_v1",
        "status": "completed",
        "hard_data_window": {"start": audit["window_start"], "end": audit["window_end"]},
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
    if task_type in {"strategy_evolution_cycle", "strategy_controller_oos", "strategy_shadow_cycle", "factor_daily_cycle"}:
        gate = _experiment_data_gate()
        if not bool(gate.get("experiment_ready")):
            return _blocked_by_decision_data(task, gate)
    if task_type == "strategy_evolution_cycle":
        return _execute_strategy_cycle(task, validation_runner=validation_runner)
    if task_type == "strategy_controller_oos":
        return _execute_strategy_cycle(task, validation_runner=validation_runner)
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
        return run_factor_daily_cycle()
    if task_type == "factor_research_cycle":
        from app.services.continuous_factor_research import (
            run_continuous_factor_research_cycle,
        )

        _heartbeat(
            int(task["id"]), phase="continuous_factor_research", progress=0.05,
            message="正在冻结历史截面并搜索因子组合",
            checkpoint=task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {},
        )
        return run_continuous_factor_research_cycle(
            candidates_per_task=max(
                1, min(int((task.get("payload") or {}).get("candidates_per_task") or 1), 3)
            ),
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
                    "连续因子组合回测 "
                    f"{int(progress.get('completed_folds') or 0)}/"
                    f"{int(progress.get('total_folds') or 1)}"
                ),
                checkpoint=progress,
            ),
        )
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

        _heartbeat(
            int(task["id"]), phase="system_autonomy", progress=0.05,
            message="正在运行模型、策略、组合、复盘与研究自治周期",
            checkpoint=task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {},
        )
        return run_system_autonomy_cycle()
    raise ValueError(f"unsupported evolution task type: {task.get('task_type')}")


def _finish_success(task_id: int, result: dict[str, Any]) -> None:
    now = _now().isoformat()
    task = get_evolution_task(int(task_id)) or {}
    task_type = str(task.get("task_type") or "")
    message = {
        "sealed_holdout_adj_factor_backfill": "密封验证集批量补全并复核完成",
        "factor_daily_cycle": "因子日物化与评价完成",
        "factor_research_cycle": "连续因子组合研究完成",
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


def _schedule_strategy_continuation(*, completed_task_id: int) -> dict[str, Any] | None:
    """Keep draining and extending the candidate lineage without waiting for next Friday."""
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
    if not bool(result.get("continue_research")):
        return None
    snapshot = result.get("snapshot") if isinstance(result.get("snapshot"), dict) else {}
    snapshot_id = str(snapshot.get("snapshot_id") or "unknown")
    task = create_evolution_task(
        task_type="factor_research_cycle",
        payload={
            "trigger": "continuous_research_continuation",
            "candidates_per_task": 1,
            "predecessor_task_id": int(completed_task_id),
        },
        dedupe_key=(
            f"factor_research_continuation:{snapshot_id}:"
            f"after-{int(completed_task_id)}"
        ),
        priority=90,
    )
    if task.get("id") and str(task.get("status") or "") == "queued":
        _update_task(
            int(task["id"]),
            next_run_at=(_now() + timedelta(seconds=5)).isoformat(),
        )
    return get_evolution_task(int(task["id"])) if task.get("id") else task


def _finish_failure(task: dict[str, Any], exc: Exception) -> None:
    task_id = int(task["id"])
    latest = get_evolution_task(task_id)
    if not latest or str(latest.get("status") or "") in TERMINAL_STATES:
        return
    task = latest
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
    if attempt < maximum:
        delay = min(900, 30 * (2 ** max(0, attempt - 1)))
        _update_active_task(
            task_id, statuses=("running",), require_not_stopped=True,
            expected_lease_owner=WORKER_ID,
            status="retry_wait", phase="retry_wait",
            message=f"执行失败，{delay} 秒后自动重试", error=str(exc)[:2000],
            next_run_at=(_now() + timedelta(seconds=delay)).isoformat(),
            lease_owner=None, lease_expires_at=None,
        )
    else:
        _update_active_task(
            task_id, statuses=("running",), require_not_stopped=True,
            expected_lease_owner=WORKER_ID,
            status="failed", phase="failed", progress=1.0,
            message="超过自动重试次数", error=str(exc)[:2000],
            lease_owner=None, lease_expires_at=None, finished_at=_now().isoformat(),
        )


def run_worker_once() -> dict[str, Any] | None:
    task = _claim_next_task()
    if not task:
        return None
    try:
        result = execute_evolution_task(task)
        _finish_success(int(task["id"]), result)
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
    except Exception as exc:
        logger.exception("evolution task failed id=%s", task.get("id"))
        _finish_failure(get_evolution_task(int(task["id"])) or task, exc)
    return get_evolution_task(int(task["id"]))


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
            lease_expires_at = row.get("lease_expires_at") if isinstance(row, dict) else row["lease_expires_at"]
            expired = not lease_expires_at
            if lease_expires_at:
                try:
                    expired = datetime.fromisoformat(str(lease_expires_at)) <= _now()
                except (TypeError, ValueError):
                    expired = True
            owner = row.get("lease_owner") if isinstance(row, dict) else row["lease_owner"]
            owner_alive = _lease_owner_process_alive(owner)
            if not expired and owner_alive is not False:
                continue
            owner_predicate = "lease_owner=?" if owner else "lease_owner IS NULL"
            params: tuple[Any, ...] = (
                (now, now, now, int(row["id"]), owner)
                if owner else (now, now, now, int(row["id"]))
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
    global _worker_started
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
                create_evolution_task(
                    task_type="factor_research_cycle",
                    dedupe_key=f"factor_research_seed:v1:{now.date().isoformat()}",
                    payload={
                        "trigger": "continuous_scheduler",
                        "candidates_per_task": 1,
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
                    create_evolution_task(
                        task_type="strategy_shadow_cycle",
                        dedupe_key=f"strategy_shadow_cycle:{now.date().isoformat()}",
                        payload={"max_new_sessions": 5, "trigger": "daily_scheduler"},
                        priority=50,
                    )
                if _is_scheduled_trade_day(now) and now.hour >= 17:
                    create_evolution_task(
                        task_type="system_autonomy_cycle",
                        dedupe_key=f"system_autonomy_cycle:{now.date().isoformat()}",
                        payload={"trigger": "daily_scheduler"},
                        priority=70,
                    )
                if _is_scheduled_trade_day(now) and now.weekday() == AUTO_WEEKDAY and now.hour >= AUTO_HOUR:
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
