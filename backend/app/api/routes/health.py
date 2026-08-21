"""Authenticated business-readiness evidence for the management console."""

from __future__ import annotations

import copy
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from typing import Any, Callable

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.services.data_health import assess_latest_decision_data_health
from app.services.profit_loop import (
    build_execution_chain_slo,
    count_pending_daily_rebalance_execution_inputs,
    get_latest_daily_rebalance_plan,
    list_pending_daily_rebalance_execution_inputs,
)
from app.services.simulation_ledger_audit import audit_simulation_ledger

router = APIRouter()
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")

_READINESS_CACHE_TTL_SECONDS = 8.0
_readiness_cache_lock = threading.Lock()
_readiness_cache: dict[str, tuple[float, int, dict[str, Any]]] = {}


def _read_gate(key: str, label: str, reader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Keep a single broken diagnostic from turning the admin page into a 500."""
    try:
        value = reader()
        return {"key": key, "label": label, "status": "ready", "detail": value, "error": ""}
    except Exception as exc:  # noqa: BLE001 - health must report partial evidence
        return {
            "key": key,
            "label": label,
            "status": "blocked",
            "detail": {},
            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
        }


def _today_ymd() -> str:
    return datetime.now(_BUSINESS_TZ).date().strftime("%Y%m%d")


def _frozen_plan_execution_is_due(plan: dict[str, Any], now: datetime | None = None) -> bool:
    """Return whether a frozen plan has reached its precise execution clock.

    ``planned_execute_date`` identifies the trading session, not the instant at
    which the executor is allowed to claim the order.  Treating the whole date
    as due made the readiness panel report a false execution-data outage from
    midnight until the frozen 15:01 execution time.
    """
    current = now or datetime.now(_BUSINESS_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_BUSINESS_TZ)
    else:
        current = current.astimezone(_BUSINESS_TZ)
    execute_date = str(plan.get("planned_execute_date") or "").replace("-", "")[:8]
    if len(execute_date) != 8 or not execute_date.isdigit():
        return False
    planned_at = str(plan.get("planned_execute_at") or "").strip()
    if planned_at:
        try:
            execution_at = datetime.fromisoformat(planned_at.replace("Z", "+00:00"))
            if execution_at.tzinfo is None:
                execution_at = execution_at.replace(tzinfo=_BUSINESS_TZ)
            return current >= execution_at.astimezone(_BUSINESS_TZ)
        except ValueError:
            # The date is still safe as a conservative compatibility fallback
            # for old persisted plans which predate the exact timestamp.
            pass
    return execute_date < current.strftime("%Y%m%d")


@router.get("/trading-ready")
def trading_readiness(detail: str = Query("full", pattern="^(full|compact)$")) -> JSONResponse:
    """Aggregate the read-only gates that decide whether automation may act."""
    global _readiness_cache
    cache_key = str(detail or "full").strip().lower()
    now = time.monotonic()
    with _readiness_cache_lock:
        cached = _readiness_cache.get(cache_key)
    if cached and now - cached[0] < _READINESS_CACHE_TTL_SECONDS:
        return JSONResponse(status_code=cached[1], content=copy.deepcopy(cached[2]))

    from app.main import _health_payload

    infrastructure = _health_payload()
    # These checks use independent short-lived connections. Running them
    # concurrently keeps one slow diagnostic from serially blocking the
    # whole readiness response, while the connection gate still bounds the
    # total number of database sessions per process.
    with ThreadPoolExecutor(max_workers=5, thread_name_prefix="trading-ready") as executor:
        gate_futures = [
            executor.submit(_read_gate, "decision_data", "最新决策数据", assess_latest_decision_data_health),
            executor.submit(_read_gate, "ledger", "独立账本", lambda: audit_simulation_ledger(persist=False)),
            executor.submit(
                _read_gate,
                "execution_chain",
                "执行链完整性",
                lambda: build_execution_chain_slo(window_plans=20, compact=cache_key == "compact"),
            ),
        ]
        latest_plan_future = executor.submit(
            _read_gate,
            "frozen_plan",
            "正式冻结计划",
            lambda: get_latest_daily_rebalance_plan(compact=cache_key == "compact"),
        )
        pending_future = executor.submit(
            _read_gate,
            "pending_plans",
            "待执行计划",
            (
                (lambda: {"count": count_pending_daily_rebalance_execution_inputs()})
                if cache_key == "compact"
                else (lambda: {"items": list_pending_daily_rebalance_execution_inputs()})
            ),
        )
        gates = [future.result() for future in gate_futures]
        latest_plan = latest_plan_future.result()
        pending = pending_future.result()

    decision_gate = next(item for item in gates if item["key"] == "decision_data")
    ledger_gate = next(item for item in gates if item["key"] == "ledger")
    execution_chain_gate = next(item for item in gates if item["key"] == "execution_chain")
    decision = decision_gate["detail"]
    ledger = ledger_gate["detail"]
    execution_chain = execution_chain_gate["detail"]
    plan = latest_plan["detail"]
    pending_items = (pending["detail"].get("items") or []) if pending["status"] == "ready" else []
    pending_count = (
        int(pending["detail"].get("count") or 0)
        if pending["status"] == "ready" and cache_key == "compact"
        else len(pending_items)
    )
    plan_status = str(plan.get("status") or "not_generated")
    execute_date = str(plan.get("planned_execute_date") or "").replace("-", "")[:8]
    plan_is_due = bool(plan_status == "planned" and _frozen_plan_execution_is_due(plan))
    plan_is_future = bool(plan_status == "planned" and not plan_is_due)
    plan_is_executable = bool(plan_status == "planned" and plan.get("execution_authorized"))

    if latest_plan["status"] != "ready":
        latest_plan["status"] = "blocked"
        plan_status = "diagnostic_error"
    elif plan_status.startswith("blocked_") or plan_status == "missed_execution_window":
        # A new explicit plan blocker must never be forgotten here and then
        # rendered as a healthy waiting plan merely because its code was not
        # in a hand-maintained allowlist.
        latest_plan["status"] = "blocked"
    elif plan_status == "executing":
        latest_plan["status"] = "running"
    elif plan_is_future:
        latest_plan["status"] = "waiting"
    elif plan_is_due and plan_status == "planned":
        # A due plan with no execution claim is not a healthy waiting state.
        # The executor will keep probing its exact T+1 data gate; the console
        # must expose the unresolved execution error instead of hiding it
        # behind a newer plan.
        latest_plan["status"] = "blocked"
    elif plan_status == "planned" and not plan_is_executable:
        latest_plan["status"] = "blocked"
    elif plan_status in {"executed", "superseded", "not_generated"}:
        latest_plan["status"] = "waiting"

    for item in gates:
        detail = item["detail"]
        if item["key"] == "decision_data":
            item["status"] = "ready" if detail.get("decision_ready") else "blocked"
            item["summary"] = "最新截面满足正式决策数据门" if item["status"] == "ready" else "最新截面不能进入正式决策"
        elif item["key"] == "ledger":
            item["status"] = "ready" if detail.get("ok") else "blocked"
            item["summary"] = "独立重算与账户投影一致" if item["status"] == "ready" else "账本存在漂移或非法记录"
        else:
            status = str(detail.get("status") or "unknown")
            current_blocking = bool(detail.get("current_blocking"))
            item["status"] = "blocked" if current_blocking else "observing" if status in {"observing", "failed", "error"} else "ready"
            if item["status"] == "ready":
                item["summary"] = "执行计划与成交绑定可审计"
            elif current_blocking:
                item["summary"] = "执行链存在当前未解决异常"
            elif status in {"failed", "error"}:
                item["summary"] = "历史执行质量未达标，但当前没有卡住的执行，不阻断当前运行"
            else:
                item["summary"] = "样本仍在积累，不阻断当前运行"

    infrastructure_ready = bool(infrastructure.get("ready"))
    hard_blockers: list[str] = []
    if not infrastructure_ready:
        hard_blockers.extend(infrastructure.get("issues") or ["基础设施未就绪"])
    if decision_gate["status"] != "ready":
        hard_blockers.append("最新决策数据未通过硬门")
    if ledger_gate["status"] != "ready":
        hard_blockers.append("独立账本未通过一致性审计")
    if latest_plan["status"] == "blocked":
        hard_blockers.append(
            str(plan.get("blocked_reason") or (
                "到期冻结计划的执行行情未就绪，执行器正在持续重试"
                if plan_is_due and plan_status == "planned"
                else "正式冻结计划不可执行"
            ))
        )
    if execution_chain_gate["status"] == "blocked":
        hard_blockers.append("执行链审计不可用")
    if pending["status"] != "ready":
        hard_blockers.append("待执行计划状态不可读取")

    if hard_blockers:
        status = "blocked"
        summary = "存在硬阻断，自动交易不得继续"
    elif latest_plan["status"] in {"waiting", "running"} or pending_count == 0:
        status = "waiting"
        summary = "系统健康，等待下一份到期冻结计划"
    else:
        status = "ready"
        summary = "当前可按冻结计划运行模拟执行"

    response = {
        "schema_version": "alis_trading_readiness_v1",
        "checked_at": datetime.now(_BUSINESS_TZ).isoformat(timespec="seconds"),
        "status": status,
        "trading_ready": status == "ready",
        "summary": summary,
        "hard_blockers": hard_blockers,
        "today": _today_ymd(),
        "layers": {
            "process": {"status": "alive", "detail": "进程响应正常"},
            "infrastructure": {
                "status": "ready" if infrastructure_ready else "blocked",
                "detail": infrastructure,
            },
            "trading": {"status": status, "detail": summary},
        },
        "gates": gates + [
            {
                "key": "frozen_plan",
                "label": "正式冻结计划",
                "status": latest_plan["status"],
                "summary": {
                    "planned": "已到冻结执行时刻，等待冻结执行数据；执行器持续重试" if plan_is_due else "冻结计划尚未到执行时刻",
                    "executing": "已有执行租约正在处理",
                    "executed": "最近冻结计划已完成",
                    "not_generated": "当前没有冻结计划",
                }.get(plan_status, str(plan.get("blocked_reason") or plan_status)),
                "detail": plan,
            },
            {
                "key": "execution_lease",
                "label": "执行租约",
                "status": "running" if plan_status == "executing" else "ready",
                "summary": "已有唯一执行器持有租约" if plan_status == "executing" else "没有卡住的执行租约",
                "detail": {
                    "plan_id": plan.get("id"),
                    "status": plan_status,
                    "execution_claimed_at": plan.get("execution_claimed_at"),
                    "execution_id": plan.get("execution_id"),
                },
            },
            {
                "key": "pending_plan",
                "label": "待执行计划",
                "status": "ready" if pending_count else "waiting",
                "summary": f"{pending_count} 份计划等待执行" if pending_count else "当前没有待执行计划，不视为故障",
                "detail": {"count": pending_count, "items": pending_items},
            },
        ],
        "plan": plan,
        "execution_chain": execution_chain,
        "policy": {
            "read_only": True,
            "page_reads_do_not_create_plans_or_trades": True,
            "waiting_without_a_due_plan_is_not_a_failure": True,
            "blocked_status_is_fail_closed": True,
        },
    }
    status_code = 503 if status == "blocked" else 200
    if cache_key == "compact":
        response = {
            "schema_version": response["schema_version"],
            "detail": "compact",
            "checked_at": response["checked_at"],
            "status": response["status"],
            "trading_ready": response["trading_ready"],
            "summary": response["summary"],
            "hard_blockers": response["hard_blockers"],
            "today": response["today"],
            "layers": {
                key: {
                    "status": value.get("status"),
                    "detail": value.get("detail"),
                }
                for key, value in (response.get("layers") or {}).items()
            },
            "gates": [
                {
                    "key": item.get("key"),
                    "label": item.get("label"),
                    "status": item.get("status"),
                    "summary": item.get("summary"),
                }
                for item in response.get("gates") or []
            ],
            "policy": response["policy"],
        }
    with _readiness_cache_lock:
        _readiness_cache[cache_key] = (time.monotonic(), status_code, copy.deepcopy(response))
    return JSONResponse(status_code=status_code, content=response)
