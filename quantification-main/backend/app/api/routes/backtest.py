from __future__ import annotations

import logging
import threading
import uuid
from fastapi import APIRouter, HTTPException, Query
from starlette.responses import Response

from app.db.store import (
    create_backtest_result,
    delete_backtest_result,
    get_active_model_version,
    get_default_strategy_id,
    get_strategy,
    get_backtest_result,
    list_backtests,
)
from app.core.exchange_groups import normalize_exchange_group
from app.schemas.backtest import (
    BacktestJobResponse,
    BacktestRunAsyncResponse,
    BacktestEquityResponse,
    BacktestListItem,
    BacktestRunRequest,
    BacktestRunResponse,
    BacktestTradesResponse,
)
from app.services.backtest_engine import run_backtest
from app.services.strategy_config import get_unsupported_backtest_factors

router = APIRouter()
logger = logging.getLogger("quant.backtest_api")
_job_lock = threading.Lock()
_jobs: dict[str, dict] = {}


@router.post("/backtest/run", response_model=BacktestRunResponse)
def api_run_backtest(body: BacktestRunRequest) -> BacktestRunResponse:
    model_version_id = body.model_version_id
    if model_version_id is None and body.strategy_id is None:
        active = get_active_model_version()
        model_version_id = int(active["id"]) if active else None
    strategy_id = int(body.strategy_id or get_default_strategy_id() or 0)
    strategy = get_strategy(strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    unsupported = [] if model_version_id else get_unsupported_backtest_factors(strategy.get("content"), strategy.get("type", "formula"))
    if unsupported:
        raise HTTPException(status_code=400, detail=f"回测不支持这些因子: {', '.join(unsupported)}")
    logger.info(
        "backtest sync request strategy_id=%s range=%s~%s freq=%s benchmark=%s exchange_group=%s",
        strategy_id,
        body.start_date,
        body.end_date,
        body.rebalance_freq,
        body.benchmark_symbol,
        body.exchange_group,
    )
    exchange_group = normalize_exchange_group(body.exchange_group)
    try:
        result = run_backtest(
            strategy_id=strategy_id,
            model_version_id=model_version_id,
            start_date=body.start_date,
            end_date=body.end_date,
            initial_cash=body.initial_cash,
            rebalance_freq=body.rebalance_freq,
            benchmark_symbol=body.benchmark_symbol,
            exchange_group=exchange_group,
        )
    except ValueError as e:
        logger.warning("backtest sync validation failed: %s", str(e)[:200])
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("backtest sync failed strategy_id=%s", body.strategy_id)
        raise HTTPException(status_code=500, detail=f"回测执行失败: {str(e)[:120]}")

    backtest_id = create_backtest_result(result)
    return BacktestRunResponse(backtest_id=backtest_id, **result)


@router.post("/backtest/run-async", response_model=BacktestRunAsyncResponse)
def api_run_backtest_async(body: BacktestRunRequest) -> BacktestRunAsyncResponse:
    model_version_id = body.model_version_id
    if model_version_id is None and body.strategy_id is None:
        active = get_active_model_version()
        model_version_id = int(active["id"]) if active else None
    strategy_id = int(body.strategy_id or get_default_strategy_id() or 0)
    strategy = get_strategy(strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    unsupported = [] if model_version_id else get_unsupported_backtest_factors(strategy.get("content"), strategy.get("type", "formula"))
    if unsupported:
        raise HTTPException(status_code=400, detail=f"回测不支持这些因子: {', '.join(unsupported)}")
    job_id = uuid.uuid4().hex
    logger.info(
        "backtest async request job_id=%s strategy_id=%s range=%s~%s freq=%s benchmark=%s exchange_group=%s",
        job_id[:8],
        strategy_id,
        body.start_date,
        body.end_date,
        body.rebalance_freq,
        body.benchmark_symbol,
        body.exchange_group,
    )
    exchange_group = normalize_exchange_group(body.exchange_group)
    with _job_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "running",
            "processed_days": 0,
            "total_days": 0,
            "phase": "queued",
            "universe_processed": 0,
            "universe_total": 0,
            "last_date": None,
            "backtest_id": None,
            "error": None,
            "daily_records": [],
        }

    def _on_progress(payload: dict) -> None:
        with _job_lock:
            job = _jobs.get(job_id)
            if not job:
                return
            job["processed_days"] = max(int(job.get("processed_days", 0) or 0), int(payload.get("processed_days", 0) or 0))
            job["total_days"] = max(int(job.get("total_days", 0) or 0), int(payload.get("total_days", 0) or 0))
            job["phase"] = payload.get("phase") or job.get("phase")
            job["universe_processed"] = int(payload.get("universe_processed", 0) or 0)
            job["universe_total"] = int(payload.get("universe_total", 0) or 0)
            job["last_date"] = payload.get("date")
            if bool(payload.get("append_daily_record")):
                job["daily_records"].append(
                    {
                        "date": payload.get("date"),
                        "total_value": float(payload.get("total_value", 0) or 0),
                        "benchmark_value": float(payload.get("benchmark_value", 0) or 0),
                        "trades_count": int(payload.get("trades_count", 0) or 0),
                    }
                )

    def _worker() -> None:
        try:
            result = run_backtest(
                strategy_id=strategy_id,
                model_version_id=model_version_id,
                start_date=body.start_date,
                end_date=body.end_date,
                initial_cash=body.initial_cash,
                rebalance_freq=body.rebalance_freq,
                benchmark_symbol=body.benchmark_symbol,
                exchange_group=exchange_group,
                progress_callback=_on_progress,
            )
            backtest_id = create_backtest_result(result)
            with _job_lock:
                job = _jobs.get(job_id)
                if job:
                    job["status"] = "completed"
                    job["phase"] = "completed"
                    job["backtest_id"] = backtest_id
                    job["processed_days"] = max(job.get("processed_days", 0), len(result.get("equity_series", [])))
                    job["total_days"] = max(job.get("total_days", 0), len(result.get("equity_series", [])))
                    if result.get("equity_series"):
                        job["last_date"] = result["equity_series"][-1].get("date")
            logger.info(
                "backtest async completed job_id=%s backtest_id=%s days=%d",
                job_id[:8],
                backtest_id,
                len(result.get("equity_series", [])),
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("backtest async failed job_id=%s strategy_id=%s", job_id[:8], strategy_id)
            with _job_lock:
                job = _jobs.get(job_id)
                if job:
                    job["status"] = "failed"
                    job["phase"] = "failed"
                    job["error"] = str(e)[:200]

    t = threading.Thread(target=_worker, daemon=True, name=f"backtest-job-{job_id[:8]}")
    t.start()
    return BacktestRunAsyncResponse(job_id=job_id, status="running", total_days=0)


@router.get("/backtest/job/{job_id}", response_model=BacktestJobResponse)
def api_get_backtest_job(job_id: str) -> BacktestJobResponse:
    with _job_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Backtest job not found")
        return BacktestJobResponse(**job)


@router.get("/backtest/list", response_model=list[BacktestListItem])
def api_list_backtests(limit: int = Query(30, ge=1, le=200)) -> list[BacktestListItem]:
    rows = list_backtests(limit=limit)
    return [BacktestListItem(**x) for x in rows]


@router.get("/backtest/{backtest_id}", response_model=BacktestRunResponse)
def api_get_backtest(backtest_id: int) -> BacktestRunResponse:
    row = get_backtest_result(backtest_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Backtest not found")
    payload = dict(row)
    payload.pop("equity_series", None)
    payload.pop("benchmark_series", None)
    payload.pop("trades", None)
    return BacktestRunResponse(backtest_id=backtest_id, **payload)


@router.get("/backtest/{backtest_id}/equity", response_model=BacktestEquityResponse)
def api_get_backtest_equity(backtest_id: int) -> BacktestEquityResponse:
    row = get_backtest_result(backtest_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Backtest not found")
    return BacktestEquityResponse(
        backtest_id=backtest_id,
        equity_series=row.get("equity_series", []),
        benchmark_series=row.get("benchmark_series", []),
    )


@router.get("/backtest/{backtest_id}/trades", response_model=BacktestTradesResponse)
def api_get_backtest_trades(backtest_id: int) -> BacktestTradesResponse:
    row = get_backtest_result(backtest_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Backtest not found")
    return BacktestTradesResponse(
        backtest_id=backtest_id,
        trades=row.get("trades", []),
    )


@router.delete("/backtest/{backtest_id}")
def api_delete_backtest(backtest_id: int) -> Response:
    if not delete_backtest_result(backtest_id):
        raise HTTPException(status_code=404, detail="Backtest not found")
    return Response(status_code=204)

