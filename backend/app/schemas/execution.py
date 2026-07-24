from pydantic import BaseModel


class ExecutionOrderResponse(BaseModel):
    symbol: str
    side: str
    price: float  # 成交价（已含滑点）
    quantity: int
    status: str
    message: str
    commission: float = 0.0
    transfer_fee: float = 0.0
    block_code: str = ""


class ExecutionResponse(BaseModel):
    execution_id: int | None = None
    executed_at: str
    orders: list[ExecutionOrderResponse]
