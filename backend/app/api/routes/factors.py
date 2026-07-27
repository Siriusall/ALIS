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
from app.services.factor_discovery import run_open_factor_discovery_cycle
from app.services.factor_strategy_evolution import (
    factor_strategy_lineage_status,
    run_factor_strategy_evolution_cycle,
)
from app.services.continuous_factor_research import continuous_factor_research_status
from app.services.evolution_orchestrator import create_evolution_task

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


@router.post("/factors/discover")
def discover_factors() -> dict:
    return run_open_factor_discovery_cycle()


@router.post("/factors/strategy-evolution")
def factor_strategy_evolution() -> dict:
    return run_factor_strategy_evolution_cycle()


@router.get("/factors/strategy-lineage")
def factor_strategy_lineage() -> dict:
    return factor_strategy_lineage_status()


@router.get("/factors/research/status")
def factor_research_status() -> dict:
    return continuous_factor_research_status()


@router.post("/factors/research/start")
def start_factor_research() -> dict:
    """Queue research; never run a long historical replay in the API thread."""
    return create_evolution_task(
        task_type="factor_research_cycle",
        payload={"trigger": "api", "candidates_per_task": 1},
        dedupe_key="factor_research_manual",
        priority=90,
        force=True,
    )
