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
from app.services.quant_task_runner import resume_quant_tasks_on_startup, start_auto_simulation_scheduler
from app.data_platform.services.daily_update import start_daily_update_scheduler
from app.services.model_governance_scheduler import start_model_governance_scheduler
from app.services.manual_review_triage import start_manual_review_triage_scheduler
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

def startup() -> None:
    """Keep service startup light.

    Heavy market-data refresh is now explicit via POST /api/init, so /api/health
    and the dashboard can come up even when TuShare or local caches are not ready.
    """

    startup_t0 = time.perf_counter()
    logger.info("Starting service: initializing database schema only.")
    init_db()
    init_data_platform_db()
    try:
        paused = resume_quant_tasks_on_startup()
        if paused > 0:
            logger.info("Paused interrupted quant tasks on startup: %d", paused)
        start_auto_simulation_scheduler()
        start_daily_update_scheduler()
        start_model_governance_scheduler()
        start_manual_review_triage_scheduler()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to reconcile interrupted quant tasks: %s", str(exc)[:120])
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
    return {
        "status": "ok" if ready else "error",
        "ready": ready,
        "degraded": ready and not redis.get("available", False),
        "issues": [] if redis.get("available", False) else ["Redis unavailable: durable stream tasks are disabled"],
        "dependencies": {
            "primary_database": primary,
            "data_platform_database": data_platform,
            "redis": redis,
        },
    }
