from pydantic import BaseModel


class OverviewResponse(BaseModel):
    total_candidates: int
    avg_confidence: float
    top_signal: str
    last_run: str
