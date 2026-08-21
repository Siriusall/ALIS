"""Durable, page-scoped read snapshots for the shared ALIS console.

PostgreSQL is the authority: every successful build is committed there first.
Redis only mirrors the latest document briefly so repeated browser opens avoid
both PostgreSQL and the expensive aggregation work.
"""

from __future__ import annotations

import copy
import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi.responses import JSONResponse

from app.data_platform.redis_store import delete as redis_delete
from app.data_platform.redis_store import get_json, set_json
from app.db.store import _conn


PAGE_KEYS = ("overview", "decision", "portfolio", "operations", "data", "models")
_DEFAULT_TTLS = {
    "overview": 30,
    "decision": 30,
    "portfolio": 30,
    "operations": 15,
    "data": 60,
    "models": 300,
}
_schema_ready = False
from threading import Lock

_schema_lock = Lock()


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


def _ensure_table() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with _conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS console_page_snapshots (
                    page_key VARCHAR(32) PRIMARY KEY,
                    version VARCHAR(64) NOT NULL,
                    generated_at VARCHAR(64) NOT NULL,
                    as_of VARCHAR(64) NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    invalidated_at VARCHAR(64),
                    payload_json TEXT NOT NULL,
                    updated_at VARCHAR(64) NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_console_page_snapshots_updated "
                "ON console_page_snapshots(updated_at DESC)"
            )
        _schema_ready = True


def _as_json(value: Any) -> Any:
    if isinstance(value, JSONResponse):
        return json.loads(value.body.decode("utf-8"))
    return value


def _section(name: str, reader: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
    try:
        value = _as_json(reader())
        unhealthy = isinstance(value, dict) and (
            value.get("ok") is False or value.get("status") in {"blocked", "error", "failed", "degraded"}
        )
        return value, {"status": "degraded" if unhealthy else "ready", "error": ""}
    except Exception as exc:  # a page can surface partial evidence safely
        return {"ok": False, "status": "unavailable", "error": str(exc)[:300]}, {
            "status": "error", "error": f"{type(exc).__name__}: {str(exc)[:240]}",
        }


def _readers(page_key: str) -> dict[str, Callable[[], Any]]:
    # Imports are intentionally lazy: this service is used from startup paths
    # and must not create a main.py import cycle.
    from app.api.routes import health, model_strategy, quant_runs, research
    from app.api.routes.decision import decision_latest_plan, decision_summary
    from app.api.routes.positions import get_positions
    from app.data_platform.routes import data_center
    from app.main import _health_payload

    common = {
        "health": _health_payload,
        "tradingReady": lambda: health.trading_readiness(detail="compact"),
    }
    pages: dict[str, dict[str, Callable[[], Any]]] = {
        "overview": {
            **common,
            "owner": model_strategy.model_strategy_owner_summary,
            "ledger": quant_runs.quant_run_ledger_summary,
            "portfolio": lambda: research.research_portfolio_candles(limit=90),
            "latestPlan": lambda: decision_latest_plan(compact=True),
            "adminTasksLive": lambda: model_strategy.model_strategy_task_center(limit=30, log_limit=20, compact=True),
            "dailyUpdateStatus": lambda: data_center.data_center_daily_update_status(compact=True),
            "watermarks": data_center.data_center_asset_watermarks,
            "ledgerAudit": model_strategy.model_strategy_ledger_audit,
            "oos": data_center.data_center_oos_hard_data,
            "sealed": data_center.data_center_sealed_strategy_holdout,
        },
        "decision": {
            **common,
            "owner": model_strategy.model_strategy_owner_summary,
            "decision": decision_summary,
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
            **common,
            "owner": model_strategy.model_strategy_owner_summary,
            "tasks": lambda: model_strategy.model_strategy_task_center(limit=16, log_limit=40, compact=True),
            "evolutionTasks": lambda: model_strategy.model_strategy_evolution_tasks(limit=30),
            "evolutionSchedule": model_strategy.model_strategy_evolution_schedule,
            "quantTasks": lambda: quant_runs.quant_run_tasks(limit=20),
            "quantStatus": quant_runs.quant_run_status,
            "quantRecent": lambda: quant_runs.quant_run_recent(limit=10),
            "operationsPage": lambda: __import__("app.api.routes.positions", fromlist=["get_operations_page"]).get_operations_page(limit=20, offset=0),
            "executionsPage": lambda: __import__("app.api.routes.positions", fromlist=["get_executions_page"]).get_executions_page(limit=20, offset=0),
            "ledgerAudit": model_strategy.model_strategy_ledger_audit,
            "oos": data_center.data_center_oos_hard_data,
            "sealed": data_center.data_center_sealed_strategy_holdout,
        },
        "data": {
            "watermarks": data_center.data_center_asset_watermarks,
            "failedRequests": data_center.data_center_failed_tushare_requests,
            "dataOperations": data_center.data_center_llm_data_operations,
            "runtimeLogs": lambda: data_center.data_center_runtime_logs(limit=100),
            "dataObservability": lambda: data_center.data_center_observability(compact=True),
            "dataStreams": data_center.data_center_streams,
            "dailyUpdateStatus": lambda: data_center.data_center_daily_update_status(compact=True),
            "backfillStatus": data_center.data_center_backfill_status,
            "backfillPlan": data_center.data_center_backfill_plan,
            "tasks": lambda: model_strategy.model_strategy_task_center(limit=16, log_limit=40, compact=True),
            "oos": data_center.data_center_oos_hard_data,
            "sealed": data_center.data_center_sealed_strategy_holdout,
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
            "factorResearchCompact": lambda: __import__("app.api.routes.factors", fromlist=["factor_research_status"]).factor_research_status(compact=True),
            "evolutionTasks": lambda: model_strategy.model_strategy_evolution_tasks(limit=30),
            "modelTasksLive": lambda: model_strategy.model_strategy_task_center(limit=24, log_limit=40, compact=True),
        },
    }
    return pages[page_key]


def _load_durable(page_key: str) -> dict[str, Any] | None:
    _ensure_table()
    with _conn() as conn:
        row = conn.execute(
            "SELECT payload_json, invalidated_at FROM console_page_snapshots WHERE page_key=?", (page_key,)
        ).fetchone()
    if not row:
        return None
    value = dict(row).get("payload_json")
    try:
        payload = json.loads(value) if value else None
        if isinstance(payload, dict):
            payload["_invalidated"] = bool(dict(row).get("invalidated_at"))
        return payload
    except (TypeError, ValueError):
        return None


def _save_durable(page_key: str, payload: dict[str, Any]) -> None:
    _ensure_table()
    serialized = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
    with _conn() as conn:
        conn.execute("""
            INSERT INTO console_page_snapshots
                (page_key, version, generated_at, as_of, status, invalidated_at, payload_json, updated_at)
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            ON CONFLICT (page_key) DO UPDATE SET
                version=EXCLUDED.version, generated_at=EXCLUDED.generated_at, as_of=EXCLUDED.as_of,
                status=EXCLUDED.status, invalidated_at=NULL, payload_json=EXCLUDED.payload_json,
                updated_at=EXCLUDED.updated_at
        """, (
            page_key, payload["version"], payload["generated_at"], payload["as_of"],
            payload["status"], serialized, payload["generated_at"],
        ))


def invalidate(page_keys: list[str] | tuple[str, ...] = PAGE_KEYS, *, reason: str = "source_changed") -> None:
    _ensure_table()
    now = _now()
    with _conn() as conn:
        for page_key in page_keys:
            if page_key in PAGE_KEYS:
                conn.execute("UPDATE console_page_snapshots SET invalidated_at=? WHERE page_key=?", (now, page_key))
                redis_delete(_redis_key(page_key))


def _pages_due_for_refresh() -> list[str]:
    """Use PostgreSQL state as the cross-node refresh protocol.

    Redis is private to the application host, so it must not coordinate a
    worker on another machine.  Invalidated timestamps and committed snapshot
    times are durable and safely visible from the control worker.
    """
    _ensure_table()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT page_key, generated_at, invalidated_at FROM console_page_snapshots"
        ).fetchall()
    by_page = {str(dict(row).get("page_key")): dict(row) for row in rows}
    now = datetime.now(timezone.utc)
    due: list[str] = []
    for page_key in PAGE_KEYS:
        row = by_page.get(page_key)
        if not row or row.get("invalidated_at"):
            due.append(page_key)
            continue
        try:
            generated_at = datetime.fromisoformat(str(row.get("generated_at") or "").replace("Z", "+00:00"))
            if generated_at.tzinfo is None:
                generated_at = generated_at.replace(tzinfo=timezone.utc)
            if (now - generated_at.astimezone(timezone.utc)).total_seconds() >= _ttl(page_key):
                due.append(page_key)
        except ValueError:
            due.append(page_key)
    return due


def run_snapshot_worker_forever(stop_event: Any | None = None) -> None:
    """Run only on the control worker; serializes all page aggregation."""
    stopper = stop_event
    while stopper is None or not stopper.is_set():
        try:
            due = _pages_due_for_refresh()
            for page_key in due:
                if stopper is not None and stopper.is_set():
                    return
                build(page_key)
                # Never fan out expensive aggregations across the small DB.
                time.sleep(1.0)
            if not due:
                time.sleep(1.0)
        except Exception:
            # Database unavailability leaves the last committed snapshot in
            # place. Retry slowly without involving the HTTP process.
            time.sleep(5.0)


def build(page_key: str) -> dict[str, Any]:
    if page_key not in PAGE_KEYS:
        raise ValueError(f"unsupported console page: {page_key}")
    readers = _readers(page_key)
    data: dict[str, Any] = {}
    sections: dict[str, dict[str, Any]] = {}
    for key, reader in readers.items():
        data[key], sections[key] = _section(key, reader)
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
        "sections": sections,
        "data": data,
    }
    _save_durable(page_key, payload)
    # The application host owns Redis.  Do not write a worker-local cache from
    # this cross-node process; the next browser read mirrors the PostgreSQL
    # authority into the application Redis instance.
    return payload


def read(page_key: str, *, force: bool = False) -> dict[str, Any]:
    if page_key not in PAGE_KEYS:
        raise ValueError(f"unsupported console page: {page_key}")
    if not force:
        cached = get_json(_redis_key(page_key))
        if isinstance(cached, dict):
            return cached
    durable = _load_durable(page_key)
    if durable:
        # A browser never performs the rebuild. It gets the last committed
        # authority while the singleton scheduler handles the refresh.
        redis_payload = copy.deepcopy(durable)
        redis_payload["stale"] = bool(durable.pop("_invalidated", False))
        redis_payload["refreshing"] = redis_payload["stale"]
        set_json(_redis_key(page_key), redis_payload, ttl=min(15, _ttl(page_key)))
        return redis_payload
    return {
        "page": page_key,
        "version": "",
        "generated_at": "",
        "as_of": "",
        "status": "warming_up",
        "stale": False,
        "refreshing": True,
        "sections": {},
        "data": {},
        "error": "No committed page snapshot exists yet; background warm-up is queued.",
    }


def status() -> dict[str, Any]:
    _ensure_table()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT page_key, version, generated_at, as_of, status, invalidated_at, updated_at "
            "FROM console_page_snapshots ORDER BY page_key"
        ).fetchall()
    return {"ok": True, "items": [dict(row) for row in rows], "pages": list(PAGE_KEYS)}
