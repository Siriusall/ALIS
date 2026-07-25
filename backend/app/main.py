import logging
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
from app.db.store import database_health as primary_database_health, init_db
from app.services.quant_task_runner import (
    ensure_autonomous_simulation_task,
    resume_quant_tasks_on_startup,
    start_auto_simulation_scheduler,
)
from app.data_platform.services.daily_update import start_daily_update_scheduler
from app.services.model_governance_scheduler import start_model_governance_scheduler
from app.services.manual_review_triage import start_manual_review_triage_scheduler
from app.services.evolution_orchestrator import (
    evolution_runtime_status,
    recover_evolution_tasks,
    start_evolution_scheduler,
    start_evolution_worker,
)
from app.services.production_strategy import ensure_production_strategy_champion
from app.services.profit_loop import recover_interrupted_daily_rebalance_plans
from app.data_platform.db import database_health as data_platform_database_health
from app.data_platform.db import init_db as init_data_platform_db
from app.data_platform.redis_store import start_task_worker
from app.data_platform.redis_store import health as redis_health
from app.data_platform.routes.data_center import router as data_center_router
from app.data_platform.services.backfill import process_stream_task, recover_interrupted_backfill
from app.data_platform.services.stream_admin import auto_heal_stream
from app.data_platform.services.data_center import recover_orphaned_ingestion_runs
from app.data_platform.services.runtime_logs import write_runtime_log

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("quant")
_startup_components: dict[str, dict] = {}


def _start_component(name: str, fn) -> object | None:
    try:
        result = fn()
        _startup_components[name] = {"status": "started", "error": "", "result": result}
        return result
    except Exception as exc:  # noqa: BLE001 - one subsystem must not suppress the rest
        _startup_components[name] = {"status": "failed", "error": str(exc)[:500]}
        logger.exception("Startup component failed: %s", name)
        return None

def startup() -> None:
    """Keep service startup light.

    Heavy market-data refresh is now explicit via POST /api/init, so /api/health
    and the dashboard can come up even when TuShare or local caches are not ready.
    """

    startup_t0 = time.perf_counter()
    logger.info("Starting service: initializing database schema only.")
    init_db()
    champion = ensure_production_strategy_champion()
    logger.info("Active ALIS production Champion: %s", champion.get("version"))
    init_data_platform_db()
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
    _start_component("model_governance_scheduler", start_model_governance_scheduler)
    _start_component("manual_review_scheduler", start_manual_review_triage_scheduler)
    recovered_evolution = _start_component("evolution_recovery", recover_evolution_tasks)
    _start_component("evolution_worker", start_evolution_worker)
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
        logger.warning("Failed to initialize data-platform worker: %s", str(exc)[:200])
    logger.info("Service startup completed in %.3fs. Use POST /api/init to refresh market data.", time.perf_counter() - startup_t0)


@asynccontextmanager
async def lifespan(_: FastAPI):
    startup()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)


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
    ready = primary.get("status") == "ok" and data_platform.get("status") == "ok"
    redis = redis_health()
    evolution = evolution_runtime_status()
    failed_components = [name for name, item in _startup_components.items() if item.get("status") == "failed"]
    runtime_issues = [f"Startup component failed: {name}" for name in failed_components]
    if not evolution.get("ok"):
        runtime_issues.append("Evolution worker or scheduler heartbeat is unhealthy")
    issues = ([] if redis.get("available", False) else ["Redis unavailable: durable stream tasks are disabled"]) + runtime_issues
    return {
        "status": "ok" if ready and not failed_components and evolution.get("ok") else "error",
        "ready": ready and not failed_components and bool(evolution.get("ok")),
        "degraded": ready and bool(issues),
        "issues": issues,
        "dependencies": {
            "primary_database": primary,
            "data_platform_database": data_platform,
            "redis": redis,
            "startup_components": _startup_components,
            "evolution_runtime": evolution,
        },
    }
