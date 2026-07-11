from typing import Optional

from pydantic import BaseModel


class PositionResponse(BaseModel):
    id: int
    symbol: str
    side: str = "BUY"
    price: float
    quantity: int
    name: str = ""
    current_price: Optional[float] = None
    current_value: Optional[float] = None
    profit_loss: Optional[float] = None
    profit_loss_pct: Optional[float] = None
    strategy_next: Optional[str] = None


class OperationResponse(BaseModel):
    id: int
    execution_id: int
    symbol: str
    side: str
    price: float
    quantity: int
    at: str
    remark: str | None = None
    name: str = ""


class PositionsListResponse(BaseModel):
    positions: list[PositionResponse]
    data_source_time: Optional[str] = None