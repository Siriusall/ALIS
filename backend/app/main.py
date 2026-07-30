import logging
import os
import sys
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import (
    account,
    auth,
    backtest,
    candidates,
    check,
    decision,
    equity,
    factors,
    execute,
    init_routes,
    logs,
    model_strategy,
    overview,
    positions,
    quant_runs,
    research,
    signals,
    strategies,
)
from app.core.config import settings
from app.core.error_evidence import error_evidence
from app.db.store import database_health as primary_database_health, init_db
from app.services.quant_task_runner import (
    ensure_autonomous_simulation_task,
    resume_quant_tasks_on_startup,
    start_auto_simulation_scheduler,
)
from app.data_platform.services.daily_update import start_daily_update_scheduler
from app.services.manual_review_triage import start_manual_review_triage_scheduler
from app.services.evolution_orchestrator import (
    evolution_runtime_status,
    mark_evolution_component_disabled,
    recover_evolution_tasks,
    start_evolution_scheduler,
    start_evolution_worker,
)
from app.services.production_strategy import ensure_production_strategy_champion
from app.services.plan_model_control import reconcile_plan_model_production_gates
from app.services.profit_loop import recover_interrupted_daily_rebalance_plans
from app.services.runtime_boundary import BackgroundRuntimeLease, acquire_background_runtime_lease
from app.data_platform.db import database_health as data_platform_database_health
from app.data_platform.db import init_db as init_data_platform_db
from app.data_platform.redis_store import start_task_worker
from app.data_platform.redis_store import health as redis_health
from app.data_platform.routes.data_center import router as data_center_router
from app.data_platform.services.backfill import process_stream_task, recover_interrupted_backfill
from app.data_platform.services.stream_admin import auto_heal_stream
from app.data_platform.services.data_center import recover_orphaned_ingestion_runs
from app.data_platform.services.runtime_logs import write_runtime_log
from app.services.runtime_heartbeat import status as runtime_heartbeat_status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("quant")
_startup_components: dict[str, dict] = {}
_background_runtime_lease: BackgroundRuntimeLease | None = None


def _env_enabled(name: str, default: bool = True) -> bool:
    fallback = "1" if default else "0"
    return str(os.getenv(name, fallback)).strip().lower() in {"1", "true", "yes", "on"}


def _start_component(name: str, fn) -> object | None:
    try:
        result = fn()
        _startup_components[name] = {"status": "started", "error": "", "result": result}
        return result
    except Exception as exc:  # noqa: BLE001 - one subsystem must not suppress the rest
        evidence = error_evidence(
            exc,
            operation=f"startup:{name}",
            source="fastapi_lifespan",
        )
        _startup_components[name] = {
            "status": "failed",
            "error": evidence["message"],
            "error_evidence": evidence,
        }
        logger.exception("Startup component failed: %s", name)
        return None

def startup() -> None:
    """Keep service startup light.

    Heavy market-data refresh is now explicit via POST /api/init, so /api/health
    and the dashboard can come up even when TuShare or local caches are not ready.
    """

    global _background_runtime_lease
    startup_t0 = time.perf_counter()
    _startup_components.clear()
    logger.info("Starting service: initializing database schema only.")
    init_db()
    init_data_platform_db()
    _background_runtime_lease = acquire_background_runtime_lease()
    _startup_components["background_runtime_boundary"] = {
        "status": "started",
        "error": "",
        "result": _background_runtime_lease.status(),
    }
    if not _background_runtime_lease.should_start:
        logger.info("Background runtime not started: %s", _background_runtime_lease.reason)
        logger.info("Web API startup completed in %.3fs.", time.perf_counter() - startup_t0)
        return

    _start_component(
        "plan_model_production_gate_reconciliation",
        reconcile_plan_model_production_gates,
    )
    champion = ensure_production_strategy_champion()
    logger.info("Active ALIS production Champion: %s", champion.get("version"))
    autonomous_task = _start_component("autonomous_simulation_task", ensure_autonomous_simulation_task)
    if isinstance(autonomous_task, dict):
        logger.info(
            "Autonomous simulation task ready: task_id=%s created=%s",
            ((autonomous_task.get("task") or {}).get("id")),
            autonomous_task.get("created"),
        )
    paused = _start_component("quant_task_recovery", resume_quant_tasks_on_startup)
    if isinstance(paused, int) and paused > 0:
        logger.info("Paused interrupted quant tasks on startup: %d", paused)
    _start_component("rebalance_execution_recovery", recover_interrupted_daily_rebalance_plans)
    _start_component("auto_simulation_scheduler", start_auto_simulation_scheduler)
    _start_component("daily_update_scheduler", start_daily_update_scheduler)
    _startup_components["legacy_model_governance_scheduler"] = {
        "status": "retired",
        "error": "",
        "result": {
            "reason": "replaced_by_task_native_five_model_governance",
            "production_change_forbidden": True,
        },
    }
    _start_component("manual_review_scheduler", start_manual_review_triage_scheduler)
    recovered_evolution = _start_component("evolution_recovery", recover_evolution_tasks)
    if _env_enabled("ALIS_EVOLUTION_WORKER_ENABLED"):
        _start_component("evolution_worker", start_evolution_worker)
    else:
        mark_evolution_component_disabled("worker", reason="external_worker")
        _startup_components["evolution_worker"] = {
            "status": "disabled",
            "error": "",
            "result": {"reason": "external_worker"},
        }
    _start_component("evolution_scheduler", start_evolution_scheduler)
    if isinstance(recovered_evolution, int) and recovered_evolution:
        logger.info("Recovered interrupted evolution tasks: %d", recovered_evolution)
    try:
        recovery = recover_interrupted_backfill()
        orphaned = recover_orphaned_ingestion_runs()
        cleaned = auto_heal_stream(limit=1000)
        start_task_worker(process_stream_task)
        if recovery.get("recovered"):
            write_runtime_log("WARNING", "backfill", "检测到服务重启中断的补数任务，已转换为可续跑状态", recovery)
        if orphaned.get("recovered"):
            write_runtime_log("WARNING", "data-platform", "检测到服务重启遗留采集任务，已转换为可续跑状态", orphaned)
        if cleaned.get("cleanup", {}).get("acked") or cleaned.get("trim", {}).get("trimmed"):
            write_runtime_log("INFO", "backfill", "已自动确认过期补全队列消息", cleaned)
        write_runtime_log("INFO", "data-platform", "统一后端中的数据中台已启动")
    except Exception as exc:  # noqa: BLE001
        evidence = error_evidence(
            exc,
            operation="startup:data_platform_worker",
            source="fastapi_lifespan",
            deferred=True,
        )
        _startup_components["data_platform_worker"] = {
            "status": "failed",
            "error": evidence["message"],
            "error_evidence": evidence,
        }
        logger.warning("Failed to initialize data-platform worker: %s", str(exc)[:200])
    logger.info("Service startup completed in %.3fs. Use POST /api/init to refresh market data.", time.perf_counter() - startup_t0)


@asynccontextmanager
async def lifespan(_: FastAPI):
    startup()
    try:
        yield
    finally:
        if _background_runtime_lease is not None:
            _background_runtime_lease.release()


app = FastAPI(title=settings.app_name, lifespan=lifespan)


@app.get("/api/openapi.json", include_in_schema=False)
def api_openapi() -> dict:
    """Serve the API catalog under the same /api prefix as browser requests."""
    return app.openapi()


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api")
protected_dependencies = [Depends(auth.require_current_user)]
for protected_router in (
    account.router,
    strategies.router,
    overview.router,
    candidates.router,
    signals.router,
    execute.router,
    equity.router,
    positions.router,
    backtest.router,
    logs.router,
    check.router,
    data_center_router,
    decision.router,
    factors.router,
    init_routes.router,
    quant_runs.router,
    model_strategy.router,
    research.router,
):
    app.include_router(
        protected_router,
        prefix="/api",
        dependencies=protected_dependencies,
    )


@app.get("/api/health")
def health() -> dict:
    primary = primary_database_health()
    data_platform = data_platform_database_health()
    databases_ready = primary.get("status") == "ok" and data_platform.get("status") == "ok"
    redis = redis_health()
    evolution = evolution_runtime_status()
    runtime_heartbeats = runtime_heartbeat_status()
    runtime_boundary = (
        _background_runtime_lease.status()
        if _background_runtime_lease is not None
        else {"mode": settings.background_runtime_mode, "should_start": False, "reason": "not_started"}
    )
    background_owner = bool(runtime_boundary.get("should_start"))
    failed_components = [name for name, item in _startup_components.items() if item.get("status") == "failed"]
    runtime_issues = [f"Startup component failed: {name}" for name in failed_components]
    if background_owner and not evolution.get("ok"):
        runtime_issues.append("Evolution worker or scheduler heartbeat is unhealthy")
    unhealthy_heartbeats = [name for name, state in runtime_heartbeats.items() if not state.get("healthy", True)]
    if background_owner and unhealthy_heartbeats:
        runtime_issues.append(f"Background component errors: {', '.join(unhealthy_heartbeats)}")
    issues = ([] if redis.get("available", False) else ["Redis unavailable: durable stream tasks are disabled"]) + runtime_issues
    runtime_ready = (
        not failed_components
        and (not background_owner or evolution.get("ok"))
        and (not background_owner or not unhealthy_heartbeats)
    )
    ready = databases_ready and bool(redis.get("available", False)) and runtime_ready
    return {
        "status": "ok" if ready else "error",
        "ready": ready,
        "degraded": databases_ready and bool(issues),
        "issues": issues,
        "dependencies": {
            "primary_database": primary,
            "data_platform_database": data_platform,
            "redis": redis,
            "startup_components": _startup_components,
            "background_runtime": runtime_boundary,
            "evolution_runtime": evolution,
            "runtime_heartbeats": runtime_heartbeats,
        },
    }
