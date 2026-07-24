from pydantic import BaseModel


class SignalDetailResponse(BaseModel):
    symbol: str
    name: str
    factors: dict
    confidence: float
