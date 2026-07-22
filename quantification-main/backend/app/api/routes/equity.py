from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.api.state import add_log
from app.db.store import get_execution, get_positions, list_executions
from app.services.equity_curve import compute_equity_curve

router = APIRouter()


@router.get("/positions-equity-curve")
def get_positions_equity_curve() -> dict:
    """按当前持仓计算跟盘折线（不按某次执行），返回 date / total_value / by_symbol 时间序列。"""
    positions = get_positions()
    if not positions:
        add_log("INFO", "持仓收益曲线：无持仓，返回空序列")
        return {"execution_id": None, "series": []}
    orders = [
        {"symbol": p["symbol"], "quantity": p["quantity"], "price": p["price"]}
        for p in positions
    ]
    executed_at = min((p.get("updated_at") or "") for p in positions) or None
    if not executed_at:
        executed_at = datetime.now().isoformat()[:10]
    curve = compute_equity_curve(orders, executed_at)
    add_log("INFO", f"持仓收益曲线：{len(positions)} 只股票，{len(curve)} 个点")
    return {"execution_id": None, "series": curve}


@router.get("/equity-curve")
def get_equity_curve(
    execution_id: int = Query(..., description="Execution id"),
) -> dict:
    """Returns time series: date, total_value, by_symbol (per-stock value per day)."""
    ex = get_execution(execution_id)
    if ex is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    orders = ex.get("orders", [])
    executed_at = ex.get("executed_at", "")
    curve = compute_equity_curve(orders, executed_at)
    add_log("INFO", f"收益曲线请求：execution_id={execution_id}，{len(curve)} 个点")
    return {"execution_id": execution_id, "series": curve}


@router.get("/strategy-equity-curve")
def get_strategy_equity_curve(
    strategy_id: Optional[int] = Query(None),
    execution_id: Optional[int] = Query(None, description="Override: use this execution"),
) -> dict:
    """Strategy PnL curve: by default uses latest execution for strategy_id; or pass execution_id."""
    if execution_id is not None:
        ex = get_execution(execution_id)
        if ex is None:
            raise HTTPException(status_code=404, detail="Execution not found")
        orders = ex.get("orders", [])
        executed_at = ex.get("executed_at", "")
        curve = compute_equity_curve(orders, executed_at)
        return {"execution_id": execution_id, "strategy_id": ex.get("strategy_id"), "series": curve}
    # Latest execution for strategy
    all_ex = list_executions(limit=100)
    ex = None
    for e in all_ex:
        if e.get("strategy_id") == strategy_id:
            ex = e
            break
    if ex is None:
        return {"execution_id": None, "strategy_id": strategy_id, "series": []}
    orders = ex.get("orders", [])
    executed_at = ex.get("executed_at", "")
    curve = compute_equity_curve(orders, executed_at)
    return {"execution_id": ex["id"], "strategy_id": strategy_id, "series": curve}
