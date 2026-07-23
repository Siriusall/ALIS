from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging
import os
import socket
import threading
import time
import uuid
from typing import Any, Callable

from app.data_platform.services.oos_hard_data import audit_oos_hard_data
from app.db.store import (
    _conn,
    list_model_versions,
    update_model_version_metrics,
    update_model_version_validation,
)
from app.services.production_strategy import resolve_production_strategy_champion
from app.services.walk_forward_validation import run_purged_walk_forward_validation
from app.services.strategy_replay import run_strategy_replay
from app.services.backtest_data_source import BacktestDataSource
from app.services.strategy_lifecycle import advance_formal_candidate_lifecycle


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
TASK_TIMEOUT_MINUTES = max(30, min(int(os.getenv("EVOLUTION_TASK_TIMEOUT_MINUTES", "240")), 1440))
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
_worker_started = False
_scheduler_started = False
_start_lock = threading.Lock()


class EvolutionTaskCancelled(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now().astimezone()


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
    with _conn() as conn:
        if task["status"] in {"queued", "retry_wait"}:
            conn.execute(
                """UPDATE evolution_tasks SET stop_requested=1,status='cancelled',
                   phase='cancelled',progress=1,message='任务已取消',finished_at=?,updated_at=?
                   WHERE id=?""",
                (now, now, int(task_id)),
            )
        else:
            conn.execute(
                """UPDATE evolution_tasks SET stop_requested=1,
                   message='收到取消请求，将在当前安全检查点停止',updated_at=? WHERE id=?""",
                (now, int(task_id)),
            )
    return get_evolution_task(int(task_id))


def _next_weekday_at(now: datetime, weekday: int, hour: int) -> datetime:
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    days = (weekday - candidate.weekday()) % 7
    candidate += timedelta(days=days)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def _next_business_day_at(now: datetime, hour: int) -> datetime:
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
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
        "next_shadow_cycle_at": _next_business_day_at(now, 16).isoformat() if AUTO_ENABLED else None,
        "next_system_cycle_at": _next_business_day_at(now, 17).isoformat() if AUTO_ENABLED else None,
        "next_formal_cycle_at": _next_weekday_at(now, AUTO_WEEKDAY, AUTO_HOUR).isoformat() if AUTO_ENABLED else None,
    }


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
    deadline = task.get("deadline_at")
    if deadline:
        try:
            if _now() >= datetime.fromisoformat(str(deadline)):
                raise EvolutionTaskCancelled("任务超过资源预算时限")
        except ValueError:
            pass


def _strategy_targets() -> list[dict[str, Any]]:
    champion = resolve_production_strategy_champion()
    from app.services.strategy_candidate_generator import ensure_dynamic_exposure_challenger

    generated = ensure_dynamic_exposure_challenger()
    targets = [{
        "role": "champion",
        "strategy_id": int(champion["strategy_id"]),
        "model_version_id": int(champion["champion_id"]),
        "name": str((champion.get("strategy") or {}).get("name") or "Champion"),
    }]
    for model_key, role in (
        ("alis_strategy_challenger", "challenger"),
        ("alis_strategy_baseline", "baseline"),
    ):
        for version in reversed(list_model_versions(model_key, limit=20)):
            config = version.get("config") if isinstance(version.get("config"), dict) else {}
            validation = version.get("validation") if isinstance(version.get("validation"), dict) else {}
            validation_status = str(validation.get("status") or "")
            if validation_status in {"quick_diagnostic_failed", "rejected"}:
                continue
            strategy_id = int(config.get("strategy_id") or 0)
            if strategy_id and all(item["strategy_id"] != strategy_id for item in targets):
                targets.append({
                    "role": role,
                    "strategy_id": strategy_id,
                    "model_version_id": int(version["id"]),
                    "name": str(config.get("baseline_key") or version.get("version") or role),
                    "generation": str(config.get("generation") or ""),
                    "validation_status": validation_status,
                })
    for item in targets:
        if int(item["strategy_id"]) == int(generated["strategy_id"]):
            item["name"] = generated["name"]
    return targets


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
        "worst_window_drawdown": summary.get("worst_window_drawdown"),
        "total_fees": summary.get("total_fees"),
        "window_returns": [float((window.get("metrics") or {}).get("total_return") or 0.0) for window in windows],
    }


def _persist_candidate_result(target: dict[str, Any], result: dict[str, Any]) -> None:
    if target["role"] == "champion":
        return
    update_model_version_metrics(target["model_version_id"], metrics={"formal_oos": _formal_metrics(result)})
    update_model_version_validation(target["model_version_id"], validation={
        "status": f"formal_oos_{result.get('status')}",
        "production_eligible": False,
        "passed_gates": [key for key, value in (result.get("promotion_gates") or {}).items() if value],
        "blocking_reasons": result.get("blocking_reasons") or [],
        "comparison_role": "diagnostic_baseline_not_promotable" if target["role"] == "baseline" else "challenger",
        "next_gate": "automatic_evolution_diagnosis",
    })


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
    targets = targets[:max(1, min(int(payload.get("max_candidates") or MAX_CANDIDATES_PER_CYCLE), MAX_CANDIDATES_PER_CYCLE))]
    max_windows = max(1, min(int(payload.get("max_windows") or MAX_WINDOWS_PER_CANDIDATE), MAX_WINDOWS_PER_CANDIDATE))
    checkpoint = task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {}
    completed = checkpoint.get("completed") if isinstance(checkpoint.get("completed"), list) else []
    completed_ids = {int(item.get("strategy_id") or 0) for item in completed}
    current = checkpoint.get("current") if isinstance(checkpoint.get("current"), dict) else {}
    total_units = max(1, len(targets) * max_windows)

    for target_index, target in enumerate(targets):
        _ensure_task_active(int(task["id"]))
        strategy_id = int(target["strategy_id"])
        if strategy_id in completed_ids:
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
                completed_ids.add(strategy_id)
                _heartbeat(
                    int(task["id"]), phase="quick_diagnostic",
                    progress=(target_index + 1) / max(1, len(targets)),
                    message=f"{target.get('name')} 快速诊断未通过，已自动淘汰",
                    checkpoint={"completed": completed, "current": {}, "hard_data_window": {"start": audit["window_start"], "end": audit["window_end"]}},
                )
                continue
        prior_windows = (
            current.get("windows") if int(current.get("strategy_id") or 0) == strategy_id
            and isinstance(current.get("windows"), list) else []
        )

        def progress(event: dict[str, Any]) -> None:
            windows = event.get("checkpoint_results") or []
            overall_units = target_index * max_windows + len(windows)
            next_checkpoint = {
                "completed": completed,
                "current": {"strategy_id": strategy_id, "windows": windows},
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
            execute=True,
            progress_callback=progress,
            checkpoint_results=prior_windows,
        )
        _persist_candidate_result(target, result)
        completed.append({
            "strategy_id": strategy_id,
            "model_version_id": int(target.get("model_version_id") or 0),
            "role": target.get("role"),
            "name": target.get("name"),
            "status": result.get("status"),
            "summary": result.get("summary"),
            "blocking_reasons": result.get("blocking_reasons"),
        })
        completed_ids.add(strategy_id)
        current = {}
        _heartbeat(
            int(task["id"]),
            phase="formal_oos",
            progress=len(completed) / max(1, len(targets)),
            message=f"{target.get('name')} 正式验证完成",
            checkpoint={"completed": completed, "current": {}, "hard_data_window": {"start": audit["window_start"], "end": audit["window_end"]}},
        )
    lifecycle = advance_formal_candidate_lifecycle(
        completed,
        shadow_start_date=str(audit["window_end"]),
    )
    return {
        "schema_version": "alis_evolution_cycle_v1",
        "status": "completed",
        "hard_data_window": {"start": audit["window_start"], "end": audit["window_end"]},
        "strategies": completed,
        "diagnosis": diagnose_strategy_results(completed),
        "lifecycle": lifecycle,
    }


def diagnose_strategy_results(items: list[dict[str, Any]]) -> dict[str, Any]:
    by_role = {str(item.get("role")): item for item in items}
    baseline_items = [item for item in items if item.get("role") == "baseline"]
    challenger = by_role.get("challenger") or {}
    challenger_return = float((challenger.get("summary") or {}).get("aggregate_cost_after_return") or 0.0)
    best_baseline = max(
        baseline_items,
        key=lambda item: float((item.get("summary") or {}).get("aggregate_cost_after_return") or -999),
        default={},
    )
    baseline_return = float((best_baseline.get("summary") or {}).get("aggregate_cost_after_return") or 0.0)
    benchmark_return = float((challenger.get("summary") or {}).get("aggregate_benchmark_return") or 0.0)
    return {
        "primary_gap": "market_exposure" if benchmark_return - max(challenger_return, baseline_return) > 0.10 else "selection_or_allocation",
        "recommended_next_candidate": "dynamic_market_exposure_with_etf_fallback_v1",
        "evidence": {
            "challenger_return": challenger_return,
            "best_baseline_return": baseline_return,
            "benchmark_return": benchmark_return,
        },
    }


def execute_evolution_task(
    task: dict[str, Any],
    *,
    validation_runner: Callable[..., dict[str, Any]] = run_purged_walk_forward_validation,
) -> dict[str, Any]:
    if task.get("task_type") == "strategy_evolution_cycle":
        return _execute_strategy_cycle(task, validation_runner=validation_runner)
    if task.get("task_type") == "strategy_shadow_cycle":
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
    if task.get("task_type") == "factor_daily_cycle":
        from app.services.factor_platform import run_factor_daily_cycle

        _heartbeat(
            int(task["id"]), phase="factor_materialization", progress=0.05,
            message="正在生产统一版本因子截面",
            checkpoint=task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {},
        )
        return run_factor_daily_cycle()
    if task.get("task_type") == "system_autonomy_cycle":
        from app.services.system_autonomy import run_system_autonomy_cycle

        _heartbeat(
            int(task["id"]), phase="system_autonomy", progress=0.05,
            message="正在运行模型、策略、组合、复盘与研究自治周期",
            checkpoint=task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {},
        )
        return run_system_autonomy_cycle()
    raise ValueError(f"unsupported evolution task type: {task.get('task_type')}")


def _finish_success(task_id: int, result: dict[str, Any]) -> None:
    _update_task(
        task_id, status="completed", phase="completed", progress=1.0,
        message="自进化周期完成", result=result, error=None,
        lease_owner=None, lease_expires_at=None, heartbeat_at=_now().isoformat(),
        finished_at=_now().isoformat(),
    )


def _finish_failure(task: dict[str, Any], exc: Exception) -> None:
    if isinstance(exc, EvolutionTaskCancelled):
        _update_task(
            int(task["id"]), status="cancelled", phase="cancelled", progress=1.0,
            message=str(exc), error=None, lease_owner=None, lease_expires_at=None,
            finished_at=_now().isoformat(),
        )
        return
    attempt = int(task.get("attempt") or 1)
    maximum = int(task.get("max_attempts") or 3)
    if attempt < maximum:
        delay = min(900, 30 * (2 ** max(0, attempt - 1)))
        _update_task(
            int(task["id"]), status="retry_wait", phase="retry_wait",
            message=f"执行失败，{delay} 秒后自动重试", error=str(exc)[:2000],
            next_run_at=(_now() + timedelta(seconds=delay)).isoformat(),
            lease_owner=None, lease_expires_at=None,
        )
    else:
        _update_task(
            int(task["id"]), status="failed", phase="failed", progress=1.0,
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
    except Exception as exc:
        logger.exception("evolution task failed id=%s", task.get("id"))
        _finish_failure(get_evolution_task(int(task["id"])) or task, exc)
    return get_evolution_task(int(task["id"]))


def recover_evolution_tasks() -> int:
    now = _now().isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id FROM evolution_tasks
               WHERE status='running' AND (lease_expires_at IS NULL OR lease_expires_at<?)""",
            (now,),
        ).fetchall()
        for row in rows:
            conn.execute(
                """UPDATE evolution_tasks SET status='queued',phase='recovered',
                   message='服务重启后从检查点恢复',lease_owner=NULL,lease_expires_at=NULL,
                   next_run_at=?,updated_at=? WHERE id=?""",
                (now, now, int(row["id"])),
            )
    return len(rows)


def start_evolution_worker() -> bool:
    global _worker_started
    with _start_lock:
        if _worker_started:
            return False
        _worker_started = True

    def loop() -> None:
        while True:
            if run_worker_once() is None:
                time.sleep(POLL_SECONDS)

    threading.Thread(target=loop, daemon=True, name="evolution-worker").start()
    return True


def start_evolution_scheduler() -> bool:
    global _scheduler_started
    if not AUTO_ENABLED:
        return False
    with _start_lock:
        if _scheduler_started:
            return False
        _scheduler_started = True

    def loop() -> None:
        while True:
            now = _now()
            if now.weekday() < 5 and now.hour >= 16:
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
            if now.weekday() < 5 and now.hour >= 17:
                create_evolution_task(
                    task_type="system_autonomy_cycle",
                    dedupe_key=f"system_autonomy_cycle:{now.date().isoformat()}",
                    payload={"trigger": "daily_scheduler"},
                    priority=70,
                )
            if now.weekday() == AUTO_WEEKDAY and now.hour >= AUTO_HOUR:
                create_evolution_task(
                    task_type="strategy_evolution_cycle",
                    dedupe_key=f"strategy_evolution_cycle:{now.date().isoformat()}",
                    payload={"max_windows": 5, "trigger": "weekly_scheduler"},
                )
            time.sleep(300)

    threading.Thread(target=loop, daemon=True, name="evolution-scheduler").start()
    return True
