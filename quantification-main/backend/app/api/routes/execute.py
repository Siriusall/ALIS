from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.state import add_log, ensure_manual_trade_allowed
from app.core.a_share_fees import FEE_SCHEDULE_VERSION
from app.db.store import (
    add_account_balance,
    create_execution,
    get_account_balance,
    get_default_strategy_id,
    get_strategy,
    merge_execution_into_positions,
)
from app.integrations.broker_adapter import OrderRequest
from app.schemas.execution import ExecutionOrderResponse, ExecutionResponse
from app.services.execution_engine import simulate_open_execution


class _CandidateItem(BaseModel):
    symbol: str
    price: float
    name: Optional[str] = None
    change_pct: float = 0.0
    volume: float = -1.0
    close: float = 0.0


class ExecuteRequest(BaseModel):
    top_n: int = Field(10, ge=1, le=50)
    strategy_id: Optional[int] = Field(None, description="Strategy id; omit for default")
    # 纯执行接口：必须传入候选，不再在执行接口内部重跑选股逻辑。
    candidates: list[_CandidateItem] = Field(..., min_length=1, description="Pre-computed candidates from /api/candidates")


router = APIRouter()


def _order_total_cost(orders: list) -> float:
    """订单总成本（成交金额 + 佣金 + 过户费）。"""
    total = 0.0
    for o in orders:
        amount = getattr(o, "price", 0) * getattr(o, "quantity", 0)
        total += amount + getattr(o, "commission", 0) + getattr(o, "transfer_fee", 0)
    return round(total, 2)


@router.post("/execute/open", response_model=ExecutionResponse)
def execute_open(req: ExecuteRequest) -> ExecutionResponse:
    try:
        ensure_manual_trade_allowed("开仓")
    except RuntimeError as e:
        raise HTTPException(status_code=403, detail=str(e))
    balance = get_account_balance()
    if balance < 100:
        raise HTTPException(
            status_code=400,
            detail=f"账户余额不足（当前 {balance:.2f} 元），最低需 100 元，请先在账户中充值或设置资金",
        )
    effective_top_n = min(req.top_n, len(req.candidates))
    take = req.candidates[:effective_top_n]
    name_by_symbol = {c.symbol: (c.name or "") for c in take}
    order_requests = [
        OrderRequest(
            symbol=c.symbol,
            side="BUY",
            price=c.price,
            quantity=0,
            name=c.name or "",
            change_pct=c.change_pct,
            volume=c.volume,
            close=c.close,
        )
        for c in take
    ]
    strategy = get_strategy(req.strategy_id) if req.strategy_id is not None else None
    if strategy is None and req.strategy_id is None:
        strategy = get_strategy(get_default_strategy_id()) if get_default_strategy_id() else None
    result = simulate_open_execution(order_requests, budget=balance)
    total_cost = _order_total_cost(result.orders)
    if total_cost > balance:
        raise HTTPException(
            status_code=400,
            detail=f"本次买入需 {total_cost:.2f} 元，账户余额 {balance:.2f} 元不足",
        )
    add_account_balance(-total_cost)
    strategy_id_for_db = (strategy or {}).get("id") or get_default_strategy_id()
    filled_orders = [o for o in result.orders if int(getattr(o, "quantity", 0) or 0) > 0]
    request_price_by_symbol = {str(o.symbol): float(o.price) for o in order_requests}
    orders_payload = [
        {
            "symbol": o.symbol,
            "side": o.side,
            "price": o.price,
            "quantity": o.quantity,
            "commission": getattr(o, "commission", 0),
            "transfer_fee": getattr(o, "transfer_fee", 0),
            "slippage_bps": round(
                (float(o.price) / request_price_by_symbol[str(o.symbol)] - 1.0) * 10000.0, 4
            ) if request_price_by_symbol.get(str(o.symbol)) else 0.0,
            "fee_schedule_version": FEE_SCHEDULE_VERSION,
            "name": name_by_symbol.get(o.symbol, ""),
        }
        for o in filled_orders
    ]
    execution_id = None
    if strategy_id_for_db is not None and orders_payload:
        execution_id = create_execution(
            strategy_id=strategy_id_for_db,
            executed_at=result.executed_at,
            budget=total_cost,
            top_n=effective_top_n,
            orders=orders_payload,
        )
    if orders_payload:
        merge_execution_into_positions(orders_payload, result.executed_at, name_by_symbol=name_by_symbol)
    add_log("INFO", f"模拟买入: 扣款 {total_cost}, 余额={get_account_balance()}, execution_id={execution_id}")
    return ExecutionResponse(
        execution_id=execution_id,
        executed_at=result.executed_at,
        orders=[
            ExecutionOrderResponse(
                symbol=o.symbol,
                side=o.side,
                price=o.price,
                quantity=o.quantity,
                status=o.status,
                message=o.message,
                commission=getattr(o, "commission", 0),
                transfer_fee=getattr(o, "transfer_fee", 0),
                block_code=getattr(o, "block_code", ""),
            )
            for o in result.orders
        ],
    )
