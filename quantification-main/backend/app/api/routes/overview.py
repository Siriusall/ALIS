from datetime import datetime

from fastapi import APIRouter

from app.api.state import add_log
from app.schemas.overview import OverviewResponse
from app.services.factor_engine import score_factors
from app.services.stock_selector import select_affordable_stocks


router = APIRouter()


@router.get("/overview", response_model=OverviewResponse)
def get_overview() -> OverviewResponse:
    try:
        quotes = select_affordable_stocks(limit=None)
        scores = score_factors(quotes)
        avg_confidence = round(
            sum(s.confidence for s in scores) / max(len(scores), 1), 2
        )
        top_signal = scores[0].symbol if scores else ""
        add_log("INFO", f"概览请求：{len(scores)} 只候选")
        return OverviewResponse(
            total_candidates=len(scores),
            avg_confidence=avg_confidence,
            top_signal=top_signal,
            last_run=datetime.utcnow().isoformat(),
        )
    except Exception as e:
        add_log("ERROR", str(e))
        return OverviewResponse(
            total_candidates=0,
            avg_confidence=0.0,
            top_signal="",
            last_run=datetime.utcnow().isoformat(),
        )
