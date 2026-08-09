from __future__ import annotations

from fastapi import APIRouter, Query

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
from app.services.continuous_strategy_research import (
    continuous_strategy_research_status,
    strategy_research_analytics_console,
    strategy_research_archive_status,
    strategy_research_analytics,
    strategy_research_expert_history,
    strategy_research_factor_analytics,
    strategy_research_timeseries,
    strategy_research_wave_detail,
    strategy_research_enabled,
)
from app.services.evolution_orchestrator import create_evolution_task
from app.core.read_only_diagnostics import degraded_read

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
    try:
        return factor_strategy_lineage_status()
    except Exception as exc:  # noqa: BLE001 - optional lineage evidence must degrade safely
        return degraded_read("factor_strategy_lineage", exc, combinations=[], strategy_links=[])


@router.get("/factors/research/status")
def factor_research_status(compact: bool = Query(default=False), console: bool = Query(default=False)) -> dict:
    try:
        legacy = continuous_factor_research_status(compact=compact, console=console)
        if compact:
            return {
                **legacy,
                "v10": {
                    "status": "deferred",
                    "compact": True,
                    "deferred": True,
                    "reason": "模型治理首屏只需要当前研究快照与合同摘要",
                },
            }
        return {
            **legacy,
            "v10": continuous_strategy_research_status(console=console),
        }
    except Exception as exc:  # noqa: BLE001 - research diagnostics must degrade safely
        return degraded_read("factor_research_status", exc, v10={}, items=[])


@router.get("/factors/research/analytics")
def factor_research_analytics() -> dict:
    try:
        return strategy_research_analytics()
    except Exception as exc:  # noqa: BLE001 - research diagnostics must degrade safely
        return degraded_read("factor_research_analytics", exc, events=[], environments=[], factor_combinations=[])


@router.get("/factors/research/analytics/console")
def factor_research_analytics_console() -> dict:
    try:
        return strategy_research_analytics_console()
    except Exception as exc:  # noqa: BLE001 - Wave diagnostics must degrade safely
        return degraded_read("factor_research_analytics_console", exc, waves=[], expert_governance=[], environment_similarity=[])


@router.get("/factors/research/analytics/summary")
def factor_research_analytics_summary() -> dict:
    try:
        return strategy_research_analytics()
    except Exception as exc:  # noqa: BLE001 - research diagnostics must degrade safely
        return degraded_read("factor_research_analytics_summary", exc, events=[], environments=[], factor_combinations=[])


@router.get("/factors/research/analytics/factors")
def factor_research_analytics_factors(limit: int = 200) -> dict:
    try:
        return strategy_research_factor_analytics(limit=limit)
    except Exception as exc:  # noqa: BLE001 - research diagnostics must degrade safely
        return degraded_read("factor_research_factor_analytics", exc, items=[])


@router.get("/factors/research/analytics/factors/{factor_key}")
def factor_research_analytics_factor(factor_key: str, limit: int = 200) -> dict:
    try:
        return strategy_research_factor_analytics(factor_key=factor_key, limit=limit)
    except Exception as exc:  # noqa: BLE001 - research diagnostics must degrade safely
        return degraded_read("factor_research_factor", exc, factor_key=factor_key, items=[])


@router.get("/factors/research/analytics/timeseries")
def factor_research_analytics_timeseries(limit: int = 500) -> dict:
    try:
        return strategy_research_timeseries(limit=limit)
    except Exception as exc:  # noqa: BLE001 - research diagnostics must degrade safely
        return degraded_read("factor_research_timeseries", exc, items=[])


@router.get("/factors/research/analytics/waves/{wave_id}")
def factor_research_analytics_wave(wave_id: str) -> dict:
    result = strategy_research_wave_detail(wave_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail="research wave not found")
    return result


@router.get("/factors/research/analytics/experts/history")
def factor_research_analytics_experts(limit: int = 500) -> dict:
    try:
        return strategy_research_expert_history(limit=limit)
    except Exception as exc:  # noqa: BLE001 - research diagnostics must degrade safely
        return degraded_read("factor_research_experts", exc, items=[])


@router.get("/factors/research/analytics/environment-similarity")
def factor_research_analytics_environment_similarity() -> dict:
    try:
        analytics = strategy_research_analytics()
        return {
            "ok": True,
            "schema_version": "alis_environment_similarity_v1",
            "items": analytics.get("environment_similarity") or [],
        }
    except Exception as exc:  # noqa: BLE001 - research diagnostics must degrade safely
        return degraded_read("factor_research_environment_similarity", exc, items=[])


@router.get("/factors/research/analytics/archive")
def factor_research_analytics_archive() -> dict:
    try:
        return strategy_research_archive_status()
    except Exception as exc:  # noqa: BLE001 - research diagnostics must degrade safely
        return degraded_read("factor_research_archive", exc, items=[])


@router.post("/factors/research/start")
def start_factor_research() -> dict:
    """Queue research; never run a long historical replay in the API thread."""
    if strategy_research_enabled():
        return create_evolution_task(
            task_type="strategy_candidate_generation",
            payload={"trigger": "api", "batch_size": 4},
            dedupe_key="strategy_research_manual_v10",
            priority=90,
            force=True,
        )
    return create_evolution_task(
        task_type="factor_research_cycle",
        payload={"trigger": "api", "candidates_per_task": 1},
        dedupe_key="factor_research_manual",
        priority=90,
        force=True,
    )
