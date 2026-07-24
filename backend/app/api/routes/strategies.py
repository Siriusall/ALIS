from __future__ import annotations

from fastapi import APIRouter, HTTPException
from starlette.responses import Response

from app.db.store import (
    create_strategy,
    delete_strategy,
    get_strategy,
    list_strategies,
    update_strategy,
)
from app.schemas.strategy import StrategyCreate, StrategyResponse, StrategyUpdate
from app.services.strategy_config import get_unsupported_backtest_factors

router = APIRouter()


@router.get("/strategies", response_model=list[StrategyResponse])
def api_list_strategies() -> list[StrategyResponse]:
    """List all strategies (including default)."""
    rows = list_strategies()
    return [StrategyResponse(**r) for r in rows]


@router.get("/strategies/{strategy_id}", response_model=StrategyResponse)
def api_get_strategy(strategy_id: int) -> StrategyResponse:
    """Get one strategy by id."""
    s = get_strategy(strategy_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return StrategyResponse(**s)


@router.post("/strategies", response_model=StrategyResponse)
def api_create_strategy(body: StrategyCreate) -> StrategyResponse:
    """Create a new strategy."""
    if body.type in ("formula", "preset"):
        unsupported = get_unsupported_backtest_factors(body.content, body.type)
        if unsupported:
            raise HTTPException(
                status_code=400,
                detail=f"存在未实现因子: {', '.join(unsupported)}",
            )
    s = create_strategy(
        name=body.name,
        type_=body.type,
        content=body.content,
    )
    return StrategyResponse(**s)


@router.put("/strategies/{strategy_id}", response_model=StrategyResponse)
def api_update_strategy(strategy_id: int, body: StrategyUpdate) -> StrategyResponse:
    """Update strategy (partial)."""
    current = get_strategy(strategy_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    merged_type = body.type if body.type is not None else current["type"]
    merged_content = body.content if body.content is not None else current["content"]
    if merged_type in ("formula", "preset"):
        unsupported = get_unsupported_backtest_factors(merged_content, merged_type)
        if unsupported:
            raise HTTPException(
                status_code=400,
                detail=f"存在未实现因子: {', '.join(unsupported)}",
            )
    s = update_strategy(
        strategy_id,
        name=body.name,
        type_=body.type,
        content=body.content,
    )
    return StrategyResponse(**s)


@router.delete("/strategies/{strategy_id}")
def api_delete_strategy(strategy_id: int) -> Response:
    """Delete strategy. Default strategy cannot be deleted. Returns 204 No Content."""
    if not delete_strategy(strategy_id):
        raise HTTPException(
            status_code=400,
            detail="Cannot delete default strategy or strategy not found",
        )
    return Response(status_code=204)
