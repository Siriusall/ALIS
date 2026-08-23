"""Durable, page-scoped read snapshots for the shared ALIS console.

PostgreSQL is the authority: every successful build is committed there first.
Redis only mirrors the latest document briefly so repeated browser opens avoid
both PostgreSQL and the expensive aggregation work.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Callable

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.data_platform.redis_store import delete as redis_delete
from app.data_platform.redis_store import get_json, set_json
from app.db.store import _conn


PAGE_KEYS = ("overview", "decision", "portfolio", "operations", "data", "models", "research", "factors", "evidence")
_DEFAULT_TTLS = {
    "overview": 30,
    "decision": 30,
    "portfolio": 30,
    "operations": 15,
    "data": 60,
    "models": 300,
    "research": 120,
    "factors": 120,
    "evidence": 60,
}
_FAILURE_RETRY_SECONDS = 15
_schema_ready = False
_schema_lock = Lock()
logger = logging.getLogger(__name__)


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


def _section(page_key: str, name: str, reader: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
    try:
        value = _as_json(reader())
        unhealthy = isinstance(value, dict) and (
            value.get("ok") is False
            or value.get("status") in {"blocked", "error", "failed", "degraded", "unavailable", "warming_up"}
        )
        return value, {"status": "degraded" if unhealthy else "ready", "error": ""}
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


def _readers(page_key: str) -> dict[str, Callable[[], Any]]:
    # Imports are intentionally lazy: this service is used from startup paths
    # and must not create a main.py import cycle.
    from app.api.routes import health, model_strategy, quant_runs, research
    from app.api.routes.decision import decision_latest_plan, decision_summary
    from app.api.routes.positions import get_positions
    from app.data_platform.routes import data_center
    from app.main import _health_payload

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

    common = {
        # The control worker builds durable PostgreSQL snapshots. It must not
        # inherit the API host's loopback Redis requirement: browser reads and
        # stream consumption remain checked by the API's normal health route.
        "health": lambda: _health_payload(require_redis=False),
        "tradingReady": control_trading_ready,
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
            "oos": lambda: data_center.data_center_oos_hard_data(include_dates=False),
            "sealed": lambda: data_center.data_center_sealed_strategy_holdout(include_dates=False),
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
            **common,
            "owner": model_strategy.model_strategy_owner_summary,
            "tasks": lambda: model_strategy.model_strategy_task_center(limit=16, log_limit=40, compact=True),
            "evolutionTasks": lambda: model_strategy.model_strategy_evolution_tasks(limit=30),
            "evolutionSchedule": model_strategy.model_strategy_evolution_schedule,
            "quantTasks": lambda: quant_runs.quant_run_tasks(limit=20),
            "quantStatus": quant_runs.quant_run_status,
            "quantRecent": lambda: quant_runs.quant_run_recent(limit=10),
            "operationsPage": lambda: __import__("app.api.routes.positions", fromlist=["get_operations_page"]).get_operations_page(execution_id=None, limit=20, offset=0),
            "executionsPage": lambda: __import__("app.api.routes.positions", fromlist=["get_executions_page"]).get_executions_page(limit=20, offset=0),
            "ledgerAudit": model_strategy.model_strategy_ledger_audit,
            "oos": lambda: data_center.data_center_oos_hard_data(include_dates=False),
            "sealed": lambda: data_center.data_center_sealed_strategy_holdout(include_dates=False),
        },
        "data": {
            "watermarks": data_center.data_center_asset_watermarks,
            "failedRequests": lambda: data_center.data_center_failed_tushare_requests(request_status="pending", limit=100),
            "dataOperations": lambda: data_center.data_center_llm_data_operations(limit=20),
            "runtimeLogs": lambda: data_center.data_center_runtime_logs(after_id=0, limit=100, wait_seconds=0.0),
            "dataObservability": lambda: data_center.data_center_observability(limit_logs=50, compact=True),
            "dataStreams": lambda: data_center.data_center_streams(limit=50, pending_limit=50),
            "dailyUpdateStatus": lambda: data_center.data_center_daily_update_status(compact=True),
            "backfillStatus": data_center.data_center_backfill_status,
            "backfillPlan": lambda: data_center.data_center_backfill_plan(
                market_batch_days=30,
                financial_batch_size=100,
                rebuild=False,
            ),
            "tasks": lambda: model_strategy.model_strategy_task_center(limit=16, log_limit=40, compact=True),
            "oos": lambda: data_center.data_center_oos_hard_data(include_dates=False),
            "sealed": lambda: data_center.data_center_sealed_strategy_holdout(include_dates=False),
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
            "theses": research.research_thesis_list,
            "industries": lambda: research.research_industries(compact=True),
            "industryAllocation": lambda: research.research_industry_allocation(compact=True),
            "themes": research.research_themes,
            "events": research.research_events,
            "pools": research.research_pools,
            "modelMarketLive": model_strategy.model_strategy_live_market_context,
        },
        "factors": {
            "owner": model_strategy.model_strategy_owner_summary,
            "strategy": model_strategy.model_strategy_strategy_controller,
            "sealed": lambda: data_center.data_center_sealed_strategy_holdout(include_dates=False),
            "factors": lambda: __import__("app.api.routes.factors", fromlist=["factor_registry"]).factor_registry(),
            "factorStatus": lambda: __import__("app.api.routes.factors", fromlist=["factor_research_status"]).factor_research_status(),
            "factorResearch": lambda: __import__("app.api.routes.factors", fromlist=["factor_research_status"]).factor_research_status(console=True, compact=True),
            "factorLineage": lambda: __import__("app.api.routes.factors", fromlist=["factor_strategy_lineage"]).factor_strategy_lineage(),
            "evolutionTasks": lambda: model_strategy.model_strategy_evolution_tasks(limit=30),
        },
        "evidence": {
            "decisionAudit": research.research_decision_audit,
            "decisions": lambda: decision_summary(limit_days=30),
            "reviews": lambda: research.research_reviews(limit=100),
            "reviewLearning": research.research_review_learning_overview,
            "llmBudget": research.research_llm_token_budget,
            "llmProposals": lambda: research.research_llm_proposals(limit=50),
            "performanceReports": lambda: quant_runs.quant_run_performance_reports(limit=30),
            "ledgerDetail": lambda: quant_runs.quant_run_simulation_ledger(limit=20),
            "reportCompact": lambda: decision_summary(limit_days=30),
            "backtests": lambda: quant_runs.quant_run_performance_reports(limit=30),
            "ledgerAudit": model_strategy.model_strategy_ledger_audit,
        },
    }
    return pages[page_key]


def _serialize(payload: dict[str, Any]) -> str:
    return json.dumps(jsonable_encoder(payload), ensure_ascii=False, separators=(",", ":"))


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
    with _conn() as conn:
        row = conn.execute(
            "SELECT page_key,version,generated_at,as_of,status,invalidated_at,updated_at,"
            "refresh_generation,last_attempt_generation,last_attempt_at,last_error_code "
            "FROM console_page_snapshots WHERE page_key=?",
            (page_key,),
        ).fetchone()
    return dict(row) if row else None


def _load_durable(page_key: str) -> dict[str, Any] | None:
    _ensure_table()
    with _conn() as conn:
        row = conn.execute(
            "SELECT page_key,version,generated_at,as_of,status,invalidated_at,updated_at,"
            "refresh_generation,last_attempt_generation,last_attempt_at,last_error_code,payload_json "
            "FROM console_page_snapshots WHERE page_key=?",
            (page_key,),
        ).fetchone()
    if not row:
        return None
    record = dict(row)
    try:
        payload = json.loads(record.get("payload_json") or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
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
            "SELECT page_key,version,status,invalidated_at,refresh_generation "
            "FROM console_page_snapshots WHERE page_key=?",
            (page_key,),
        ).fetchone()
    if not row:
        raise RuntimeError("console_snapshot_record_unavailable")
    return dict(row)


def _save_durable(page_key: str, payload: dict[str, Any], expected_generation: int) -> bool:
    """Commit only if no invalidation happened while the readers were running."""
    _ensure_table()
    serialized = _serialize(payload)
    with _conn() as conn:
        row = conn.execute(
            """UPDATE console_page_snapshots SET
                    version=?, generated_at=?, as_of=?, status=?, invalidated_at=NULL,
                    payload_json=?, updated_at=?, last_attempt_generation=-1,
                    last_attempt_at=NULL, last_error_code=''
                WHERE page_key=? AND refresh_generation=?
                RETURNING refresh_generation""",
            (
                payload["version"],
                payload["generated_at"],
                payload["as_of"],
                payload["status"],
                serialized,
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
    _ensure_table()
    now = _now()
    invalidated: list[str] = []
    with _conn() as conn:
        for page_key in page_keys:
            if page_key not in PAGE_KEYS:
                continue
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


def _parse_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


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
        key=lambda page_key: 0 if (by_page.get(page_key) or {}).get("invalidated_at") else 1,
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


def run_snapshot_worker_forever(stop_event: Any | None = None) -> None:
    """Run only on the control worker; serialize expensive page aggregation."""
    stopper = stop_event
    while stopper is None or not stopper.is_set():
        try:
            due = _pages_due_for_refresh()
        except Exception as exc:  # database failures must not involve the API host
            logger.warning("Console snapshot due scan unavailable error_type=%s", type(exc).__name__)
            if _wait_or_stop(stopper, 5.0):
                return
            continue
        for page_key in due:
            if stopper is not None and stopper.is_set():
                return
            try:
                build(page_key)
            except Exception as exc:  # retain any LKG; the next scan retries safely
                logger.warning(
                    "Console snapshot build unavailable page=%s error_type=%s",
                    page_key,
                    type(exc).__name__,
                )
            # Never fan out expensive aggregations across the small DB.
            if _wait_or_stop(stopper, 1.0):
                return
        if not due and _wait_or_stop(stopper, 1.0):
            return


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
    }
    if errors:
        _record_failed_build(page_key, expected_generation, payload)
        return payload
    # The application host owns Redis. It mirrors this committed PostgreSQL
    # version only after its next read, so a worker never writes remote cache.
    _save_durable(page_key, payload, expected_generation)
    return payload


def _cache_matches_metadata(cached: dict[str, Any], metadata: dict[str, Any]) -> bool:
    return (
        _has_committed_snapshot(metadata)
        and not metadata.get("invalidated_at")
        and str(cached.get("version") or "") == str(metadata.get("version") or "")
        and _generation(cached) == _generation(metadata)
    )


def read(page_key: str, *, force: bool = False) -> dict[str, Any]:
    if page_key not in PAGE_KEYS:
        raise ValueError(f"unsupported console page: {page_key}")
    metadata = _snapshot_metadata(page_key)
    cached = None if force else get_json(_redis_key(page_key))
    if isinstance(cached, dict) and metadata and _cache_matches_metadata(cached, metadata):
        return cached
    if isinstance(cached, dict):
        redis_delete(_redis_key(page_key))
    generation = _generation(metadata)
    durable = _load_durable(page_key) if metadata else None
    if metadata and _has_committed_snapshot(metadata) and durable:
        response = copy.deepcopy(durable)
        response["stale"] = bool(metadata.get("invalidated_at"))
        response["refreshing"] = response["stale"]
        if not response["stale"]:
            set_json(_redis_key(page_key), response, ttl=min(15, _ttl(page_key)))
        return response
    if durable and str(durable.get("status") or "") == "unavailable":
        durable["stale"] = False
        durable["refreshing"] = True
        return durable
    return _warming_payload(page_key, generation)


def status() -> dict[str, Any]:
    _ensure_table()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT page_key,version,generated_at,as_of,status,invalidated_at,updated_at,"
            "refresh_generation,last_attempt_generation,last_attempt_at,last_error_code "
            "FROM console_page_snapshots ORDER BY page_key"
        ).fetchall()
    return {"ok": True, "items": [dict(row) for row in rows], "pages": list(PAGE_KEYS)}
