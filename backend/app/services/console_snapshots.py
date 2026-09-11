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
import time
import uuid
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Callable

from fastapi.encoders import jsonable_encoder
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


def _section(page_key: str, name: str, reader: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
    try:
        value = _as_json(reader())
        # Route payloads can correctly describe a blocked model, historical
        # failure, or partial research evidence while still carrying a complete
        # readable document.  A page snapshot is degraded only when the read
        # itself failed or the endpoint explicitly says no data is available.
        unavailable = isinstance(value, dict) and value.get("data_available") is False and not _has_readable_evidence(value)
        return value, {"status": "degraded" if unavailable else "ready", "error": ""}
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
        "health": lambda: _health_payload(require_redis=False),
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
            "dataStreams": lambda: data_center.data_center_streams(limit=50, pending_limit=50),
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


def _control_work_active() -> bool:
    """Do not make page aggregation compete with database-heavy autonomy."""

    with _read_conn() as conn:
        placeholders = ",".join("?" for _ in _DB_HEAVY_TASK_TYPES)
        row = conn.execute(
            f"""SELECT 1 AS active
                 FROM evolution_tasks
                WHERE status='running'
                  AND (
                    COALESCE(worker_pool,'control')='control'
                    OR task_type IN ({placeholders})
                  )
                LIMIT 1""",
            tuple(sorted(_DB_HEAVY_TASK_TYPES)),
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


def _snapshot_build_should_yield() -> bool:
    return (
        not optional_network_work_allowed()
        or _control_work_active()
        or _data_ingestion_active()
    )


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
                if page_key != "runtime" and _snapshot_build_should_yield():
                    break
            except Exception as exc:
                logger.warning(
                    "Console snapshot workload gate unavailable error_type=%s",
                    type(exc).__name__,
                )
                break
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
        # A workload-gate break leaves due pages in the queue. Without a wait
        # this spins on PostgreSQL continuously while research owns the DB.
        if _wait_or_stop(stopper, 5.0 if due else 1.0):
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
        return _with_runtime_overlay(page_key, response)
    if isinstance(cached, dict):
        redis_delete(_redis_key(page_key))
    generation = _generation(metadata)
    durable = _load_durable(page_key) if metadata else None
    if metadata and _has_committed_snapshot(metadata) and durable:
        response = copy.deepcopy(durable)
        response["stale"] = expired or bool(metadata.get("invalidated_at"))
        response["refreshing"] = response["stale"]
        # Expiry affects freshness, not content identity. Mirror the committed
        # version while waiting, avoiding repeated cross-node payload transfers.
        if not metadata.get("invalidated_at"):
            set_json(_redis_key(page_key), response, ttl=_app_cache_ttl())
        return _with_runtime_overlay(page_key, response)
    if durable and str(durable.get("status") or "") == "unavailable":
        durable["stale"] = False
        durable["refreshing"] = True
        return _with_runtime_overlay(page_key, durable)
    return _with_runtime_overlay(page_key, _warming_payload(page_key, generation))


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
    return {
        "ok": True,
        "items": items,
        "pages": list(PAGE_KEYS),
        "payload_size_bytes": sum(int(item.get("payload_size_bytes") or 0) for item in items),
        "compressed_size_bytes": sum(int(item.get("compressed_size_bytes") or 0) for item in items),
    }
