import logging
import sys
import time

from fastapi import FastAPI
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
from app.db.store import init_db
from app.services.quant_task_runner import resume_quant_tasks_on_startup, start_auto_simulation_scheduler
from app.data_platform.db import init_db as init_data_platform_db
from app.data_platform.redis_store import start_task_worker
from app.data_platform.routes.data_center import router as data_center_router
from app.data_platform.services.backfill import process_stream_task, recover_interrupted_backfill
from app.data_platform.services.runtime_logs import write_runtime_log

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("quant")

app = FastAPI(title=settings.app_name)


@app.on_event("startup")
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to reconcile interrupted quant tasks: %s", str(exc)[:120])
    try:
        recovery = recover_interrupted_backfill()
        start_task_worker(process_stream_task)
        if recovery.get("recovered"):
            write_runtime_log("WARNING", "backfill", "检测到服务重启中断的补数任务，已转换为可续跑状态", recovery)
        write_runtime_log("INFO", "data-platform", "统一后端中的数据中台已启动")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to initialize data-platform worker: %s", str(exc)[:200])
    logger.info("Service startup completed in %.3fs. Use POST /api/init to refresh market data.", time.perf_counter() - startup_t0)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api")
app.include_router(account.router, prefix="/api")
app.include_router(strategies.router, prefix="/api")
app.include_router(overview.router, prefix="/api")
app.include_router(candidates.router, prefix="/api")
app.include_router(signals.router, prefix="/api")
app.include_router(execute.router, prefix="/api")
app.include_router(equity.router, prefix="/api")
app.include_router(positions.router, prefix="/api")
app.include_router(backtest.router, prefix="/api")
app.include_router(logs.router, prefix="/api")
app.include_router(check.router, prefix="/api")
app.include_router(data_center_router, prefix="/api")
app.include_router(decision.router, prefix="/api")
app.include_router(factors.router, prefix="/api")
app.include_router(init_routes.router, prefix="/api")
app.include_router(quant_runs.router, prefix="/api")
app.include_router(model_strategy.router, prefix="/api")
app.include_router(research.router, prefix="/api")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}
