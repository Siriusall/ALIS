from pydantic import BaseModel


class ExecutionOrderItem(BaseModel):
    symbol: str
    side: str
    price: float
    quantity: int


class ExecutionListItem(BaseModel):
    id: int
    strategy_id: int
    executed_at: str
    budget: float
    top_n: int
    orders: list[ExecutionOrderItem]
