from pydantic import BaseModel


class CandidateResponse(BaseModel):
    symbol: str
    name: str
    price: float
    change_pct: float
    volume: float
    turnover: float
    confidence: float


class CandidatesListResponse(BaseModel):
    candidates: list[CandidateResponse]
    warnings: list[str] = []
