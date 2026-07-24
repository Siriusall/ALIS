from __future__ import annotations

from fastapi import APIRouter

from fastapi import HTTPException

from app.services.factor_registry import factor_library_summary
from app.services.factor_platform import (
    evaluate_materialized_factors,
    factor_registry_status,
    materialize_factor_snapshot,
    run_factor_daily_cycle,
)

router = APIRouter()


@router.get("/factors")
def list_factors() -> dict:
    return factor_library_summary()


@router.get("/factors/registry")
def factor_registry() -> dict:
    return factor_registry_status()


@router.post("/factors/materialize")
def materialize_factors(trade_date: str) -> dict:
    try:
        return materialize_factor_snapshot(trade_date=trade_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/factors/evaluate")
def evaluate_factors(trade_date: str, forward_date: str) -> dict:
    try:
        return evaluate_materialized_factors(trade_date=trade_date, forward_date=forward_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/factors/daily-cycle")
def factor_daily_cycle() -> dict:
    return run_factor_daily_cycle()
