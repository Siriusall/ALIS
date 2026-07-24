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
    execution_id: Optional[int] = None
    symbol: str
    side: str
    price: float
    quantity: int
    at: str
    remark: str | None = None
    name: str = ""
    commission: float = 0.0
    transfer_fee: float = 0.0
    stamp_tax: float = 0.0
    slippage_bps: float = 0.0
    fee_schedule_version: str = ""


class PositionsListResponse(BaseModel):
    positions: list[PositionResponse]
    data_source_time: Optional[str] = None
